# -*- coding: utf-8 -*-
import os
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

_scanner_thread = None
_scanner_thread_lock = threading.Lock()


def _background_scanner_loop():
    print(f"[BG] Thread running in PID={os.getpid()}, _cache id={id(_cache)}", flush=True)
    while True:
        try:
            print(f"[BG] PID={os.getpid()} Starting scan cycle...", flush=True)
            with _cache_lock:
                _cache["last_run_started"] = datetime.utcnow().isoformat()

            result = scanner.run_scan()
            print(f"[BG] PID={os.getpid()} run_scan() returned, "
                  f"positions={len(result.get('positions', []))}, "
                  f"message={result.get('message')}", flush=True)

            with _cache_lock:
                _cache["data"] = result
                _cache["last_run_finished"] = datetime.utcnow().isoformat()
                _cache["last_error"] = None
            print(f"[BG] PID={os.getpid()} Cache updated. "
                  f"positions len={len(_cache['data'].get('positions', []))}", flush=True)

        except Exception as e:
            err_text = f"{e}\n{traceback.format_exc()}"
            with _cache_lock:
                _cache["last_error"] = err_text
                prev = _cache["data"]
                _cache["data"] = {
                    **prev,
                    "message": (f"Background scan crashed: {e}. "
                                f"Check Render logs. Retrying in {SCAN_INTERVAL_SECONDS}s."),
                }
            print(f"[SCAN ERROR] PID={os.getpid()} {err_text}", flush=True)

        print(f"[BG] PID={os.getpid()} Sleeping for {SCAN_INTERVAL_SECONDS}s...", flush=True)
        time.sleep(SCAN_INTERVAL_SECONDS)


def start_background_scanner():
    """
    Idempotently starts the background scanner thread in the CURRENT
    process. Must be called from inside each gunicorn worker process
    (via the post_fork hook in gunicorn.conf.py) — NOT at plain module
    import time — otherwise the thread ends up running in gunicorn's
    master process instead of the worker(s) that actually serve HTTP
    requests, and the cache those workers see never gets updated.
    """
    global _scanner_thread
    with _scanner_thread_lock:
        if _scanner_thread is not None and _scanner_thread.is_alive():
            print(f"[APP] PID={os.getpid()} scanner thread already running, skip.", flush=True)
            return
        _scanner_thread = threading.Thread(target=_background_scanner_loop, daemon=True)
        _scanner_thread.start()
        print(f"[APP] PID={os.getpid()} scanner thread started. "
              f"alive={_scanner_thread.is_alive()}", flush=True)


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
    print(f"[API] PID={os.getpid()} /api/pnl called. "
          f"positions len={len(data.get('positions', []))}, phase={data.get('phase')}",
          flush=True)
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
            "scanner_thread_alive": _scanner_thread.is_alive() if _scanner_thread else False,
            "pid": os.getpid(),
        })


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    # Local/dev run (no gunicorn, no fork) — safe to start the thread here.
    start_background_scanner()
    app.run(host="0.0.0.0", port=5000, debug=False)
