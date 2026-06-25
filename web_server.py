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
    # The generator writes a gzipped twin (foo.json + foo.json.gz). Data files run
    # to several MB uncompressed (the city search index is ~13MB), which stalls on
    # mobile; serve the precompressed copy when the client accepts gzip.
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
    if accepts_gzip and os.path.exists(os.path.join(DATA_DIR, filepath + ".gz")):
        resp = make_response(send_from_directory(DATA_DIR, filepath + ".gz"))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Vary"] = "Accept-Encoding"
        return resp
    return send_from_directory(DATA_DIR, filepath)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
