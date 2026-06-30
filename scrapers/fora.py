import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config
from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo
from scrapers.http import make_session

log = logging.getLogger(__name__)

# Fora has its own API (independent of zakaz.ua), shared SKU infra with Silpo (Fozzy).
API_URL = "https://api.catalog.ecom.fora.ua/api/2.0/exec/EcomCatalogGlobal"

MERCHANT_ID = 4          # Fora merchant id
BUSINESS_ID = 4
DELIVERY_TYPE = 1        # PICKUP (DELIVERY_FAR_AWAY=9, UNKNOWN=0)

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "https://fora.ua",
    "Referer": "https://fora.ua/",
}

CITY_WORKERS = 8
PAGE_WORKERS = 3  # fora drops connections under load; keep per-store page fan-out small

# Strip settlement-type prefixes so cities match the rest of the app ("м. Київ" -> "Київ").
_CITY_PREFIX = re.compile(r"^(?:м|с|смт|сел|сщ)\.?\s+", re.IGNORECASE)


def _clean_city(name: str | None) -> str | None:
    if not name:
        return None
    return _CITY_PREFIX.sub("", name).strip() or None


class ForaScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = make_session(HEADERS)
            self._local.session = s
        return s

    def _exec(self, method: str, data: dict) -> dict:
        resp = self._get_session().post(
            API_URL, json={"method": method, "data": data}, timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def chain_name(self) -> str:
        return "fora"

    def get_stores(self) -> list[StoreInfo]:
        log.info("[fora] Fetching pickup cities...")
        cities = self._exec(
            "GetPickupCities", {"merchantId": MERCHANT_ID, "businessId": BUSINESS_ID},
        ).get("items", []) or []
        log.info("[fora] %d cities, fetching filials...", len(cities))

        stores: list[StoreInfo] = []
        with ThreadPoolExecutor(max_workers=CITY_WORKERS) as pool:
            futures = {pool.submit(self._city_filials, city): city for city in cities}
            for future in as_completed(futures):
                city = futures[future]
                try:
                    stores.extend(future.result())
                except Exception as e:
                    log.warning("[fora] city %s failed: %s", city, e)

        log.info("[fora] Found %d stores", len(stores))
        return stores

    def _city_filials(self, city: str) -> list[StoreInfo]:
        items = self._exec(
            "GetPickupFilials",
            {"merchantId": MERCHANT_ID, "businessId": BUSINESS_ID, "city": city},
        ).get("items", []) or []
        result = []
        for f in items:
            store = StoreInfo(
                id=f"fora_{f['id']}",
                chain="fora",
                name=f.get("title") or f.get("address") or f"Фора {f['id']}",
                city=_clean_city(f.get("city") or city),
                address=f.get("address") or None,
                lat=f.get("lat"),
                lng=f.get("lon"),
            )
            store._filial_id = f["id"]
            result.append(store)
        return result

    def _any_filial_id(self):
        """Categories need a real filialId; grab the first Kyiv pickup filial."""
        items = self._exec(
            "GetPickupFilials",
            {"merchantId": MERCHANT_ID, "businessId": BUSINESS_ID, "city": "м. Київ"},
        ).get("items", []) or []
        return items[0]["id"] if items else 0

    def scrape_categories(self) -> list[ScrapedCategory]:
        log.info("[fora] Fetching categories...")
        tree = self._exec(
            "GetCategories",
            {"merchantId": MERCHANT_ID, "deliveryType": DELIVERY_TYPE,
             "filialId": self._any_filial_id()},
        ).get("tree", []) or []
        result = []
        for node in tree:
            parent = node.get("parentId")
            result.append(ScrapedCategory(
                chain="fora",
                slug=str(node["id"]),
                title=node.get("name") or str(node["id"]),
                parent_slug=str(parent) if parent else None,
            ))
        log.info("[fora] %d categories", len(result))
        return result

    def _fetch_page(self, filial_id, frm: int, to: int) -> dict:
        return self._exec("GetSimpleCatalogItems", {
            "merchantId": MERCHANT_ID,
            "deliveryType": DELIVERY_TYPE,
            "filialId": filial_id,
            "onlyPromo": True,
            "From": frm,
            "To": to,
        })

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        filial_id = getattr(store, "_filial_id", None)
        if filial_id is None:
            return []
        log.info("[fora] Fetching promos for %s...", store.name)
        page = config.PAGE_SIZE

        data = self._fetch_page(filial_id, 1, page)
        items = data.get("items", []) or []
        total = data.get("itemsCount", 0) or 0
        products = [self._normalize(it) for it in items]

        if total <= page or not items:
            log.info("[fora] %s: %d products", store.name, len(products))
            return products

        # Pages are 1-indexed, inclusive ranges: (1..page), (page+1..2*page), ...
        ranges = [(start, start + page - 1) for start in range(page + 1, total + 1, page)]
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = {pool.submit(self._fetch_page, filial_id, a, b): (a, b) for a, b in ranges}
            for future in as_completed(futures):
                try:
                    page_items = future.result().get("items", []) or []
                    products.extend(self._normalize(it) for it in page_items)
                except Exception as e:
                    rng = futures[future]
                    log.warning("[fora] %s: range %s failed: %s", store.id, rng, e)

        log.info("[fora] %s: %d / %d products", store.name, len(products), total)
        return products

    def _normalize(self, item) -> ScrapedProduct:
        price = item.get("price") or 0
        old_price = item.get("oldPrice")

        discount_pct = None
        if old_price and old_price > 0 and price < old_price:
            discount_pct = round((1 - price / old_price) * 100)
        else:
            dv = item.get("priceDiscountValue") or {}
            try:
                discount_pct = abs(int(float(dv.get("value")))) if dv.get("value") else None
            except (TypeError, ValueError):
                discount_pct = None

        cats = item.get("categories") or []
        category_slug = str(cats[0]["id"]) if cats and cats[0].get("id") else None

        slug = item.get("slug") or ""
        promo = item.get("promotion") or {}
        qty = item.get("storeQuantity")
        if qty is None:
            qty = item.get("quantity")

        return ScrapedProduct(
            external_id=str(item.get("id", "")),
            chain="fora",
            category_slug=category_slug,
            title=item.get("name", ""),
            price=price,
            old_price=old_price,
            discount_pct=discount_pct,
            image_url=item.get("mainImage") or None,
            url=f"https://fora.ua/product/{slug}" if slug else None,
            unit=item.get("unit") or item.get("unitText"),
            in_stock=(qty is None) or (qty > 0),
            promo_end_date=promo.get("stopAfter"),
        )
