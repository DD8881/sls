#!/usr/bin/env python3
"""
Generate static JSON files from SQLite for the frontend.

Loads all data into memory first (few big SQL queries), then generates
JSON files purely from Python dicts — no DB access during city generation.

Output structure:
  data/
    cities.json                 # [{city, store_cnt, product_cnt}]
    Київ/
      index.json                # {chains, categories, stores}
      dairy.json                # {products: [...]}
      dairy_stores.json         # {product_id: [store_ids...]}
      ...
"""
import gzip
import json
import logging
import os
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
from categories_map import UNIFIED, UNIFIED_DICT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# Units that are just a count/packaging label, not a volume/weight worth showing.
_COUNT_UNITS = {"шт", "шт.", "уп", "уп.", "пак", "пак.", "компл", "компл."}


def _norm_unit(s: str) -> str:
    return s.lower().replace(",", ".").replace(" ", "")


def _title_with_unit(title: str | None, unit: str | None) -> str:
    """Append a volume/weight unit to the title when it isn't already there.

    Fora/Fozzy keep the bottle volume in a separate `unit` field ("0,7л", "350г",
    "кг"); other chains bake it into the title. We surface it for the former and
    skip bare count units ("шт") and anything already present in the title.
    """
    title = title or ""
    if not unit:
        return title
    u = unit.strip()
    if not u or u.lower() in _COUNT_UNITS:
        return title
    if _norm_unit(u) in _norm_unit(title):
        return title
    return f"{title} {u}".strip()

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")

CITY_WORKERS = 6


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    with gzip.open(path + ".gz", "wt", encoding="utf-8") as f:
        f.write(raw)
    return len(raw)


def _load_all():
    log.info("Loading all data into memory...")
    conn = db.get_connection()

    stores = {}
    for r in conn.execute("SELECT * FROM stores WHERE city IS NOT NULL AND city != ''").fetchall():
        stores[r["id"]] = dict(r)

    products = {}
    for r in conn.execute("SELECT * FROM products").fetchall():
        products[r["id"]] = dict(r)

    sp_by_store = defaultdict(list)
    for r in conn.execute(
        "SELECT product_id, store_id, price, old_price, discount_pct, promo_end_date "
        "FROM store_products"
    ).fetchall():
        sp_by_store[r["store_id"]].append(dict(r))

    slug_to_title = {}
    for r in conn.execute("SELECT chain, slug, title FROM categories").fetchall():
        slug_to_title[(r["chain"], r["slug"])] = r["title"]

    conn.close()

    city_stores = defaultdict(list)
    for s in stores.values():
        city_stores[s["city"]].append(s)

    log.info("Loaded: %d stores, %d products, %d store_product links",
             len(stores), len(products), sum(len(v) for v in sp_by_store.values()))

    return products, city_stores, sp_by_store, slug_to_title


def _generate_city(city, city_store_list, products, sp_by_store, slug_to_title):
    city_dir = os.path.join(OUTPUT_DIR, city)
    store_ids = {s["id"] for s in city_store_list}

    stores_map = {}
    for s in city_store_list:
        entry = {
            "chain": s["chain"],
            "name": s["name"],
            "addr": s["address"] or "",
        }
        if s.get("lat") is not None and s.get("lng") is not None:
            entry["lat"] = s["lat"]
            entry["lng"] = s["lng"]
        stores_map[s["id"]] = entry

    product_entries = defaultdict(list)
    for sid in store_ids:
        for sp in sp_by_store.get(sid, []):
            product_entries[sp["product_id"]].append({**sp, "store_id": sid})

    cat_products = defaultdict(list)
    for pid, sp_list in product_entries.items():
        p = products.get(pid)
        if not p or not p.get("unified_category"):
            continue

        best_price = min(sp["price"] for sp in sp_list)
        max_discount = max((sp["discount_pct"] or 0) for sp in sp_list)
        old_price = next((sp["old_price"] for sp in sp_list if sp.get("old_price")), None)
        promo_end = next((sp["promo_end_date"] for sp in sp_list if sp.get("promo_end_date")), None)
        sub = slug_to_title.get((p["chain"], p.get("category_slug") or ""), "")

        item = {
            "id": pid,
            "t": _title_with_unit(p["title"], p.get("unit")),
            "p": best_price,
            "ch": p["chain"],
            "sc": len(sp_list),
        }
        if sub:
            item["sub"] = sub
        if old_price:
            item["op"] = old_price
        if max_discount:
            item["d"] = round(max_discount)
        if p.get("image_url"):
            item["img"] = p["image_url"]
        if p.get("url"):
            item["url"] = p["url"]
        if promo_end:
            item["end"] = promo_end

        availability = []
        for sp in sorted(sp_list, key=lambda x: x["price"]):
            entry = {"s": sp["store_id"]}
            if sp["price"] != best_price:
                entry["p"] = sp["price"]
            availability.append(entry)

        cat_products[p["unified_category"]].append((item, availability))

    categories = []
    for ucat, items in cat_products.items():
        categories.append({
            "slug": ucat,
            "title": UNIFIED_DICT.get(ucat, ucat),
            "cnt": len(items),
        })
    categories.sort(key=lambda x: -x["cnt"])

    write_json(os.path.join(city_dir, "index.json"), {
        "city": city,
        "stores": stores_map,
        "categories": categories,
    })

    total_products = 0
    for cat_slug, _ in UNIFIED:
        items_data = cat_products.get(cat_slug)
        if not items_data:
            continue

        items_data.sort(key=lambda x: -(x[0].get("d", 0) or 0))

        products_compact = [item for item, _ in items_data]

        subcat_counts = {}
        for item, _ in items_data:
            sub = item.get("sub", "")
            if sub:
                subcat_counts[sub] = subcat_counts.get(sub, 0) + 1
        subcategories = sorted(
            [{"title": t, "cnt": c} for t, c in subcat_counts.items()],
            key=lambda x: -x["cnt"],
        )

        write_json(
            os.path.join(city_dir, f"{cat_slug}.json"),
            {"products": products_compact, "subcategories": subcategories},
        )

        avail_map = {}
        for item, avail in items_data:
            avail_map[str(item["id"])] = avail

        write_json(
            os.path.join(city_dir, f"{cat_slug}_stores.json"),
            avail_map,
        )

        total_products += len(products_compact)

    log.info("Generated %s/: %d products, %d stores", city, total_products, len(stores_map))
    return city, total_products


def generate():
    products, city_stores, sp_by_store, slug_to_title = _load_all()

    cities = sorted(city_stores.keys(), key=lambda c: -len(city_stores[c]))
    cities_index = []
    for city in cities:
        store_ids = {s["id"] for s in city_stores[city]}
        product_ids = set()
        for sid in store_ids:
            for sp in sp_by_store.get(sid, []):
                product_ids.add(sp["product_id"])
        cities_index.append({
            "city": city,
            "store_cnt": len(store_ids),
            "product_cnt": len(product_ids),
        })

    write_json(os.path.join(OUTPUT_DIR, "cities.json"), cities_index)
    log.info("Generated cities.json (%d cities)", len(cities))

    with ThreadPoolExecutor(max_workers=CITY_WORKERS) as pool:
        futures = {
            pool.submit(
                _generate_city, city, city_stores[city],
                products, sp_by_store, slug_to_title,
            ): city
            for city in cities
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                log.exception("Failed to generate %s", futures[future])

    log.info("Done. Output: %s", OUTPUT_DIR)


if __name__ == "__main__":
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    generate()
