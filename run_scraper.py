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
import sys
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


def scrape_store(scraper, store):
    """Scrape a single store. Returns (store, products) or (store, error)."""
    try:
        products = scraper.scrape_products(store)
        return store, products
    except Exception as e:
        log.error("[%s] Failed to scrape %s: %s", store.chain, store.name, e)
        return store, None


def main():
    parser = argparse.ArgumentParser(description="Scrape supermarket promos")
    parser.add_argument("--city", help="Filter stores by city (e.g. 'Київ')")
    parser.add_argument("--chain", help="Filter by chain (silpo/novus/metro)")
    parser.add_argument("--workers", type=int, default=10, help="Parallel store workers (default: 10)")
    args = parser.parse_args()

    db.init_db()
    conn = db.get_connection()
    scrapers = get_scrapers()
    total_products = 0
    total_stores = 0

    for scraper in scrapers:
        chain = scraper.chain_name()
        if args.chain and chain != args.chain:
            continue

        try:
            categories = scraper.scrape_categories()
            log.info("[%s] %d categories", chain, len(categories))
            db.upsert_categories(conn, categories)
            conn.commit()
        except Exception:
            log.exception("[%s] Failed to fetch categories", chain)

        try:
            all_stores = scraper.get_stores()
        except Exception:
            log.exception("[%s] Failed to fetch stores", chain)
            continue

        stores = all_stores
        if args.city:
            stores = [s for s in all_stores if s.city == args.city]
        stores = [s for s in stores if s.city]

        log.info("[%s] Scraping %d stores (of %d total)...", chain, len(stores), len(all_stores))

        for s in stores:
            db.upsert_store(conn, s.id, s.chain, s.name, s.city, s.address, s.lat, s.lng)
        conn.commit()

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(scrape_store, scraper, s): s for s in stores}
            for future in as_completed(futures):
                store, products = future.result()
                if products is None:
                    continue
                if not products:
                    log.info("[%s] %s: 0 products, skipping", chain, store.name)
                    continue
                db.clear_store_products(conn, store.id)
                db.upsert_products(conn, products, store.id)
                conn.commit()
                total_products += len(products)
                total_stores += 1
                log.info("[%s] %s: %d products saved", chain, store.name, len(products))

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
