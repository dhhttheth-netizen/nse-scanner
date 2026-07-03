# -*- coding: utf-8 -*-
"""
NSE Next-Day Buy Scanner — core logic module.
Used by app.py (via a background thread) to power the dashboard.
No printing / blocking here beyond normal logging — just data in, data out.
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
YF_BATCH_SIZE        = 25       # smaller batches = less likely to be rate-limited
BATCH_DELAY_SECONDS  = 3        # pause between batches
BATCH_RETRIES        = 2        # retry attempts per batch on failure
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
    if trade_date.weekday() >= 5:
        return "WEEKEND"
    h, m = now.hour, now.minute
    if h in (7, 8):
        return "PRE_OPEN"
    if h in (16, 17):
        return "CLOSED"
    if (h == 9 and m >= 15) or (10 <= h < 15) or (h == 15 and m <= 30):
        return "LIVE"
    return "OTHER"


# ── LOAD SYMBOLS ─────────────────────────────────────────────────────────

def load_symbols(path: str = CSV_PATH) -> list:
    df  = pd.read_csv(path)
    col = df.columns[0]
    syms = (
        df[col].dropna().str.strip().str.upper()
        .pipe(lambda s: s[s != ""])
        .tolist()
    )
    return [s.replace(".NS", "").replace(".NSE", "") for s in syms]


# ── FETCH INTRADAY  (with retry + backoff to survive rate limiting) ──────

def fetch_intraday(symbols: list, scan_date: date, trade_date: date) -> dict:
    start_str = (scan_date - timedelta(days=1)).strftime("%Y-%m-%d")
    end_str   = (trade_date + timedelta(days=1)).strftime("%Y-%m-%d")

    yf_syms  = [s + ".NS" for s in symbols]
    all_data = {}

    total_batches = (len(yf_syms) + YF_BATCH_SIZE - 1) // YF_BATCH_SIZE

    for i in range(0, len(yf_syms), YF_BATCH_SIZE):
        batch_num = i // YF_BATCH_SIZE + 1
        batch     = yf_syms[i : i + YF_BATCH_SIZE]
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
                print(f"[WARN] Batch {batch_num} attempt {attempt+1} failed: {e}",
                      flush=True)
                time.sleep(5 * (attempt + 1))   # exponential-ish backoff

        if raw is None or raw.empty:
            time.sleep(BATCH_DELAY_SECONDS)
            continue

        for sym_ns, sym in zip(batch, batch_raw):
            try:
                df = raw.copy() if len(batch) == 1 else raw[sym_ns].copy()
                df.dropna(how="all", inplace=True)
                if df.empty:
                    continue
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                df.index = df.index.tz_convert("Asia/Kolkata")
                df = df.between_time("09:15", "15:30")
                if df.empty:
                    continue
                df = df.copy()
                df["_date"] = df.index.date
                all_data[sym] = df
            except Exception:
                pass

        time.sleep(BATCH_DELAY_SECONDS)   # pause between batches — avoids rate limit

    return all_data


# ── SCAN WINDOW ───────────────────────────────────────────────────────────

def scan_window(all_data: dict, scan_date: date) -> pd.DataFrame:
    rows = []
    for sym, df in all_data.items():
        day_df = df[df["_date"] == scan_date]
        if day_df.empty:
            continue
        win = day_df.between_time(WINDOW_START, WINDOW_END)
        if win.empty:
            continue

        first_open = float(win["Open"].iat[0])
        last_close = float(win["Close"].iat[-1])
        max_high   = float(win["High"].max())
        day_close  = float(day_df["Close"].iat[-1])

        if first_open <= 0 or last_close <= 0:
            continue
        pct_gain = (last_close - first_open) / first_open * 100
        if last_close <= first_open or pct_gain < MIN_WINDOW_GAIN_PCT:
            continue

        rows.append({
            "symbol": sym, "prev_close": round(day_close, 2),
            "win_open": round(first_open, 2), "win_close": round(last_close, 2),
            "win_high": round(max_high, 2), "pct_gain": round(pct_gain, 3),
            "gap_skip_above": round(day_close * GAP_UP_FILTER_MULT, 2),
        })
    return pd.DataFrame(rows)


def build_buylist(scan_df: pd.DataFrame) -> pd.DataFrame:
    if scan_df.empty:
        return pd.DataFrame()
    buylist = scan_df.nlargest(GAINER_TOP_N, "pct_gain").copy()
    buylist.sort_values("pct_gain", ascending=False, inplace=True)
    buylist.reset_index(drop=True, inplace=True)
    return buylist


# ── P&L ───────────────────────────────────────────────────────────────────

def compute_pnl(buylist: pd.DataFrame, all_data: dict, trade_date: date, phase: str) -> pd.DataFrame:
    rows = []
    for _, row in buylist.iterrows():
        sym, prev_close = row["symbol"], row["prev_close"]
        entry_price = current_price = None
        status = ""

        df = all_data.get(sym)
        day_df = df[df["_date"] == trade_date] if df is not None else pd.DataFrame()

        if phase == "PRE_OPEN":
            status = "NOT BOUGHT YET"
        elif phase in ("LIVE", "CLOSED"):
            if day_df.empty:
                status = "NO DATA"
            else:
                entry_price   = float(day_df["Open"].iat[0])
                current_price = float(day_df["Close"].iat[-1])
                status = "LIVE" if phase == "LIVE" else "CLOSED"

        est_qty = int(CAPITAL_PER_TRADE // prev_close) if prev_close > 0 else 0

        if entry_price and current_price:
            pnl_pct = (current_price - entry_price) / entry_price * 100
            pnl_rs  = round((current_price - entry_price) * est_qty, 2)
        else:
            pnl_pct, pnl_rs = 0.0, 0.0

        rows.append({
            "symbol": sym, "status": status, "prev_close":
