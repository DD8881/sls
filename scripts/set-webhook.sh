#!/usr/bin/env bash
# One-time: point the Telegram bot at the Worker webhook.
# Run after the first `wrangler deploy` (and whenever the URL or secret changes).
#
# Reads BOT_TOKEN from .env. Pass the Worker base URL + the webhook secret you
# also set via `wrangler secret put WEBHOOK_SECRET`:
#
#   ./scripts/set-webhook.sh https://sls.<sub>.workers.dev <WEBHOOK_SECRET>
#
# Switching to a webhook DISABLES polling — don't run run_bot.py against the same
# token afterwards (delete the webhook first: .../deleteWebhook).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] && set -a && . ./.env && set +a
: "${BOT_TOKEN:?BOT_TOKEN not set (in .env)}"

BASE_URL="${1:?usage: set-webhook.sh <worker-base-url> <webhook-secret>}"
SECRET="${2:?usage: set-webhook.sh <worker-base-url> <webhook-secret>}"

curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${BASE_URL%/}/webhook" \
  --data-urlencode "secret_token=${SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]'
echo
