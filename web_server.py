import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qsl

import requests
from flask import Flask, send_from_directory, request, make_response, jsonify

import config

app = Flask(__name__, static_folder="webapp", static_url_path="/static")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@app.route("/")
def index():
    # Never let the Telegram webview cache the shell, so bumped ?v= asset URLs
    # (and any fix in them) are always picked up on reopen.
    resp = make_response(send_from_directory("webapp", "index.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/data/<path:filepath>")
def serve_data(filepath):
    # NOTE: previously served the precompressed .gz twin with Content-Encoding:
    # gzip, but the iOS Telegram webview did not transparently decompress fetch()
    # responses, so res.json() choked on raw gzip bytes and all data loads failed.
    # Serve plain JSON; revisit on-the-fly compression at the proxy if needed.
    return send_from_directory(DATA_DIR, filepath)


def _verify_init_data(init_data: str):
    """Validate Telegram WebApp initData (HMAC per the docs). Returns the parsed
    user dict on success, or None if the signature is missing/invalid — so the
    endpoint can't be spammed from outside a real Telegram session."""
    if not init_data or not config.BOT_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return {}


@app.route("/api/feedback", methods=["POST"])
def feedback():
    if not config.BOT_TOKEN or not config.FEEDBACK_CHAT_ID:
        return jsonify(error="feedback not configured"), 503
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:2000]
    if not text:
        return jsonify(error="empty"), 400
    user = _verify_init_data(data.get("initData", ""))
    if user is None:
        return jsonify(error="unauthorized"), 401

    who = "невідомий"
    if user:
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "—"
        uname = f" @{user['username']}" if user.get("username") else ""
        who = f"{name}{uname} (id {user.get('id')})"
    msg = f"💬 Новий відгук\n\n{text}\n\n👤 {who}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            json={"chat_id": config.FEEDBACK_CHAT_ID, "text": msg, "disable_web_page_preview": True},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logging.error("feedback send failed: %s", exc)
        return jsonify(error="send failed"), 502
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
