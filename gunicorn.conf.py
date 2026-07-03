# -*- coding: utf-8 -*-
"""
Gunicorn config. Starts the background scanner thread AFTER fork,
inside each worker process — NOT in the master process.

This fixes a bug where the background thread ran in gunicorn's master
process while HTTP requests were served by a separate forked worker
process with its own independent memory / cache, causing the dashboard
to always see a stale "STARTING" cache that never updated.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
timeout = 0  # disable gunicorn's worker timeout/kill — our background
             # thread can legitimately run long fetches; we don't want
             # gunicorn silently recycling the worker mid-scan.
worker_class = "sync"
preload_app = False  # explicit: we do NOT want the app module imported
                      # (and its threads started) before fork. Threads
                      # must only start in post_fork, inside the worker.


def post_fork(server, worker):
    """
    Called by gunicorn in the CHILD (worker) process, right after fork().
    This is the correct place to start any background thread that needs
    to run in the same process that serves HTTP requests.
    """
    import app as app_module
    app_module.start_background_scanner()
    server.log.info(
        f"[gunicorn] post_fork: background scanner thread started in "
        f"worker PID={os.getpid()}"
    )
