"""Metro Cash & Carry Ukraine — own e-commerce API (shop.metro.ua), NOT zakaz.ua.

The storefront is a Module-Federation SPA on Metro's global "betty" platform.
Two public, auth-free endpoints power the catalog:

  1. searchdiscover/articlesearch/search  → resultIds + regular (shelf) price only
  2. evaluate.article.v1/betty-variants   → title, image, FINAL (discounted) price,
                                            strike-through old price, promo end date

So a promo listing is a two-step flow: collect resultIds for `filter=promotion:true`,
then batch them through betty-variants to get the actual discounted prices.

Metro promo flyers are national (one weekly catalog for all of Ukraine), so — like
ATB — we scrape the catalog ONCE from a reference store and attach it to per-city
synthetic stores. storeId here is the 5-digit store CODE ("00010"), not the UUID.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo
from scrapers.http import make_session

log = logging.getLogger(__name__)

BASE = "https://shop.metro.ua"
COUNTRY = "UA"
LANGUAGE = "uk-UA"

# Reference store to scrape the (national) promo catalog from. Online-visible Kyiv store.
REF_STORE_CODE = "00010"
# Promo catalog is national → attach to one synthetic store per city (like ATB).
METRO_CITIES = [
    "Київ", "Дніпро", "Харків", "Одеса", "Львів", "Запоріжжя", "Кривий Ріг",
    "Миколаїв", "Вінниця", "Полтава", "Чернівці", "Івано-Франківськ", "Житомир",
]

ROWS = 100        # search page size
PAGE_WORKERS = 4  # parallel search pages
BATCH_SIZE = 40   # resultIds per betty-variants call
BATCH_WORKERS = 6 # parallel betty-variants batches

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "CallTreeId": "BTEX-sls-scraper",
}


class MetroScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()
        self._products_cache: list[ScrapedProduct] | None = None
        self._cache_lock = threading.Lock()

    def chain_name(self) -> str:
        return "metro"

    # --- session per thread ---
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = make_session(_HEADERS)
            self._local.session = s
        return s

    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        resp = self._session().get(f"{BASE}{path}", params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    # --- categories ---
    def scrape_categories(self) -> list[ScrapedCategory]:
        log.info("[metro] Fetching category tree...")
        data = self._get(
            "/searchdiscover/articlesearch/mainCategories",
            [("country", COUNTRY), ("language", LANGUAGE), ("storeId", REF_STORE_CODE)],
        )
        cats: list[ScrapedCategory] = []
        for top in (data.get("children") or {}).values():
            top_name = top.get("displayName") or top.get("name")
            cats.append(ScrapedCategory(chain="metro", slug=top_name, title=top_name, parent_slug=None))
            for dep in (top.get("children") or {}).values():
                dep_name = dep.get("displayName") or dep.get("name")
                # slug == department display name; matches product category_slug + _METRO_MAP keys
                cats.append(ScrapedCategory(chain="metro", slug=dep_name, title=dep_name, parent_slug=top_name))
        log.info("[metro] %d categories", len(cats))
        return cats

    # --- stores ---
    def get_stores(self) -> list[StoreInfo]:
        # National promo catalog → synthetic per-city stores sharing it.
        return [
            StoreInfo(
                id=f"metro_{_translit(city)}",
                chain="metro",
                name=f"METRO ({city})",
                city=city,
                address=None,
            )
            for city in METRO_CITIES
        ]

    # --- products ---
    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        with self._cache_lock:
            if self._products_cache is None:
                self._products_cache = self._scrape_all()
        return self._products_cache

    def _scrape_all(self) -> list[ScrapedProduct]:
        result_ids = self._collect_promo_ids()
        log.info("[metro] %d promo resultIds collected", len(result_ids))

        batches = [result_ids[i:i + BATCH_SIZE] for i in range(0, len(result_ids), BATCH_SIZE)]
        products: dict[str, ScrapedProduct] = {}
        with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
            futures = [pool.submit(self._fetch_variants, b) for b in batches]
            for fut in as_completed(futures):
                try:
                    for p in fut.result():
                        products[p.external_id] = p
                except Exception as e:
                    log.warning("[metro] variants batch failed: %s", e)
        log.info("[metro] %d promo products parsed", len(products))
        return list(products.values())

    def _collect_promo_ids(self) -> list[str]:
        first = self._search(page=1)
        ids = list(first.get("resultIds") or [])
        total_pages = first.get("totalPages") or 1
        log.info("[metro] %d promo items across %d pages", first.get("amount"), total_pages)
        if total_pages <= 1:
            return ids

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = [pool.submit(self._search, p) for p in range(2, total_pages + 1)]
            for fut in as_completed(futures):
                try:
                    ids.extend(fut.result().get("resultIds") or [])
                except Exception as e:
                    log.warning("[metro] search page failed: %s", e)
        return ids

    def _search(self, page: int) -> dict:
        return self._get(
            "/searchdiscover/articlesearch/search",
            [
                ("country", COUNTRY), ("language", LANGUAGE), ("storeId", REF_STORE_CODE),
                ("query", "*"), ("rows", str(ROWS)), ("page", str(page)),
                ("facets", "false"), ("categories", "false"),
                ("filter", "promotion:true"),
            ],
        )

    def _fetch_variants(self, ids: list[str]) -> list[ScrapedProduct]:
        params = [("storeIds", REF_STORE_CODE), ("country", COUNTRY), ("locale", LANGUAGE)]
        params += [("ids", i) for i in ids]
        data = self._get("/evaluate.article.v1/betty-variants", params)
        wanted = set(ids)
        out: list[ScrapedProduct] = []
        for art_id, art in (data.get("result") or {}).items():
            for var_id, var in (art.get("variants") or {}).items():
                rid = f"{art_id}{var_id}"
                if rid not in wanted:
                    continue
                prod = self._normalize(rid, var)
                if prod is not None:
                    out.append(prod)
        return out

    def _normalize(self, rid: str, var: dict) -> ScrapedProduct | None:
        bundle, spi = self._pick_bundle_price(var)
        if spi is None:
            return None
        price = spi.get("finalPrice")
        if price is None:
            return None
        old_price = spi.get("strikeThrough") or spi.get("shelfPrice")
        if not (old_price and old_price > price):
            old_price = None
        discount_pct = round((1 - price / old_price) * 100) if old_price else None

        title = (bundle.get("description") or var.get("description") or "").strip()
        image = bundle.get("imageUrlL") or bundle.get("imageUrl") or var.get("imageUrlL")
        promo = (spi.get("promotionLabels") or {}).get("promotion") or {}

        return ScrapedProduct(
            external_id=rid,
            chain="metro",
            category_slug=_department(var) or _department(bundle),
            title=title,
            price=float(price),
            old_price=float(old_price) if old_price else None,
            discount_pct=discount_pct,
            image_url=image,
            url=None,
            unit=None,
            in_stock=True,
            promo_end_date=promo.get("end"),
        )

    @staticmethod
    def _pick_bundle_price(var: dict):
        """Return (bundle, sellingPriceInfo) for the reference store from any bundle."""
        for bundle in (var.get("bundles") or {}).values():
            store = (bundle.get("stores") or {}).get(REF_STORE_CODE)
            spi = _selling_price_info(store)
            if spi is not None:
                return bundle, spi
        return {}, None


def _selling_price_info(store_node) -> dict | None:
    if not isinstance(store_node, dict):
        return None
    spi = store_node.get("sellingPriceInfo")
    if isinstance(spi, dict) and spi.get("finalPrice") is not None:
        return spi
    for mode in (store_node.get("possibleDeliveryModes") or {}).values():
        for ft in ((mode or {}).get("possibleFulfillmentTypes") or {}).values():
            spi = (ft or {}).get("sellingPriceInfo")
            if isinstance(spi, dict) and spi.get("finalPrice") is not None:
                return spi
    return None


def _department(node: dict) -> str | None:
    """Level-1 category (department) name, e.g. 'Молочні продукти та яйця'."""
    cats = node.get("categories") or []
    if not cats:
        return None
    levels = cats[0].get("levels") or []
    if len(levels) >= 2:
        return levels[1].get("displayName")
    if levels:
        return levels[0].get("displayName")
    # fallback: split "A / B / C" path
    name = cats[0].get("name") or ""
    parts = [p.strip() for p in name.split("/")]
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else None)


def _translit(city: str) -> str:
    table = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
             "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
             "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
             "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia"}
    return "".join(table.get(ch, ch) for ch in city.lower())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = MetroScraper()
    prods = s.scrape_products(s.get_stores()[0])
    print(f"\n{len(prods)} promo products")
    for p in prods[:10]:
        print(f"  {p.discount_pct}% | {p.price}→{p.old_price} грн | {p.title[:48]} | {p.category_slug}")
