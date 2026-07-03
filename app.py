# -*- coding: utf-8 -*-
from flask import Flask, render_template, jsonify
from datetime import datetime, timedelta
import threading

import scanner

app = Flask(__name__)

# Simple in-memory cache so every browser refresh doesn't re-hit Yahoo Finance
_cache = {"data": None, "fetched_at": None}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 90   # re-scan at most every 90 seconds


def get_scan_data(force: bool = False):
    with _cache_lock:
        now = datetime.utcnow()
        stale = (
            _cache["data"] is None
            or _cache["fetched_at"] is None
            or (now - _cache["fetched_at"]).total_seconds() > CACHE_TTL_SECONDS
        )
        if force or stale:
            _cache["data"] = scanner.run_scan()
            _cache["fetched_at"] = now
        return _cache["data"]


@app.route("/")
def dashboard():
    data = get_scan_data()
    return render_template("dashboard.html", data=data)


@app.route("/api/pnl")
def api_pnl():
    """JSON endpoint — used for the auto-refresh JS on the dashboard."""
    data = get_scan_data()
    return jsonify(data)


@app.route("/api/refresh")
def api_refresh():
    """Force a fresh scan, bypassing the cache."""
    data = get_scan_data(force=True)
    return jsonify(data)


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
