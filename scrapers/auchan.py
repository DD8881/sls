import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as creq

from scrapers.base import BaseScraper, ScrapedCategory, ScrapedProduct, StoreInfo

log = logging.getLogger(__name__)

BASE = "https://auchan.ua"
GRAPHQL = f"{BASE}/graphql"
IMPERSONATE = "chrome"
STORE = "ua"  # Store header → Ukrainian product/category names

# auchan.ua is a single national online catalog (Magento 2 / PWA). Prices are the
# same regardless of city, so — like ATB — we scrape the catalog ONCE and attach
# it to synthetic per-city stores.
# Auchan UA physical stores (national prices). Curated city list.
AUCHAN_CITIES = [
    "Київ", "Одеса", "Львів", "Дніпро", "Запоріжжя", "Кривий Ріг",
    "Чернівці", "Біла Церква", "Рівне",
]

CAT_WORKERS = 6    # parallel level-3 categories
PAGE_WORKERS = 4   # parallel pages within a category
PAGE_SIZE = 200    # products per GraphQL page (works reliably up to 200)

# categoryList returns ~20 top-level entries; the real catalog lives under this one.
# Other roots are secondary nav that duplicates branches of the main tree.
ROOT_URL_KEY = "vsi-kategorii"

# Level-3 categories are "anchor" — querying one returns products from all of its
# descendants too, so iterating level-3 gives full coverage with useful slugs.
_CATEGORY_TREE_Q = """
{
  categoryList(filters:{}) {
    uid name url_key
    children {
      uid name url_key
      children { uid name url_key }
    }
  }
}
"""

_PRODUCTS_Q = """
query($uid:String!,$page:Int!,$size:Int!){
  products(filter:{category_uid:{eq:$uid}}, pageSize:$size, currentPage:$page){
    total_count
    page_info{ total_pages current_page }
    items{
      sku name url_key stock_status
      image{ url }
      small_image{ url }
      price_range{ minimum_price{
        regular_price{ value }
        final_price{ value }
        discount{ percent_off }
      } }
    }
  }
}
"""


class AuchanScraper(BaseScraper):
    def __init__(self):
        self._local = threading.local()
        self._products_cache: list[ScrapedProduct] | None = None
        self._cache_lock = threading.Lock()
        self._categories: list[ScrapedCategory] | None = None

    def _session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = creq.Session(impersonate=IMPERSONATE)
            self._local.session = s
        return s

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        resp = self._session().post(
            GRAPHQL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Content-Type": "application/json",
                "Origin": BASE,
                "Store": STORE,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    def chain_name(self) -> str:
        return "auchan"

    def scrape_categories(self) -> list[ScrapedCategory]:
        if self._categories is not None:
            return self._categories
        log.info("[auchan] Fetching categories...")
        cats: list[ScrapedCategory] = []
        for dept in self._catalog_root().get("children") or []:
            cats.append(ScrapedCategory(
                chain="auchan", slug=dept["url_key"], title=dept["name"], parent_slug=None,
            ))
            for l3 in dept.get("children") or []:
                cats.append(ScrapedCategory(
                    chain="auchan", slug=l3["url_key"], title=l3["name"],
                    parent_slug=dept["url_key"],
                ))
        self._categories = cats
        log.info("[auchan] Found %d categories", len(cats))
        return cats

    def get_stores(self) -> list[StoreInfo]:
        # Synthetic per-city stores sharing the national online catalog.
        return [
            StoreInfo(
                id=f"auchan_{_translit(city)}",
                chain="auchan",
                name=f"Ашан ({city})",
                city=city,
                address=None,
            )
            for city in AUCHAN_CITIES
        ]

    def scrape_products(self, store: StoreInfo) -> list[ScrapedProduct]:
        # National catalog is identical for every store — scrape once, cache.
        with self._cache_lock:
            if self._products_cache is None:
                self._products_cache = self._scrape_all()
        return self._products_cache

    def _scrape_all(self) -> list[ScrapedProduct]:
        # Level-3 categories are anchors; departments (parent_slug=None) duplicate them.
        leaf_cats = [c for c in self.scrape_categories() if c.parent_slug is not None]
        products: dict[str, ScrapedProduct] = {}
        with ThreadPoolExecutor(max_workers=CAT_WORKERS) as pool:
            futures = {pool.submit(self._scrape_category, c): c.slug for c in leaf_cats}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    for p in fut.result():
                        products[p.external_id] = p  # dedupe across categories
                except Exception as e:
                    log.warning("[auchan] category %s failed: %s", slug, e)
        log.info("[auchan] %d unique promo products", len(products))
        return list(products.values())

    def _scrape_category(self, cat: ScrapedCategory) -> list[ScrapedProduct]:
        uid = self._uid_for(cat.slug)
        if not uid:
            return []
        first = self._gql(_PRODUCTS_Q, {"uid": uid, "page": 1, "size": PAGE_SIZE})["products"]
        items = self._parse_promo(first["items"], cat.slug)
        total_pages = first["page_info"]["total_pages"] or 1
        if total_pages <= 1:
            return items

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            futures = [
                pool.submit(self._gql, _PRODUCTS_Q, {"uid": uid, "page": p, "size": PAGE_SIZE})
                for p in range(2, total_pages + 1)
            ]
            for fut in as_completed(futures):
                try:
                    items.extend(self._parse_promo(fut.result()["products"]["items"], cat.slug))
                except Exception as e:
                    log.warning("[auchan] %s page failed: %s", cat.slug, e)
        return items

    def _uid_for(self, slug: str) -> str | None:
        return self._uid_map().get(slug)

    def _uid_map(self) -> dict[str, str]:
        cached = getattr(self, "_uids", None)
        if cached is not None:
            return cached
        m: dict[str, str] = {}
        for dept in self._catalog_root().get("children") or []:
            m[dept["url_key"]] = dept["uid"]
            for l3 in dept.get("children") or []:
                m[l3["url_key"]] = l3["uid"]
        self._uids = m
        return m

    def _catalog_root(self) -> dict:
        cached = getattr(self, "_root", None)
        if cached is not None:
            return cached
        roots = self._gql(_CATEGORY_TREE_Q)["categoryList"]
        root = next((r for r in roots if r.get("url_key") == ROOT_URL_KEY), None)
        if root is None:
            raise RuntimeError(f"[auchan] catalog root '{ROOT_URL_KEY}' not found")
        self._root = root
        return root

    @staticmethod
    def _parse_promo(items: list[dict], slug: str) -> list[ScrapedProduct]:
        out: list[ScrapedProduct] = []
        for it in items:
            mp = (it.get("price_range") or {}).get("minimum_price") or {}
            pct = ((mp.get("discount") or {}).get("percent_off")) or 0
            if pct <= 0:
                continue
            final = (mp.get("final_price") or {}).get("value")
            regular = (mp.get("regular_price") or {}).get("value")
            if final is None:
                continue
            sku = it.get("sku")
            if not sku:
                continue
            img = (it.get("image") or {}).get("url") or (it.get("small_image") or {}).get("url")
            url_key = it.get("url_key")
            out.append(ScrapedProduct(
                external_id=f"auchan_{sku}",
                chain="auchan",
                category_slug=slug,
                title=it.get("name") or "",
                price=float(final),
                old_price=float(regular) if regular and regular > final else None,
                discount_pct=round(pct),
                image_url=img,
                url=f"{BASE}/{url_key}.html" if url_key else None,
                unit=None,
                in_stock=(it.get("stock_status") == "IN_STOCK"),
                promo_end_date=None,
            ))
        return out


def _translit(city: str) -> str:
    table = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
             "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
             "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
             "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia"}
    return "".join(table.get(ch, ch) for ch in city.lower())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sc = AuchanScraper()
    prods = sc.scrape_products(sc.get_stores()[0])
    print(f"\n{len(prods)} promo products")
    for p in prods[:8]:
        print(f"  {p.discount_pct}% | {p.price}→{p.old_price} грн | {p.title[:50]} | {p.category_slug}")
