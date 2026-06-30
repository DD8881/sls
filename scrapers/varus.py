import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo
from scrapers.http import REQUEST_GATE

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://varus.ua/api/graphql"
IMAGE_BASE = "https://varus.ua/img/product/300/300"
PAGE_SIZE = 48
# varus' GraphQL is the most fragile host (SSL-EOF / reset under load). Its
# fan-out is 3-level (store x category x page); keep cat/page low so even a few
# store-workers stay gentle. The global REQUEST_GATE caps the aggregate too.
CAT_WORKERS = 3
PAGE_WORKERS = 3
STORE_WORKERS = 10

CATEGORIES = [
    (52876, "Бакалія"),
    (53297, "Алкоголь"),
    (53036, "Молочні продукти та яйця"),
    (53028, "М'ясо та напівфабрикати"),
    (53253, "Фрукти, овочі, горіхи"),
    (53273, "Хлібобулочні вироби"),
    (52962, "Заморожені продукти"),
    (58295, "Консервація та соління"),
    (52971, "Кондитерські вироби та солодощі"),
    (52922, "Снеки"),
    (52905, "Чай, кава, гарячі напої"),
    (52956, "Вода, соки, напої"),
    (52981, "Гігієна та догляд"),
    (53168, "Побутова хімія"),
    (53085, "Товари для дому"),
    (53244, "Товари для тварин"),
    (57351, "Власна випічка та десерти VARUS"),
]

CITY_MAP = {
    1: "Дніпро",
    2: "Дніпро",
    6: "Бориспіль",
    8: "Київ",
    17: "Кривий Ріг",
    30: "Запоріжжя",
    48: "Кам'янське",
    49: "Мелітополь",
    52: "Верхньодніпровськ",
    53: "Інгулець",
    54: "Дніпро",
    55: "Дніпро",
    56: "Дніпро",
    57: "Лозова",
    60: "Вишгород",
    61: "Одеса",
    64: "Дніпро",
    68: "Київ",
    69: "Дніпро",
    72: "Київ",
    74: "Запоріжжя",
    75: "Київ",
    79: "Дніпро",
    81: "Запоріжжя",
}

PRODUCTS_QUERY = """\
{ category(id: "%s", shopId: %d) {
    products(page: %d, pageSize: %d, quickFilters: [{code: PROMO, selected: true}]) {
      total totalPages
      items {
        id sku name urlKey weight
        priceInfo { price specialPrice discountPercent specialTo }
        availability { inStock }
        primaryCategory { id name }
        gallery { mainImageTimestamp }
      }
    }
  }
}"""

SHOPS_QUERY = """\
{ product(id: "%s", shopId: 1) {
    shopAvailability {
      shop { id name address lat long cityId }
      availability { inStock }
    }
  }
}"""


class VarusScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            })
            self._local.session = s
        return s

    def chain_name(self) -> str:
        return "varus"

    def _graphql(self, query: str) -> dict:
        session = self._get_session()
        for attempt in range(4):
            with REQUEST_GATE:
                resp = session.post(
                    GRAPHQL_URL,
                    json={"query": query},
                    timeout=60,
                )
            if resp.status_code in (429, 502, 503) and attempt < 3:
                wait = 0.5 * (attempt + 2)
                log.warning("[varus] %d on attempt %d, retrying in %.1fs...",
                            resp.status_code, attempt + 1, wait)
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                log.warning("[varus] GraphQL errors: %s", data["errors"])
            return data
        resp.raise_for_status()

    def get_stores(self) -> list[StoreInfo]:
        log.info("[varus] Fetching stores...")
        sample = self._find_sample_product()
        data = self._graphql(SHOPS_QUERY % (sample, ))
        shops_data = data["data"]["product"]["shopAvailability"]

        stores = []
        for entry in shops_data:
            shop = entry["shop"]
            shop_id = int(shop["id"])
            city_id = int(shop.get("cityId") or 0)
            city = CITY_MAP.get(city_id)
            if not city:
                addr = shop.get("address") or shop.get("name") or ""
                city = self._city_from_address(addr)

            stores.append(StoreInfo(
                id=f"varus_{shop_id}",
                chain="varus",
                name=shop.get("name") or f"Varus #{shop_id}",
                city=city,
                address=shop.get("address"),
                lat=float(shop["lat"]) if shop.get("lat") else None,
                lng=float(shop["long"]) if shop.get("long") else None,
            ))
            stores[-1]._varus_shop_id = shop_id

        log.info("[varus] Found %d stores", len(stores))
        return stores

    def _find_sample_product(self) -> str:
        query = '{ category(id: "%d", shopId: 1) { products(page:1, pageSize:1) { items { id } } } }' % CATEGORIES[0][0]
        data = self._graphql(query)
        items = data["data"]["category"]["products"]["items"]
        return items[0]["id"] if items else "70631"

    @staticmethod
    def _city_from_address(addr: str) -> str | None:
        addr_lower = addr.lower()
        for marker, city in [
            ("київ", "Київ"), ("дніпро", "Дніпро"), ("кривий", "Кривий Ріг"),
            ("запоріжж", "Запоріжжя"), ("одес", "Одеса"), ("бориспіль", "Бориспіль"),
            ("вишгород", "Вишгород"), ("вишневе", "Вишневе"), ("ірпінь", "Ірпінь"),
            ("боярк", "Боярка"),
        ]:
            if marker in addr_lower:
                return city
        return None

    def scrape_categories(self) -> list[ScrapedCategory]:
        return [
            ScrapedCategory(chain="varus", slug=str(cat_id), title=name, parent_slug=None)
            for cat_id, name in CATEGORIES
        ]

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        shop_id = getattr(store, "_varus_shop_id", int(store.id.replace("varus_", "")))
        log.info("[varus] Fetching promos for %s (shop %d)...", store.address or store.name, shop_id)

        products = []
        with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_category_promos, shop_id, cat_id): cat_name
                for cat_id, cat_name in CATEGORIES
            }
            for future in as_completed(futures):
                cat_name = futures[future]
                try:
                    cat_products = future.result()
                    products.extend(cat_products)
                except Exception as e:
                    log.warning("[varus] %s: category %s failed: %s", store.name, cat_name, e)

        log.info("[varus] %s: %d promo products total", store.address or store.name, len(products))
        return products

    def _fetch_page(self, cat_id: int, shop_id: int, page: int) -> list[dict]:
        query = PRODUCTS_QUERY % (cat_id, shop_id, page, PAGE_SIZE)
        data = self._graphql(query)
        return data["data"]["category"]["products"]["items"]

    def _fetch_category_promos(self, shop_id: int, cat_id: int) -> list[ScrapedProduct]:
        query = PRODUCTS_QUERY % (cat_id, shop_id, 1, PAGE_SIZE)
        data = self._graphql(query)
        prod_data = data["data"]["category"]["products"]
        total_pages = prod_data["totalPages"]

        promos = []
        for item in prod_data["items"]:
            if item["priceInfo"].get("specialPrice"):
                promos.append(self._normalize(item, str(cat_id)))

        if total_pages <= 1:
            return promos

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_page, cat_id, shop_id, p): p
                for p in range(2, total_pages + 1)
            }
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    items = future.result()
                    for item in items:
                        if item["priceInfo"].get("specialPrice"):
                            promos.append(self._normalize(item, str(cat_id)))
                except Exception as e:
                    log.warning("[varus] shop %d cat %d page %d failed: %s",
                                shop_id, cat_id, page_num, e)

        return promos

    def _normalize(self, item: dict, category_slug: str) -> ScrapedProduct:
        pi = item["priceInfo"]
        price = pi["specialPrice"]
        old_price = pi["price"]
        discount_pct = pi.get("discountPercent")

        sku = item["sku"]
        url_key = item.get("urlKey")

        return ScrapedProduct(
            external_id=sku,
            chain="varus",
            category_slug=category_slug,
            title=item["name"],
            price=price,
            old_price=old_price if old_price != price else None,
            discount_pct=round(discount_pct, 1) if discount_pct else None,
            image_url=f"{IMAGE_BASE}/{sku}",
            url=f"https://varus.ua/{url_key}" if url_key else None,
            unit=None,
            in_stock=item.get("availability", {}).get("inStock", True),
            promo_end_date=pi.get("specialTo"),
        )
