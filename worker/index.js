// Cloudflare Worker: single origin for the whole Mini App.
//
//   - GET  /, /static/*, /data/*  -> static assets (Mini App + generated JSON),
//                                    served by the [assets] binding. This replaces
//                                    web_server.py's static role.
//   - POST /api/feedback          -> verify Telegram WebApp initData (HMAC) and
//                                    forward the message to the feedback group.
//                                    Port of web_server.py:_verify_init_data + feedback().
//   - POST /webhook               -> Telegram bot webhook. The bot is now static:
//                                    /start opens the Mini App, /help prints help.
//                                    No DB at runtime (SQLite stays a build-time tool).
//
// Secrets (wrangler secret put): BOT_TOKEN, FEEDBACK_CHAT_ID, WEBHOOK_SECRET.
// Vars (wrangler.jsonc): WEBAPP_URL  -> this Worker's own public URL.

const TG_API = "https://api.telegram.org";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmac(keyData, msg) {
  const key = await crypto.subtle.importKey(
    "raw",
    keyData,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
}

// Validate Telegram WebApp initData (HMAC per the docs). Returns the parsed user
// object on success, or null if the signature is missing/invalid — so the
// endpoint can't be spammed from outside a real Telegram session.
async function verifyInitData(initData, botToken) {
  if (!initData || !botToken) return null;
  const params = new URLSearchParams(initData);
  const received = params.get("hash");
  if (!received) return null;
  params.delete("hash");

  const checkString = [...params.entries()]
    .map(([k, v]) => `${k}=${v}`)
    .sort()
    .join("\n");

  const enc = new TextEncoder();
  const secret = await hmac(enc.encode("WebAppData"), botToken);
  const calc = hex(await hmac(secret, checkString));

  // constant-time-ish compare
  if (calc.length !== received.length) return null;
  let diff = 0;
  for (let i = 0; i < calc.length; i++) diff |= calc.charCodeAt(i) ^ received.charCodeAt(i);
  if (diff !== 0) return null;

  try {
    return JSON.parse(params.get("user") || "{}");
  } catch {
    return {};
  }
}

async function handleFeedback(request, env) {
  if (!env.BOT_TOKEN || !env.FEEDBACK_CHAT_ID) {
    return json({ error: "feedback not configured" }, 503);
  }
  const data = await request.json().catch(() => ({}));
  const text = (data.text || "").trim().slice(0, 2000);
  if (!text) return json({ error: "empty" }, 400);

  const user = await verifyInitData(data.initData || "", env.BOT_TOKEN);
  if (user === null) return json({ error: "unauthorized" }, 401);

  let who = "невідомий";
  if (user && Object.keys(user).length) {
    const name = [user.first_name, user.last_name].filter(Boolean).join(" ") || "—";
    const uname = user.username ? ` @${user.username}` : "";
    who = `${name}${uname} (id ${user.id})`;
  }
  const msg = `💬 Новий відгук\n\n${text}\n\n👤 ${who}`;

  const resp = await fetch(`${TG_API}/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: env.FEEDBACK_CHAT_ID,
      text: msg,
      disable_web_page_preview: true,
    }),
  });
  if (!resp.ok) return json({ error: "send failed" }, 502);
  return json({ ok: true });
}

function sendMessage(env, chatId, text, extra = {}) {
  return fetch(`${TG_API}/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, ...extra }),
  });
}

// Telegram bot webhook. Static-only: no DB. Mirrors the surviving Python handlers
// (start/help); /search and /stats were dropped — search lives in the Mini App.
async function handleWebhook(request, env) {
  if (env.WEBHOOK_SECRET) {
    const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (got !== env.WEBHOOK_SECRET) return new Response("forbidden", { status: 403 });
  }
  const update = await request.json().catch(() => null);
  const message = update && update.message;
  const text = message && message.text;
  if (!message || !text) return json({ ok: true });

  const chatId = message.chat.id;
  const cmd = text.split(/\s+/)[0].split("@")[0];

  if (cmd === "/start") {
    if (env.WEBAPP_URL) {
      const first = (message.from && message.from.first_name) || "";
      const hi = first
        ? `👋 Вітаємо, ${first.replace(/[<>&]/g, "")}!`
        : "👋 Вітаємо!";
      await sendMessage(
        env,
        chatId,
        `${hi}\n\n` +
          "🛒 <b>Sales UA</b> — усі знижки супермаркетів України в одному застосунку.\n\n" +
          "📍 Ваше місто й найближчі магазини\n" +
          "🔍 Пошук товарів одразу за всіма категоріями\n\n" +
          "Тисніть кнопку нижче, щоб почати 👇",
        {
          parse_mode: "HTML",
          reply_markup: {
            inline_keyboard: [[{ text: "🛒 Відкрити знижки", web_app: { url: env.WEBAPP_URL } }]],
          },
        },
      );
    } else {
      await sendMessage(env, chatId, "Налаштуйте WEBAPP_URL для використання бота.");
    }
  } else if (cmd === "/help") {
    await sendMessage(
      env,
      chatId,
      "🛒 *Бот знижок у супермаркетах*\n\n" +
        "/start — відкрити застосунок\n" +
        "Пошук товарів — усередині застосунку.",
      { parse_mode: "Markdown" },
    );
  }
  return json({ ok: true });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/api/feedback" && request.method === "POST") {
      return handleFeedback(request, env);
    }
    if (url.pathname === "/webhook" && request.method === "POST") {
      return handleWebhook(request, env);
    }

    // Everything else -> static assets (Mini App shell + /static + /data),
    // served directly by the asset layer (the Worker isn't invoked for paths
    // that match a file). The shell's no-store header — so a bumped ?v= asset
    // URL is always picked up on reopen — comes from public/_headers, written
    // by deploy.sh, since this handler never runs for "/".
    return env.ASSETS.fetch(request);
  },
};
