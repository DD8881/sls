import logging
import os
from flask import Flask, send_from_directory, request, abort

app = Flask(__name__, static_folder="webapp", static_url_path="/static")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@app.route("/")
def index():
    return send_from_directory("webapp", "index.html")


@app.route("/data/<path:filepath>")
def serve_data(filepath):
    return send_from_directory(DATA_DIR, filepath)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
