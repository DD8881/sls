import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import config
from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo
from scrapers.http import make_session

log = logging.getLogger(__name__)

BASE_URL = "https://sf-ecom-api.silpo.ua/v1/uk"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "https://silpo.ua",
    "Referer": "https://silpo.ua/",
}

NULL_BRANCH = "00000000-0000-0000-0000-000000000000"

PAGE_WORKERS = 5


class SilpoScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = make_session(HEADERS)
            self._local.session = s
        return s

    def chain_name(self) -> str:
        return "silpo"

    def get_stores(self) -> list[StoreInfo]:
        log.info("[silpo] Fetching branches...")
        resp = self._get_session().get(f"{BASE_URL}/branches", timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        stores = []
        for b in items:
            ext_id = b.get("externalId", b["branchId"][:8])
            store = StoreInfo(
                id=f"silpo_{ext_id}",
                chain="silpo",
                name=f"Сільпо {b.get('addressFull', '')}",
                city=b.get("cityFull") or None,
                address=b.get("addressFull") or None,
                lat=float(b["latitude"]) if b.get("latitude") else None,
                lng=float(b["longitude"]) if b.get("longitude") else None,
            )
            store._branch_id = b["branchId"]
            stores.append(store)
        log.info("[silpo] Found %d branches", len(stores))
        return stores

    def scrape_categories(self) -> list[ScrapedCategory]:
        log.info("[silpo] Fetching categories...")
        resp = self._get_session().get(
            f"{BASE_URL}/branches/{NULL_BRANCH}/categories", timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", data.get("content", []))
        return self._parse_categories(items, parent_slug=None)

    def _parse_categories(self, items, parent_slug):
        result = []
        for item in items:
            slug = item.get("slug") or item.get("sectionSlug") or str(item.get("id", ""))
            if not slug:
                continue
            result.append(ScrapedCategory(
                chain="silpo", slug=slug,
                title=item.get("title") or item.get("name", slug),
                parent_slug=parent_slug,
            ))
            children = item.get("children") or item.get("subcategories") or []
            if children:
                result.extend(self._parse_categories(children, slug))
        return result

    def _fetch_page(self, branch_id: str, offset: int, limit: int) -> dict:
        resp = self._get_session().get(
            f"{BASE_URL}/branches/{branch_id}/products",
            params={"mustHavePromotion": "true", "limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        branch_id = getattr(store, "_branch_id", NULL_BRANCH)
        log.info("[silpo] Fetching promos for %s...", store.address or store.id)
        limit = config.PAGE_SIZE

        data = self._fetch_page(branch_id, 0, limit)
        items = data.get("items", data.get("content", []))
        total = data.get("total", 0)
        products = [self._normalize(item) for item in items]

        if total <= limit or not items:
            log.info("[silpo] %s: %d products", store.address or store.id, len(products))
            return products

        offsets = list(range(limit, total, limit))
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = {pool.submit(self._fetch_page, branch_id, off, limit): off for off in offsets}
            for future in as_completed(futures):
                try:
                    page_data = future.result()
                    page_items = page_data.get("items", page_data.get("content", []))
                    products.extend(self._normalize(item) for item in page_items)
                except Exception as e:
                    off = futures[future]
                    log.warning("[silpo] %s: page offset=%d failed: %s", store.id, off, e)

        log.info("[silpo] %s: %d / %d products", store.address or store.id, len(products), total)
        return products

    def _normalize(self, item):
        price = item.get("price", 0)
        old_price = item.get("oldPrice")
        discount_pct = None
        if old_price and old_price > 0 and price < old_price:
            discount_pct = round((1 - price / old_price) * 100)

        icon = item.get("icon", "")
        if icon and not icon.startswith("http"):
            icon = f"https://images.silpo.ua/v2/products/400x400/webp/{icon}"

        slug = item.get("slug", "")
        return ScrapedProduct(
            external_id=str(item.get("id") or item.get("externalProductId", "")),
            chain="silpo",
            category_slug=item.get("sectionSlug"),
            title=item.get("title", ""),
            price=price,
            old_price=old_price,
            discount_pct=discount_pct,
            image_url=icon or None,
            url=f"https://silpo.ua/product/{slug}" if slug else None,
            unit=item.get("ratioTitle") or item.get("unit"),
            in_stock=item.get("stock", 1) > 0 if isinstance(item.get("stock"), (int, float)) else True,
            promo_end_date=None,
        )
