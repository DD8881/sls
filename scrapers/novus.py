from __future__ import annotations

import logging
import re
import threading
import time

import requests

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://novus.ua/graphql"
SHOPS_URL = "https://novus.ua/shops"
SALES_CATEGORY_ID = "108"
PAGE_SIZE = 500
CRAWL_DELAY = 1.5

PRODUCTS_QUERY = (
    "{products(filter:{category_id:{eq:\"%s\"}},pageSize:%d,currentPage:%d)"
    "{total_count,page_info{total_pages},items{name,sku,url_key,special_price,"
    "special_to_date,novus_shops,categories{id,name},image{url},"
    "price_range{minimum_price{regular_price{value},final_price{value},"
    "discount{percent_off}}}}}}"
)

CATEGORIES_QUERY = (
    "{categoryList(filters:{ids:{eq:\"%s\"}})"
    "{children{id,name,url_key,children{id,name,url_key}}}}"
)


class NovusScraper(BaseScraper):
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        self._products_cache: list[dict] | None = None
        self._fetch_lock = threading.Lock()

    def chain_name(self) -> str:
        return "novus"

    def get_stores(self) -> list[StoreInfo]:
        log.info("[novus] Fetching stores from novus.ua...")
        resp = self._session.get(SHOPS_URL, timeout=30)
        resp.raise_for_status()
        stores = []
        for m in re.findall(r'(\[{.*?}\])', resp.text, re.DOTALL):
            try:
                data = __import__("json").loads(m)
            except ValueError:
                continue
            if not data or "novus_shop_id" not in data[0]:
                continue
            for s in data:
                stores.append(StoreInfo(
                    id=f"novus_{s['novus_shop_id']}",
                    chain="novus",
                    name=s.get("title") or s.get("address", ""),
                    city=s.get("city_name") or None,
                    address=s.get("address") or None,
                    lat=float(s["lat"]) if s.get("lat") else None,
                    lng=float(s["lng"]) if s.get("lng") else None,
                ))
                stores[-1]._novus_shop_id = str(s["novus_shop_id"])
            break
        log.info("[novus] Found %d stores", len(stores))
        return stores

    def scrape_categories(self) -> list[ScrapedCategory]:
        log.info("[novus] Fetching categories...")
        query = CATEGORIES_QUERY % SALES_CATEGORY_ID
        data = self._graphql(query).get("data", {})
        cat_list = data.get("categoryList", [])
        if not cat_list:
            return []
        result = []
        for child in cat_list[0].get("children", []):
            slug = str(child["id"])
            result.append(ScrapedCategory(
                chain="novus", slug=slug,
                title=child["name"], parent_slug=None,
            ))
            for sub in child.get("children", []):
                result.append(ScrapedCategory(
                    chain="novus", slug=str(sub["id"]),
                    title=sub["name"], parent_slug=slug,
                ))
        log.info("[novus] %d categories", len(result))
        return result

    def _graphql(self, query: str) -> dict:
        for attempt in range(4):
            resp = self._session.post(
                GRAPHQL_URL,
                json={"query": query},
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            if resp.status_code in (502, 503) and attempt < 3:
                wait = CRAWL_DELAY * (attempt + 1)
                log.warning("[novus] %d on attempt %d, retrying in %.1fs...", resp.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def _fetch_all_products(self):
        if self._products_cache is not None:
            return self._products_cache
        with self._fetch_lock:
            if self._products_cache is not None:
                return self._products_cache
            log.info("[novus] Fetching all sale products from GraphQL...")
            all_items = []
            page = 1
            while True:
                query = PRODUCTS_QUERY % (SALES_CATEGORY_ID, PAGE_SIZE, page)
                data = self._graphql(query)["data"]["products"]
                items = data.get("items", [])
                total = data["total_count"]
                total_pages = data["page_info"]["total_pages"]
                all_items.extend(items)
                log.info("[novus] Page %d/%d — %d/%d products", page, total_pages, len(all_items), total)
                if page >= total_pages or not items:
                    break
                page += 1
                time.sleep(CRAWL_DELAY)
            self._products_cache = all_items
            log.info("[novus] Fetched %d sale products total", len(all_items))
            return all_items

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        shop_id = getattr(store, "_novus_shop_id", store.id.replace("novus_", ""))
        all_products = self._fetch_all_products()
        products = []
        for item in all_products:
            shops_str = item.get("novus_shops") or ""
            shop_ids = shops_str.split(",")
            if shop_id not in shop_ids:
                continue
            products.append(self._normalize(item))
        return products

    def _normalize(self, item) -> ScrapedProduct:
        price_info = item["price_range"]["minimum_price"]
        old_price = price_info["regular_price"]["value"]
        price = price_info["final_price"]["value"]
        discount_pct = price_info["discount"]["percent_off"]

        cats = [c for c in (item.get("categories") or []) if c["id"] != 108]
        category_slug = str(cats[0]["id"]) if cats else None

        url_key = item.get("url_key")
        return ScrapedProduct(
            external_id=item["sku"],
            chain="novus",
            category_slug=category_slug,
            title=item["name"],
            price=price,
            old_price=old_price if old_price != price else None,
            discount_pct=round(discount_pct, 1) if discount_pct else None,
            image_url=(item.get("image") or {}).get("url"),
            url=f"https://novus.ua/{url_key}.html" if url_key else None,
            unit=None,
            in_stock=True,
            promo_end_date=item.get("special_to_date"),
        )
