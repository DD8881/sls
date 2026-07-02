#!/usr/bin/env python3
"""Автопостер денних знижок у Buffer → Instagram Reel / TikTok / Threads.

Standalone (без Claude/MCP) — придатний для launchd. Читає згенерований
promo.py файл best-sales-images/<дата>/caption.json і публікує через Buffer
GraphQL API (https://api.buffer.com), автопублікація одразу (shareNow).

Токен: BUFFER_TOKEN у .env (створити на publish.buffer.com/settings/api,
Bearer). Формати: IG=Reel(video), TikTok=video, Threads=hook→payoff тред;
якщо ролика нема (--no-reel) — фолбек на фото (IG-карусель, TikTok-фото).

    ./.venv/bin/python post.py                 # сьогодні, усі канали, live
    ./.venv/bin/python post.py --dry-run       # показати payload, не постити
    ./.venv/bin/python post.py --only threads  # лише один канал
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.buffer.com"
TOKEN = os.environ.get("BUFFER_TOKEN")

# Канали Buffer (стабільні; якщо переконектиш канал у Buffer — онови ID через
# list_channels). Org "My Organization".
CHANNELS = {
    "instagram": "6a4519e35ab6d2f106915b70",
    "threads":   "6a451ba85ab6d2f106916179",
    "tiktok":    "6a4513e25ab6d2f1069140fb",
}

MUTATION = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on MutationError { message }
  }
}
"""


def _base(cap: dict) -> dict:
    return {"schedulingType": "automatic", "mode": "shareNow"}


def build_inputs(cap: dict, only: str | None) -> dict:
    """Складає GraphQL-input під кожен канал з caption.json."""
    img = cap["images"]
    reel = img.get("reel")
    out = {}

    # Instagram: Reel (video) або фолбек-карусель (2 фото).
    ig = {**_base(cap), "channelId": CHANNELS["instagram"], "text": cap["instagram"]["text"]}
    if reel:
        ig["assets"] = [{"video": {"url": reel}}]
        ig["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
    else:
        ig["assets"] = [{"image": {"url": img["pct"]}}, {"image": {"url": img["save"]}}]
        ig["metadata"] = {"instagram": {"type": "post", "shouldShareToFeed": True}}
    out["instagram"] = ig

    # TikTok: video або фолбек-фото.
    tt = {**_base(cap), "channelId": CHANNELS["tiktok"], "text": cap["tiktok"]["text"],
          "metadata": {"tiktok": {"title": cap["tiktok"]["title"]}}}
    tt["assets"] = [{"video": {"url": reel}}] if reel else [{"image": {"url": img["pct"]}}]
    out["tiktok"] = tt

    # Threads: hook → payoff тред (текст + банер на reply).
    th = cap["threads"]
    out["threads"] = {
        **_base(cap), "channelId": CHANNELS["threads"], "text": th["post1"],
        "metadata": {"threads": {"type": "post", "thread": [
            {"text": th["post1"]},
            {"text": th["post2"], "assets": [{"image": {"url": img["pct"]}}]},
        ]}},
    }

    return {only: out[only]} if only else out


def create_post(inp: dict) -> tuple[bool, object]:
    r = requests.post(API, timeout=60, json={"query": MUTATION, "variables": {"input": inp}},
                      headers={"Authorization": f"Bearer {TOKEN}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        return False, data["errors"]
    res = data["data"]["createPost"]
    if res.get("__typename") == "PostActionSuccess":
        return True, res["post"]
    return False, res.get("message", res)


def main():
    ap = argparse.ArgumentParser(description="Автопостер знижок дня в Buffer")
    ap.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    ap.add_argument("--caption", default=None, help="шлях до caption.json (перекриває --date)")
    ap.add_argument("--only", choices=list(CHANNELS), help="постити лише один канал")
    ap.add_argument("--dry-run", action="store_true", help="показати payload, не постити")
    args = ap.parse_args()

    path = args.caption or os.path.join("best-sales-images", args.date, "caption.json")
    if not os.path.exists(path):
        print(f"нема caption.json: {path} (спершу promo.py)", file=sys.stderr)
        sys.exit(1)
    cap = json.load(open(path, encoding="utf-8"))
    inputs = build_inputs(cap, args.only)

    if args.dry_run:
        print(json.dumps(inputs, ensure_ascii=False, indent=2))
        return
    if not TOKEN:
        print("нема BUFFER_TOKEN у .env (publish.buffer.com/settings/api)", file=sys.stderr)
        sys.exit(2)

    failed = 0
    for name, inp in inputs.items():
        try:
            ok, info = create_post(inp)
        except Exception as e:
            ok, info = False, repr(e)
        if ok:
            print(f"✓ {name}: {info.get('id')} ({info.get('status')})")
        else:
            failed += 1
            print(f"✗ {name}: {info}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
