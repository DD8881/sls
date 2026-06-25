import logging
import os
from flask import Flask, send_from_directory, request, make_response

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
