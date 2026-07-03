# -*- coding: utf-8 -*-
"""
NSE Next-Day Buy Scanner — core logic module.
Used by app.py (via a background thread) to power the dashboard.
"""

import os
import time
import warnings
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

warnings.filterwarnings("ignore")

IST = pytz.timezone("Asia/Kolkata")

# ── CONFIG ───────────────────────────────────────────────────────────────
CSV_PATH             = "5000.csv"
GAINER_TOP_N         = 5
MIN_WINDOW_GAIN_PCT  = 1.5
GAP_UP_FILTER_MULT   = 1.02
CAPITAL_PER_TRADE    = 50_000
YF_INTERVAL          = "1m"
YF_BATCH_SIZE        = 50
BATCH_DELAY_SECONDS  = 3
BATCH_RETRIES        = 2
WINDOW_START         = "14:15"
WINDOW_END           = "15:30"


# ── DATE / TIME HELPERS ──────────────────────────────────────────────────

def _skip_weekends(d: date, step: int) -> date:
    d += timedelta(days=step)
    while d.weekday() >= 5:
        d += timedelta(days=step)
    return d

def next_trading_day(d: date) -> date:
    return _skip_weekends(d, 1)

def prev_trading_day(d: date) -> date:
    return _skip_weekends(d, -1)

def get_ist_now() -> datetime:
    return datetime.now(IST)

def market_phase(now: datetime, trade_date: date) -> str:
    """
    PRE_OPEN : 07:00 - 08:59  -> positions not taken yet, P&L = 0
    LIVE     : 09:15 - 15:29  -> market open, running P&L
    CLOSED   : 15:30 - 17:59  -> market closed, final day P&L
               (starts exactly at market close, no gap)
    OTHER    : everything else -> outside tracking hours
    """
    if trade_date.weekday() >= 5:
        return "WEEKEND"

    h, m = now.hour, now.minute
    minutes_now = h * 60 + m

    PRE_OPEN_START = 7 * 60          # 07:00
    PRE_OPEN_END   = 9 * 60 + 14     # 09:14 (LIVE starts 09:15)
    LIVE_START     = 9 * 60 + 15     # 09:15
    LIVE_END       = 15 * 60 + 29    # 15:29
    CLOSED_START   = 15 * 60 + 30    # 15:30  <- closed starts right at market close
    CLOSED_END     = 17 * 60 + 59    # 17:59

    if PRE_OPEN_START <= minutes_now <= PRE_OPEN_END:
        return "PRE_OPEN"
    if LIVE_START <= minutes_now <= LIVE_END:
        return "LIVE"
    if CLOSED_START <= minutes_now <= CLOSED_END:
        return "CLOSED"
    return "OTHER"


# ── LOAD SYMBOLS ─────────────────────────────────────────────────────────

def load_symbols(path: str = CSV_PATH) -> list:
    df = pd.read_csv(path)
    col = df.columns[0]
    syms = (
        df[col].dropna().str.strip().str.upper()
        .pipe(lambda s: s[s != ""])
        .tolist()
    )
    return [s.replace(".NS", "").replace(".NSE", "") for s in syms]


# ── FETCH INTRADAY ────────────────────────────────────────────────────────

def fetch_intraday(symbols: list, scan_date: date, trade_date: date) -> dict:
    start_str = (scan_date - timedelta(days=1)).strftime("%Y-%m-%d")
    end_str = (trade_date + timedelta(days=1)).strftime("%Y-%m-%d")

    yf_syms = [s + ".NS" for s in symbols]
    all_data = {}

    total_batches = (len(yf_syms) + YF_BATCH_SIZE - 1) // YF_BATCH_SIZE

    for i in range(0, len(yf_syms), YF_BATCH_SIZE):
        batch_num = i // YF_BATCH_SIZE + 1
        batch = yf_syms[i:i + YF_BATCH_SIZE]
        batch_raw = [s.replace(".NS", "") for s in batch]

        print(f"[FETCH] Batch {batch_num}/{total_batches}: "
              f"{batch_raw[0]}...{batch_raw[-1]}", flush=True)

        raw = None
        for attempt in range(BATCH_RETRIES + 1):
            try:
                raw = yf.download(
                    tickers=batch, start=start_str, end=end_str,
                    interval=YF_INTERVAL, group_by="ticker",
                    auto_adjust=False, progress=False, threads=True,
                )
                break
            except Exception as e:
                print(f"[WARN] Batch {batch_num} attempt {attempt + 1} failed: {e}",
                      flush=True)
