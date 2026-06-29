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

// Thin wrapper over the Bot API. Returns the parsed JSON ({ok, result}|{ok:false,...}).
async function tg(env, method, body) {
  const resp = await fetch(`${TG_API}/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json().catch(() => ({ ok: false }));
}

function sendMessage(env, chatId, text, extra = {}) {
  return tg(env, "sendMessage", { chat_id: chatId, text, ...extra });
}

// Forced-subscription gate. Returns true if the user is a member of env.CHANNEL.
//
// Requires the bot to be an ADMINISTRATOR of the channel (otherwise getChatMember
// errors and we fail OPEN — a misconfigured/absent gate must never lock everyone
// out of the bot). If env.CHANNEL is unset the gate is disabled (returns true).
async function isSubscribed(env, userId) {
  if (!env.CHANNEL) return true; // gate disabled
  const r = await tg(env, "getChatMember", { chat_id: env.CHANNEL, user_id: userId });
  if (!r || !r.ok || !r.result) return true; // can't verify -> fail open
  const status = r.result.status;
  if (status === "creator" || status === "administrator" || status === "member") return true;
  if (status === "restricted") return r.result.is_member === true;
  return false; // left | kicked
}

// Public t.me link to the channel: explicit CHANNEL_INVITE wins (needed for
// private channels), else derive from an @username CHANNEL.
function channelUrl(env) {
  if (env.CHANNEL_INVITE) return env.CHANNEL_INVITE;
  if (env.CHANNEL && env.CHANNEL.startsWith("@")) return `https://t.me/${env.CHANNEL.slice(1)}`;
  return null;
}

function openWebappMessage(env, firstName) {
  const first = (firstName || "").replace(/[<>&]/g, "");
  const hi = first ? `👋 Вітаємо, ${first}!` : "👋 Вітаємо!";
  return {
    text:
      `${hi}\n\n` +
      "🛒 <b>Sales UA</b> — усі знижки супермаркетів України в одному застосунку.\n\n" +
      "📍 Ваше місто й найближчі магазини\n" +
      "🔍 Пошук товарів одразу за всіма категоріями\n\n" +
      "Тисніть кнопку нижче, щоб почати 👇",
    extra: {
      parse_mode: "HTML",
      reply_markup: {
        inline_keyboard: [[{ text: "🛒 Відкрити знижки", web_app: { url: env.WEBAPP_URL } }]],
      },
    },
  };
}

// Prompt shown to users who haven't joined the channel yet: a link to the
// channel + an inline "I subscribed" button that re-checks via callback_query.
function subscribePrompt(env) {
  const url = channelUrl(env);
  const rows = [];
  if (url) rows.push([{ text: "📢 Підписатися на канал", url }]);
  rows.push([{ text: "✅ Я підписався", callback_data: "check_sub" }]);
  return {
    text:
      "🔒 <b>Майже готово!</b>\n\n" +
      "Щоб користуватися застосунком, спершу підпишіться на наш канал — " +
      "там анонси найкращих знижок.\n\n" +
      "Після підписки тисніть «✅ Я підписався».",
    extra: { parse_mode: "HTML", reply_markup: { inline_keyboard: rows } },
  };
}

// Telegram bot webhook. Static-only: no DB. Mirrors the surviving Python handlers
// (start/help); /search and /stats were dropped — search lives in the Mini App.
async function handleWebhook(request, env) {
  if (env.WEBHOOK_SECRET) {
    const got = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (got !== env.WEBHOOK_SECRET) return new Response("forbidden", { status: 403 });
  }
  const update = await request.json().catch(() => null);
  if (!update) return json({ ok: true });

  // Inline "✅ Я підписався" button -> re-check membership.
  if (update.callback_query) {
    const cq = update.callback_query;
    if (cq.data === "check_sub") {
      const userId = cq.from && cq.from.id;
      const msg = cq.message;
      if (await isSubscribed(env, userId)) {
        await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
        if (msg && env.WEBAPP_URL) {
          const m = openWebappMessage(env, cq.from && cq.from.first_name);
          await tg(env, "editMessageText", {
            chat_id: msg.chat.id,
            message_id: msg.message_id,
            text: m.text,
            ...m.extra,
          });
        }
      } else {
        await tg(env, "answerCallbackQuery", {
          callback_query_id: cq.id,
          text: "Ще не бачу підписки 🙈 Підпишіться на канал і спробуйте ще раз.",
          show_alert: true,
        });
      }
    } else {
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
    }
    return json({ ok: true });
  }

  const message = update.message;
  const text = message && message.text;
  if (!message || !text) return json({ ok: true });

  const chatId = message.chat.id;
  const cmd = text.split(/\s+/)[0].split("@")[0];

  if (cmd === "/start") {
    if (!env.WEBAPP_URL) {
      await sendMessage(env, chatId, "Налаштуйте WEBAPP_URL для використання бота.");
    } else if (await isSubscribed(env, message.from && message.from.id)) {
      const m = openWebappMessage(env, message.from && message.from.first_name);
      await sendMessage(env, chatId, m.text, m.extra);
    } else {
      const p = subscribePrompt(env);
      await sendMessage(env, chatId, p.text, p.extra);
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
