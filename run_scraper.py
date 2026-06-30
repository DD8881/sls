#!/usr/bin/env python3
"""
Scrape promo products from supermarkets.

Usage:
    python run_scraper.py                     # Scrape all stores, all cities
    python run_scraper.py --city Київ         # Only stores in Kyiv
    python run_scraper.py --city Київ --chain silpo  # Only Silpo in Kyiv
    python run_scraper.py --workers 5         # Parallel scraping (per store)
"""
import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
from categories_map import (
    map_atb_category,
    map_auchan_category,
    map_fora_category,
    map_fozzy_category,
    map_metro_category,
    map_novus_category,
    map_silpo_category,
    map_varus_category,
)
from scrapers.registry import get_scrapers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def assign_unified_categories(conn):
    rows = conn.execute(
        "SELECT DISTINCT p.chain, p.category_slug, c.title, c.parent_slug "
        "FROM products p "
        "LEFT JOIN categories c ON c.id = p.chain || '::' || p.category_slug"
    ).fetchall()

    for r in rows:
        chain, slug, title, parent_slug = r["chain"], r["category_slug"], r["title"], r["parent_slug"]
        if chain == "silpo":
            unified = map_silpo_category(slug or "")
        elif chain == "varus":
            unified = map_varus_category(slug or "")
        elif chain == "atb":
            unified = map_atb_category(slug or "")
        elif chain == "fora":
            unified = map_fora_category(slug or "")
        elif chain == "auchan":
            unified = map_auchan_category(slug or "")
        elif chain == "fozzy":
            unified = map_fozzy_category(slug or "")
        elif chain == "metro":
            unified = map_metro_category(slug or "")
        elif chain == "novus":
            unified = map_novus_category(slug or "")
        else:
            unified = "other"

        conn.execute(
            "UPDATE products SET unified_category = ? WHERE chain = ? AND category_slug = ?",
            (unified, chain, slug),
        )


# Per-chain store-worker counts. Each chain targets its OWN host, so scraping
# all chains at once does NOT raise any single host's request rate above these
# numbers — only the machine-wide socket total rises, which is covered by the
# launchd soft fd limit of 8192 (com.sls.refresh.plist). Tuned per host:
#   silpo/fora — robust REST APIs; 10 store-workers x 5 page-workers ≈ 50 sockets.
#   varus      — fragile GraphQL with a 3-level fan-out (store x category x page);
#                workers=10 meant ~250 sockets and a storm of 500s with lost data.
#                Capped at 2 (well under the "≤5" ceiling) to stay reliable.
#   atb/auchan/metro/novus/fozzy — one national catalog, scraped once then cached;
#                extra store-workers just block on the cache lock, so keep low.
CHAIN_WORKERS = {
    "silpo": 10,
    "fora": 10,
    "varus": 2,
    "fozzy": 4,
    "atb": 2,
    "auchan": 2,
    "metro": 2,
    "novus": 2,
}
DEFAULT_WORKERS = 6


def scrape_store(scraper, store):
    """Scrape a single store. Returns (store, products) or (store, None) on error."""
    try:
        products = scraper.scrape_products(store)
        return store, products
    except Exception as e:
        log.error("[%s] Failed to scrape %s: %s", store.chain, store.name, e)
        return store, None


def run_chain(scraper, conn, db_lock, args):
    """Scrape one chain end to end.

    Runs in its own thread (one per chain). All DB writes go through db_lock so
    the shared connection keeps a single writer at any instant; network I/O for
    every chain overlaps freely in between.
    """
    chain = scraper.chain_name()

    try:
        categories = scraper.scrape_categories()
        with db_lock:
            db.upsert_categories(conn, categories)
            conn.commit()
        log.info("[%s] %d categories", chain, len(categories))
    except Exception:
        log.exception("[%s] Failed to fetch categories", chain)

    try:
        all_stores = scraper.get_stores()
    except Exception:
        log.exception("[%s] Failed to fetch stores", chain)
        return 0, 0

    stores = [s for s in all_stores if s.city]
    if args.city:
        stores = [s for s in stores if s.city == args.city]
    log.info("[%s] Scraping %d stores (of %d total)...", chain, len(stores), len(all_stores))

    with db_lock:
        for s in stores:
            db.upsert_store(conn, s.id, s.chain, s.name, s.city, s.address, s.lat, s.lng)
        conn.commit()

    workers = args.workers or CHAIN_WORKERS.get(chain, DEFAULT_WORKERS)
    total_products = 0
    total_stores = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{chain}-store") as pool:
        futures = {pool.submit(scrape_store, scraper, s): s for s in stores}
        for future in as_completed(futures):
            store, products = future.result()
            if not products:
                if products is not None:
                    log.info("[%s] %s: 0 products, skipping", chain, store.name)
                continue
            with db_lock:
                db.clear_store_products(conn, store.id)
                db.upsert_products(conn, products, store.id)
                conn.commit()
            total_products += len(products)
            total_stores += 1
            log.info("[%s] %s: %d products saved", chain, store.name, len(products))

    log.info("[%s] Done: %d products across %d stores", chain, total_products, total_stores)
    return total_products, total_stores


def main():
    parser = argparse.ArgumentParser(description="Scrape supermarket promos")
    parser.add_argument("--city", help="Filter stores by city (e.g. 'Київ')")
    parser.add_argument("--chain", help="Filter by chain (silpo/novus/metro/...)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Override store workers for every chain (default: per-chain tuned values)")
    args = parser.parse_args()

    db.init_db()
    conn = db.get_connection(check_same_thread=False)
    conn.execute("PRAGMA synchronous=NORMAL")  # safe under WAL; far fewer fsyncs over the run
    db_lock = threading.Lock()

    scrapers = [s for s in get_scrapers() if not args.chain or s.chain_name() == args.chain]

    # Chains hit independent hosts, so scrape them all concurrently: wall-clock
    # drops from sum(chain times) to ~max(chain time) with no extra load per host.
    with ThreadPoolExecutor(max_workers=max(len(scrapers), 1), thread_name_prefix="chain") as pool:
        futures = {pool.submit(run_chain, sc, conn, db_lock, args): sc.chain_name() for sc in scrapers}
        for future in as_completed(futures):
            chain = futures[future]
            try:
                future.result()
            except Exception:
                log.exception("[%s] chain crashed", chain)

    # All chain threads have joined here — finalisation is single-threaded.
    log.info("Assigning unified categories...")
    assign_unified_categories(conn)
    conn.commit()

    orphaned = db.cleanup_orphaned_products(conn)
    conn.commit()
    if orphaned:
        log.info("Cleaned up %d orphaned products", orphaned)

    stats = db.get_stats(conn)
    conn.close()
    log.info(
        "Done. %d products across %d stores. Chains: %s",
        stats["total_products"], stats["total_stores"], stats["chains"],
    )


if __name__ == "__main__":
    main()
