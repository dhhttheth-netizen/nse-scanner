# -*- coding: utf-8 -*-
from flask import Flask, render_template, jsonify
from datetime import datetime
import threading
import time
import traceback

import scanner

app = Flask(__name__)

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

SCAN_INTERVAL_SECONDS = 60


def background_scanner():
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
            # scanner.run_scan() now catches its own internal exceptions,
            # so this branch should only fire for truly unexpected errors
            # (e.g. import-level issues). Even so, we refresh _cache["data"]
            # here too, so the dashboard NEVER stays stuck on "Warming up".
            err_text = f"{e}\n{traceback.format_exc()}"
            with _cache_lock:
                _cache["last_error"] = err_text
                prev = _cache["data"]
                _cache["data"] = {
                    **prev,
                    "message": (f"Background scan crashed: {e}. "
                                f"Check Render logs. Retrying in {SCAN_INTERVAL_SECONDS}s."),
                }
            print(f"[SCAN ERROR] {err_text}", flush=True)

        time.sleep(SCAN_INTERVAL_SECONDS)


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
    with _cache_lock:
        return jsonify({
            "last_run_started": _cache["last_run_started"],
            "last_run_finished": _cache["last_run_finished"],
            "has_error": _cache["last_error"] is not None,
            "last_error": _cache["last_error"],
            "phase": _cache["data"].get("phase"),
        })


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
