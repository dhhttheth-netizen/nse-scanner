# -*- coding: utf-8 -*-
from flask import Flask, render_template, jsonify
from datetime import datetime
import threading
import time
import traceback

import scanner

app = Flask(__name__)

# ── Shared cache, updated by a background thread ────────────────────────
_cache = {
    "data": {
        "generated_at": None,
        "trade_date": None,
        "phase": "STARTING",
        "positions": [],
        "total_pnl_rs": 0.0,
        "winners": 0,
        "losers": 0,
        "message": "Warming up — first scan in progress, check back in a minute.",
    },
    "last_run_started": None,
    "last_run_finished": None,
    "last_error": None,
}
_cache_lock = threading.Lock()

SCAN_INTERVAL_SECONDS = 120   # how often the background thread rescans


def background_scanner():
    """Runs forever in a background thread, refreshing the cache periodically.
    This NEVER blocks the Flask request/response cycle or the health check."""
    while True:
        try:
            with _cache_lock:
                _cache["last_run_started"] = datetime.utcnow().isoformat()

            result = scanner.run_scan()

            with _cache_lock:
                _cache["data"] = result
                _cache["last_run_finished"] = datetime.utcnow().isoformat()
                _cache["last_error"] = None

        except Exception as e:
            err_text = f"{e}\n{traceback.format_exc()}"
            with _cache_lock:
                _cache["last_error"] = err_text
            print(f"[SCAN ERROR] {err_text}", flush=True)

        time.sleep(SCAN_INTERVAL_SECONDS)


# Start the background thread once, when the app process boots.
# use_reloader is off in production (gunicorn), so this runs exactly once.
_scanner_thread = threading.Thread(target=background_scanner, daemon=True)
_scanner_thread.start()


@app.route("/")
def dashboard():
    with _cache_lock:
        data = _cache["data"]
        last_error = _cache["last_error"]
    return render_template("dashboard.html", data=data, last_error=last_error)


@app.route("/api/pnl")
def api_pnl():
    with _cache_lock:
        data = _cache["data"]
    return jsonify(data)


@app.route("/api/status")
def api_status():
    """Debug endpoint — shows background thread health without exposing internals."""
    with _cache_lock:
        return jsonify({
            "last_run_started": _cache["last_run_started"],
            "last_run_finished": _cache["last_run_finished"],
            "has_error": _cache["last_error"] is not None,
            "phase": _cache["data"].get("phase"),
        })


@app.route("/healthz")
def healthz():
    # Always responds instantly — never blocked by the scanner thread
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
