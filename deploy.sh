#!/usr/bin/env bash
# Build the static bundle and deploy the Worker to Cloudflare.
#
# Assumes discounts.db is already fresh (run the scrapers separately first:
#   python3 run_scraper.py --city ... --chain ...).
# Steps: regenerate JSON -> assemble ./public -> wrangler deploy.
set -euo pipefail
cd "$(dirname "$0")"

# Prefer the project venv (Python 3.11; generate_static.py needs 3.10+ for
# `str | None`). Falls back to python3 so launchd/cron work without activation.
PYTHON="./.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

echo "==> Regenerating static JSON from discounts.db ($PYTHON)"
"$PYTHON" generate_static.py

echo "==> Assembling ./public"
rm -rf public
mkdir -p public/static public/data
cp webapp/index.html public/index.html
cp webapp/app.js webapp/analytics.js webapp/style.css webapp/logo.png public/static/
# Copy data, but skip the pre-gzipped twins — Cloudflare compresses on the fly.
rsync -a --exclude='*.gz' data/ public/data/
# Never cache the Mini App shell, so a bumped ?v= asset URL is picked up on
# reopen in the Telegram webview. Assets are served bypassing the Worker, so
# this header must come from the asset layer's _headers file, not worker code.
printf '/\n  Cache-Control: no-store\n/index.html\n  Cache-Control: no-store\n' > public/_headers

echo "==> Deploying to Cloudflare"
npx wrangler deploy

echo "==> Done."
