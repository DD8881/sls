from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,
    chain       TEXT NOT NULL,
    name        TEXT NOT NULL,
    city        TEXT,
    address     TEXT,
    lat         REAL,
    lng         REAL
);

CREATE TABLE IF NOT EXISTS categories (
    id          TEXT PRIMARY KEY,
    chain       TEXT NOT NULL,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    parent_slug TEXT,
    UNIQUE(chain, slug)
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT NOT NULL,
    chain           TEXT NOT NULL,
    category_slug   TEXT,
    title           TEXT NOT NULL,
    image_url       TEXT,
    url             TEXT,
    unit            TEXT,
    unified_category TEXT,
    UNIQUE(chain, external_id)
);

CREATE TABLE IF NOT EXISTS store_products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    store_id        TEXT NOT NULL REFERENCES stores(id),
    price           REAL NOT NULL,
    old_price       REAL,
    discount_pct    REAL,
    in_stock        INTEGER DEFAULT 1,
    promo_end_date  TEXT,
    scraped_at      TEXT NOT NULL,
    UNIQUE(product_id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_products_chain ON products(chain);
CREATE INDEX IF NOT EXISTS idx_products_unified ON products(unified_category);
CREATE INDEX IF NOT EXISTS idx_products_external ON products(chain, external_id);
CREATE INDEX IF NOT EXISTS idx_sp_store ON store_products(store_id);
CREATE INDEX IF NOT EXISTS idx_sp_product ON store_products(product_id);
CREATE INDEX IF NOT EXISTS idx_sp_discount ON store_products(discount_pct DESC);
CREATE INDEX IF NOT EXISTS idx_stores_city ON stores(city);
"""


def get_connection(check_same_thread: bool = True) -> sqlite3.Connection:
    """Open the SQLite database.

    Pass ``check_same_thread=False`` to share one connection across threads (the
    scraper runs all chains concurrently and serialises every write behind a
    single lock). ``busy_timeout`` keeps a concurrent reader — e.g. the bot — from
    failing instantly if it touches the DB mid-write.
    """
    conn = sqlite3.connect(config.DATABASE_PATH, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(_SCHEMA)
    conn.close()


def normalize_city(city: str | None) -> str | None:
    """Canonicalize city names so the same place groups into one page.

    Source chains disagree on the apostrophe (straight ' / modifier ʼ vs the
    Ukrainian typographic ’), which otherwise splits e.g. Кам'янське in two.
    """
    if not city:
        return city
    return city.replace("'", "’").replace("ʼ", "’").strip()


def upsert_store(conn, store_id: str, chain: str, name: str,
                 city: str | None = None, address: str | None = None,
                 lat: float | None = None, lng: float | None = None):
    city = normalize_city(city)
    conn.execute(
        "INSERT OR REPLACE INTO stores (id, chain, name, city, address, lat, lng) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (store_id, chain, name, city, address, lat, lng),
    )


def upsert_categories(conn, categories):
    for cat in categories:
        conn.execute(
            "INSERT OR REPLACE INTO categories (id, chain, slug, title, parent_slug) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{cat.chain}::{cat.slug}", cat.chain, cat.slug, cat.title, cat.parent_slug),
        )


def upsert_products(conn, products, store_id: str):
    """Upsert products and link them to a store.

    Batched: one executemany for products, a chunked id lookup, then one
    executemany for store_products. Avoids the per-product SELECT round-trip,
    which was the bottleneck for large stores (thousands of promo items).
    """
    if not products:
        return
    now = datetime.now(timezone.utc).isoformat()

    conn.executemany(
        "INSERT INTO products (external_id, chain, category_slug, title, image_url, url, unit) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chain, external_id) DO UPDATE SET "
        "title=excluded.title, image_url=excluded.image_url, url=excluded.url, "
        "unit=excluded.unit, category_slug=excluded.category_slug",
        [(p.external_id, p.chain, p.category_slug, p.title,
          p.image_url, p.url, p.unit) for p in products],
    )

    # Resolve product ids in bulk (all products in a batch share one chain).
    chain = products[0].chain
    ext_ids = [p.external_id for p in products]
    id_map: dict[str, int] = {}
    for i in range(0, len(ext_ids), 500):
        chunk = ext_ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT external_id, id FROM products WHERE chain = ? AND external_id IN ({placeholders})",
            [chain, *chunk],
        ).fetchall()
        for ext_id, pid in rows:
            id_map[ext_id] = pid

    conn.executemany(
        "INSERT INTO store_products (product_id, store_id, price, old_price, "
        "discount_pct, in_stock, promo_end_date, scraped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(product_id, store_id) DO UPDATE SET "
        "price=excluded.price, old_price=excluded.old_price, "
        "discount_pct=excluded.discount_pct, in_stock=excluded.in_stock, "
        "promo_end_date=excluded.promo_end_date, scraped_at=excluded.scraped_at",
        [(id_map[p.external_id], store_id, p.price, p.old_price,
          p.discount_pct, int(p.in_stock), p.promo_end_date, now) for p in products],
    )


def clear_store_products(conn, store_id: str):
    conn.execute("DELETE FROM store_products WHERE store_id = ?", (store_id,))


def cleanup_orphaned_products(conn) -> int:
    cur = conn.execute(
        "DELETE FROM products WHERE id NOT IN (SELECT DISTINCT product_id FROM store_products)"
    )
    return cur.rowcount


def get_cities(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT s.city, COUNT(DISTINCT sp.product_id) as product_cnt, "
        "COUNT(DISTINCT s.id) as store_cnt "
        "FROM stores s "
        "JOIN store_products sp ON sp.store_id = s.id "
        "WHERE s.city IS NOT NULL AND s.city != '' "
        "GROUP BY s.city ORDER BY product_cnt DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_stores(conn, city: str | None = None, chain: str | None = None) -> list[dict]:
    where, params = [], []
    if city:
        where.append("s.city = ?")
        params.append(city)
    if chain:
        where.append("s.chain = ?")
        params.append(chain)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT s.*, COUNT(sp.id) as product_cnt "
        f"FROM stores s "
        f"LEFT JOIN store_products sp ON sp.store_id = s.id "
        f"{where_sql} "
        f"GROUP BY s.id ORDER BY s.chain, s.name",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_chains(conn, city: str | None = None) -> list[dict]:
    where, params = [], []
    if city:
        where.append("s.city = ?")
        params.append(city)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT s.chain, COUNT(DISTINCT p.id) as cnt "
        f"FROM stores s "
        f"JOIN store_products sp ON sp.store_id = s.id "
        f"JOIN products p ON p.id = sp.product_id "
        f"{where_sql} "
        f"GROUP BY s.chain ORDER BY cnt DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_unified_categories(conn, chain: str | None = None, city: str | None = None) -> list[dict]:
    joins = "FROM products p"
    where = ["p.unified_category IS NOT NULL"]
    params = []
    if city or chain:
        joins += " JOIN store_products sp ON sp.product_id = p.id JOIN stores s ON s.id = sp.store_id"
        if city:
            where.append("s.city = ?")
            params.append(city)
        if chain:
            where.append("s.chain = ?")
            params.append(chain)
    where_sql = "WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"SELECT p.unified_category as slug, COUNT(DISTINCT p.id) as cnt "
        f"{joins} {where_sql} "
        f"GROUP BY p.unified_category HAVING cnt > 0 ORDER BY cnt DESC",
        params,
    ).fetchall()
    from categories_map import UNIFIED_DICT
    return [{"slug": r["slug"], "title": UNIFIED_DICT.get(r["slug"], r["slug"]), "cnt": r["cnt"]} for r in rows]


def get_products(
    conn,
    chain: str | None = None,
    category_slug: str | None = None,
    city: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict], int]:
    joins = (
        "FROM products p "
        "JOIN store_products sp ON sp.product_id = p.id "
        "JOIN stores s ON s.id = sp.store_id"
    )
    where, params = [], []
    if chain:
        where.append("s.chain = ?")
        params.append(chain)
    if category_slug:
        where.append("p.unified_category = ?")
        params.append(category_slug)
    if city:
        where.append("s.city = ?")
        params.append(city)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Count distinct products
    total = conn.execute(
        f"SELECT COUNT(DISTINCT p.id) {joins} {where_sql}", params
    ).fetchone()[0]

    # Get products with best price across matching stores
    rows = conn.execute(
        f"SELECT p.*, MIN(sp.price) as price, sp.old_price, "
        f"MAX(sp.discount_pct) as discount_pct, sp.promo_end_date, "
        f"s.chain, COUNT(DISTINCT s.id) as store_count "
        f"{joins} {where_sql} "
        f"GROUP BY p.id "
        f"ORDER BY discount_pct DESC NULLS LAST "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def get_city_store_products(conn, city: str) -> dict[int, list[dict]]:
    rows = conn.execute(
        "SELECT sp.product_id, s.id, sp.price, sp.old_price, sp.discount_pct, sp.promo_end_date "
        "FROM store_products sp "
        "JOIN stores s ON s.id = sp.store_id "
        "WHERE s.city = ? "
        "ORDER BY sp.product_id, sp.price ASC",
        (city,),
    ).fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        pid = r["product_id"]
        if pid not in result:
            result[pid] = []
        result[pid].append(dict(r))
    return result


def get_product_stores(conn, product_id: int, city: str | None = None) -> list[dict]:
    """Get all stores that carry a specific product (with prices)."""
    where = ["sp.product_id = ?"]
    params: list = [product_id]
    if city:
        where.append("s.city = ?")
        params.append(city)
    where_sql = "WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"SELECT s.id, s.chain, s.name, s.address, "
        f"sp.price, sp.old_price, sp.discount_pct, sp.promo_end_date "
        f"FROM store_products sp "
        f"JOIN stores s ON s.id = sp.store_id "
        f"{where_sql} "
        f"ORDER BY sp.price ASC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def search_products(
    conn,
    query: str,
    chain: str | None = None,
    city: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict], int]:
    joins = (
        "FROM products p "
        "JOIN store_products sp ON sp.product_id = p.id "
        "JOIN stores s ON s.id = sp.store_id"
    )
    where = ["p.title LIKE ?"]
    params: list = [f"%{query}%"]
    if chain:
        where.append("s.chain = ?")
        params.append(chain)
    if city:
        where.append("s.city = ?")
        params.append(city)
    where_sql = "WHERE " + " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(DISTINCT p.id) {joins} {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT p.*, MIN(sp.price) as price, sp.old_price, "
        f"MAX(sp.discount_pct) as discount_pct, sp.promo_end_date, "
        f"s.chain, COUNT(DISTINCT s.id) as store_count "
        f"{joins} {where_sql} "
        f"GROUP BY p.id "
        f"ORDER BY discount_pct DESC NULLS LAST "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def get_stats(conn) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    chains = conn.execute(
        "SELECT p.chain, COUNT(DISTINCT p.id) as cnt "
        "FROM products p GROUP BY p.chain"
    ).fetchall()
    stores = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    last_update = conn.execute(
        "SELECT MAX(scraped_at) FROM store_products"
    ).fetchone()[0]
    return {
        "total_products": total,
        "total_stores": stores,
        "chains": {r["chain"]: r["cnt"] for r in chains},
        "last_update": last_update,
    }
