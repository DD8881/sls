# Deployment (Cloudflare Worker)

The whole app runs as **one free Cloudflare Worker**:

- **Static Mini App + data** (`/`, `/static/*`, `/data/*`) — unlimited bandwidth.
- **`/api/feedback`** — verifies Telegram WebApp `initData` (HMAC) and forwards
  feedback to the group. Replaces `web_server.py` in production.
- **`/webhook`** — the Telegram bot. Static-only: `/start` opens the Mini App.

SQLite (`discounts.db`) is now a **build-time tool only** — scrape → DB →
`generate_static.py` → JSON. Nothing queries the DB at runtime. Search happens
client-side in the Mini App (`webapp/app.js`, `search.json`).

## One-time setup

1. Cloudflare account + Wrangler:
   ```bash
   npm i -g wrangler
   wrangler login
   ```
2. Set `WEBAPP_URL` in `wrangler.jsonc` to your Worker URL (you'll know it after
   the first deploy — `https://sls.<subdomain>.workers.dev/`).
3. Secrets:
   ```bash
   wrangler secret put BOT_TOKEN
   wrangler secret put FEEDBACK_CHAT_ID
   wrangler secret put WEBHOOK_SECRET   # any long random string
   ```

## Deploy (each data refresh)

Run the scrapers first (separately), then:
```bash
./deploy.sh
```
This regenerates JSON, assembles `./public`, and runs `wrangler deploy`.

## Point the bot at the Worker (one-time, and after URL/secret changes)

```bash
./scripts/set-webhook.sh https://sls.<subdomain>.workers.dev <WEBHOOK_SECRET>
```
> Webhook mode disables polling — don't run `run_bot.py` against the same token
> afterwards. To go back: `curl .../bot<TOKEN>/deleteWebhook`.

## Automating refresh (local, free)

Schedule `deploy.sh` via `launchd` (macOS) so it runs daily after the scrapers.
Use `caffeinate` to keep the Mac awake for the run.

## Local dev

- `wrangler dev` — runs the Worker locally (closest to production).
- `python3 web_server.py` — Flask alternative for the static + feedback parts.
- `python3 run_bot.py` — polling bot for local testing (delete the webhook first).
