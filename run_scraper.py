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
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
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


# Per-chain store-worker counts. A test run with every chain live showed the
# real limit is AGGREGATE, not per-host: silpo (plain GET) handled 10x5≈50
# sockets fine, but fora and varus (POST/GraphQL) started dropping connections
# (BrokenPipe / reset / SSL-EOF) once the machine ran ~150-200 sockets at once.
# Concurrency is now bounded two ways:
#   1. scrapers.http.REQUEST_GATE caps total in-flight requests across ALL chains
#      (HTTP_MAX_CONCURRENCY, default 48) — the systemic fix for the aggregate.
#   2. these per-chain caps keep the two fragile hosts gentle on their own:
#        fora  — 4 store x 3 page  ≈ 12 concurrent (was 10x5=50 → broke).
#        varus — 2 store x 3 cat x 3 page ≈ 18 (was effectively 10x5x5=250).
#   silpo stays at 10 (proven fine). National-catalog chains scrape once then
#   cache, so extra store-workers just wait on the cache lock — keep low.
CHAIN_WORKERS = {
    "silpo": 10,
    "fora": 4,
    "varus": 2,
    "fozzy": 4,
    "atb": 2,
    "auchan": 2,
    "metro": 2,
    "novus": 2,
}
DEFAULT_WORKERS = 6
# Stores that errored are re-scraped once at the end with low concurrency — by
# then most chains are done, so the calmer conditions recover transient drops.
RETRY_WORKERS = 3


def scrape_store(scraper, store):
    """Scrape a single store. Returns (store, products) or (store, None) on error."""
    try:
        products = scraper.scrape_products(store)
        return store, products
    except Exception as e:
        log.error("[%s] Failed to scrape %s: %s", store.chain, store.name, e)
        return store, None


def run_chain(scraper, conn, db_lock, args, fresh, deadline):
    """Scrape one chain end to end.

    Runs in its own thread (one per chain). All DB writes go through db_lock so
    the shared connection keeps a single writer at any instant; network I/O for
    every chain overlaps freely in between. ``fresh`` is True for a from-scratch
    build (empty DB) — then per-store clears are unnecessary. ``deadline`` is a
    monotonic() cap: past it the chain stops with partial data (a hard backstop
    against a bad-host day dragging the run for hours; the circuit breaker in
    scrapers/http.py normally makes such a chain fail fast well before this).
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
        return 0, 0, []

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
    failed: list = []  # stores that errored (returned None) — retried later in a calm pass
    # Per-chain {external_id: product_id}: promo products repeat across a chain's
    # branches, so this lets the writer skip re-upserting the same product per store.
    id_cache: dict = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{chain}-store") as pool:
        futures = {pool.submit(scrape_store, scraper, s): s for s in stores}
        for future in as_completed(futures):
            if time.monotonic() > deadline:
                log.warning("[%s] time budget exceeded — stopping with %d stores saved (partial)",
                            chain, total_stores)
                break
            store, products = future.result()
            if not products:
                if products is None:
                    failed.append(store)  # error, not a legit empty store
                else:
                    log.info("[%s] %s: 0 products, skipping", chain, store.name)
                continue
            with db_lock:
                if not fresh:
                    db.clear_store_products(conn, store.id)
                db.upsert_products(conn, products, store.id, id_cache)
                conn.commit()
            total_products += len(products)
            total_stores += 1
            log.info("[%s] %s: %d products saved", chain, store.name, len(products))

    if failed:
        log.info("[%s] %d store(s) failed — queued for retry pass", chain, len(failed))
    log.info("[%s] Done: %d products across %d stores", chain, total_products, total_stores)
    return total_products, total_stores, failed


def _existing_product_total(db_path: str) -> int | None:
    """Product count in the current live DB, or None if there isn't one yet."""
    if not os.path.exists(db_path):
        return None
    try:
        c = sqlite3.connect(db_path)
        try:
            return c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _swap_into_place(build_path: str, db_path: str, new_total: int, prev_total: int | None):
    """Atomically replace db_path with the freshly built DB, keeping a .bak.

    Refuses to swap if the fresh build looks broken (zero products, or a >50%%
    drop vs the previous DB) so a failed/partial scrape can't wipe good data.
    """
    if new_total == 0:
        log.error("Fresh build has 0 products — NOT swapping. Keeping %s; build left at %s",
                  db_path, build_path)
        return
    if prev_total and new_total < prev_total * 0.5:
        log.error("Fresh build has %d products vs %d before (<50%%) — NOT swapping. "
                  "Keeping %s; inspect %s", new_total, prev_total, db_path, build_path)
        return
    if os.path.exists(db_path):
        os.replace(db_path, db_path + ".bak")
    os.replace(build_path, db_path)
    log.info("Swapped fresh DB into %s (previous kept as %s.bak)", db_path, db_path)


def main():
    parser = argparse.ArgumentParser(description="Scrape supermarket promos")
    parser.add_argument("--city", help="Filter stores by city (e.g. 'Київ')")
    parser.add_argument("--chain", help="Filter by chain (silpo/novus/metro/...)")
    parser.add_argument("--workers", type=int, default=None,
                        help="Override store workers for every chain (default: per-chain tuned values)")
    args = parser.parse_args()
    # Wall-clock BACKSTOP on the whole scrape (env SCRAPE_BUDGET_MIN, default 120).
    # A healthy run finishes in ~30-35 min; a slow-but-progressing day (e.g. poor
    # local internet) is allowed to run on and complete rather than be cut off
    # with partial data. Past the cap chains stop with whatever they have — this
    # only guards a truly stuck run, not normal slowness (the per-host circuit
    # breaker already handles a genuinely dead host).
    deadline = time.monotonic() + int(os.getenv("SCRAPE_BUDGET_MIN", "120")) * 60

    # Full run -> build a fresh DB off to the side and atomically swap it in.
    # Nothing reads the DB during a scrape, so this is faster (disposable file:
    # no fsync, indexes built once at the end) AND safer (production is untouched
    # until the swap; a crash leaves yesterday's data intact). Partial runs
    # (--chain/--city) must NOT swap — they'd wipe the data they didn't scrape —
    # so they update the live DB in place as before.
    full_run = not (args.chain or args.city)
    db_path = config.DATABASE_PATH

    if full_run:
        build_path = db_path + ".tmp"
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(build_path + suffix)
            except FileNotFoundError:
                pass
        prev_total = _existing_product_total(db_path)
        conn = db.get_build_connection(build_path)
        db.create_tables(conn)
        log.info("Full run: building fresh DB at %s (swap into %s at end)", build_path, db_path)
    else:
        db.init_db()
        conn = db.get_connection(check_same_thread=False)
        conn.execute("PRAGMA synchronous=NORMAL")  # safe under WAL; far fewer fsyncs
        build_path, prev_total = None, None
        log.info("Partial run: updating %s in place", db_path)

    db_lock = threading.Lock()
    scrapers = [s for s in get_scrapers() if not args.chain or s.chain_name() == args.chain]

    # Chains hit independent hosts, so scrape them all concurrently: wall-clock
    # drops from sum(chain times) to ~max(chain time) with no extra load per host.
    failed_stores = []  # (scraper, store) pairs that errored — retried below
    with ThreadPoolExecutor(max_workers=max(len(scrapers), 1), thread_name_prefix="chain") as pool:
        futures = {pool.submit(run_chain, sc, conn, db_lock, args, full_run, deadline): sc for sc in scrapers}
        for future in as_completed(futures):
            sc = futures[future]
            try:
                _tp, _ts, failed = future.result()
                failed_stores.extend((sc, st) for st in failed)
            except Exception:
                log.exception("[%s] chain crashed", sc.chain_name())

    # Retry pass: re-scrape stores that errored, now that load has dropped (most
    # chains finished). Low concurrency on purpose — they failed under burst, so a
    # calm pass recovers the transient ones. Runs before finalisation so recovered
    # data is indexed and swapped in.
    if failed_stores and time.monotonic() < deadline:
        log.info("Retry pass: re-scraping %d store(s) that failed...", len(failed_stores))
        recovered = 0
        retry_caches: dict = {}
        with ThreadPoolExecutor(max_workers=RETRY_WORKERS, thread_name_prefix="retry") as pool:
            futures = {pool.submit(scrape_store, sc, st): st for sc, st in failed_stores}
            for future in as_completed(futures):
                store, products = future.result()
                if not products:
                    continue
                cache = retry_caches.setdefault(store.chain, {})
                with db_lock:
                    if not full_run:
                        db.clear_store_products(conn, store.id)
                    db.upsert_products(conn, products, store.id, cache)
                    conn.commit()
                recovered += 1
                log.info("[retry] %s %s: %d products recovered", store.chain, store.name, len(products))
        log.info("Retry pass: recovered %d of %d failed store(s)", recovered, len(failed_stores))
    elif failed_stores:
        log.warning("Retry pass skipped (time budget exceeded) — %d store(s) left for next run",
                    len(failed_stores))

    # All scraping done — finalisation is single-threaded.
    log.info("Assigning unified categories...")
    assign_unified_categories(conn)
    conn.commit()

    orphaned = db.cleanup_orphaned_products(conn)
    conn.commit()
    if orphaned:
        log.info("Cleaned up %d orphaned products", orphaned)

    if full_run:
        log.info("Creating indexes...")
        db.create_indexes(conn)
        conn.commit()

    stats = db.get_stats(conn)
    conn.close()
    log.info(
        "Done. %d products across %d stores. Chains: %s",
        stats["total_products"], stats["total_stores"], stats["chains"],
    )

    if full_run:
        _swap_into_place(build_path, db_path, stats["total_products"], prev_total)


if __name__ == "__main__":
    main()
