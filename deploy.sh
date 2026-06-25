#!/usr/bin/env bash
# Build the static bundle and deploy the Worker to Cloudflare.
#
# Assumes discounts.db is already fresh (run the scrapers separately first:
#   python3 run_scraper.py --city ... --chain ...).
# Steps: regenerate JSON -> assemble ./public -> wrangler deploy.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Regenerating static JSON from discounts.db"
python3 generate_static.py

echo "==> Assembling ./public"
rm -rf public
mkdir -p public/static public/data
cp webapp/index.html public/index.html
cp webapp/app.js webapp/style.css webapp/logo.png public/static/
# Copy data, but skip the pre-gzipped twins — Cloudflare compresses on the fly.
rsync -a --exclude='*.gz' data/ public/data/

echo "==> Deploying to Cloudflare"
npx wrangler deploy

echo "==> Done."
