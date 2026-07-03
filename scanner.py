# -*- coding: utf-8 -*-
"""
NSE Next-Day Buy Scanner — core logic module.
Used by app.py (via a background thread) to power the dashboard.
"""

import os
import time
import socket
import warnings
import traceback
from datetime import date, datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

warnings.filterwarnings("ignore")

# Global safety net: no single network call in this process should hang forever.
socket.setdefaulttimeout(30)

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


def log(msg: str):
    print(msg, flush=True)


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
    minutes_now = h * 60 + m

    PRE_OPEN_START = 7 * 60
    PRE_OPEN_END   = 9 * 60 + 14
    LIVE_START     = 9 * 60 + 15
    LIVE_END       = 15 * 60 + 29
    CLOSED_START   = 15 * 60 + 30
    CLOSED_END     = 17 * 60 + 59

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
        df[col].dropna().astype(str).str.strip().str.upper()
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

        log(f"[FETCH] Batch {batch_num}/{total_batches}: "
            f"{batch_raw[0]}...{batch_raw[-1]}")

        raw = None
        for attempt in range(BATCH_RETRIES + 1):
            try:
                raw = yf.download(
                    tickers=batch, start=start_str, end=end_str,
                    interval=YF_INTERVAL, group_by="ticker",
                    auto_adjust=False, progress=False, threads=True,
                    timeout=30,
                )
                break
            except Exception as e:
                log(f"[WARN] Batch {batch_num} attempt {attempt + 1} failed: {e}")
                time.sleep(5 * (attempt + 1))

        if raw is None or raw.empty:
            log(f"[WARN] Batch {batch_num} returned no data after retries, skipping.")
            time.sleep(BATCH_DELAY_SECONDS)
            continue

        log(f"[FETCH] Batch {batch_num} download() returned, "
            f"shape={raw.shape}, now post-processing {len(batch)} symbols...")

        for idx, (sym_ns, sym) in enumerate(zip(batch, batch_raw)):
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
            except Exception as e:
                log(f"[WARN] Failed to process {sym} (item {idx}): {e}")

        log(f"[FETCH] Batch {batch_num} done — {len(all_data)} symbols collected so far.")
        time.sleep(BATCH_DELAY_SECONDS)

    return all_data


# ── SCAN WINDOW ────────────────────────────────────────────────────────────

def scan_window(all_data: dict, scan_date: date) -> pd.DataFrame:
    log(f"[SCAN_WINDOW] Starting, {len(all_data)} symbols to check for scan_date={scan_date}")
    rows = []
    for sym, df in all_data.items():
        try:
            day_df = df[df["_date"] == scan_date]
            if day_df.empty:
                continue
            win = day_df.between_time(WINDOW_START, WINDOW_END)
            if win.empty:
                continue

            first_open = float(win["Open"].iat[0])
            last_close = float(win["Close"].iat[-1])
            max_high = float(win["High"].max())
            day_close = float(day_df["Close"].iat[-1])

            if first_open <= 0 or last_close <= 0:
                continue
            pct_gain = (last_close - first_open) / first_open * 100
            if last_close <= first_open or pct_gain < MIN_WINDOW_GAIN_PCT:
                continue

            rows.append({
                "symbol": sym,
                "prev_close": round(day_close, 2),
                "win_open": round(first_open, 2),
                "win_close": round(last_close, 2),
                "win_high": round(max_high, 2),
                "pct_gain": round(pct_gain, 3),
                "gap_skip_above": round(day_close * GAP_UP_FILTER_MULT, 2),
            })
        except Exception as e:
            log(f"[WARN] scan_window failed for {sym}: {e}")
            continue
    log(f"[SCAN_WINDOW] Done — {len(rows)} symbols passed filters")
    return pd.DataFrame(rows)


def build_buylist(scan_df: pd.DataFrame) -> pd.DataFrame:
    if scan_df.empty:
        return pd.DataFrame()
    buylist = scan_df.nlargest(GAINER_TOP_N, "pct_gain").copy()
    buylist.sort_values("pct_gain", ascending=False, inplace=True)
    buylist.reset_index(drop=True, inplace=True)
    log(f"[BUYLIST] {len(buylist)} symbols selected: {buylist['symbol'].tolist()}")
    return buylist


# ── P&L ─────────────────────────────────────────────────────────────────────

def compute_pnl(buylist: pd.DataFrame, all_data: dict, trade_date: date, phase: str) -> pd.DataFrame:
    log(f"[COMPUTE_PNL] Starting for {len(buylist)} symbols, phase={phase}")
    rows = []
    for _, row in buylist.iterrows():
        sym = row["symbol"]
        prev_close = row["prev_close"]
        entry_price = None
        current_price = None
        status = ""

        try:
            df = all_data.get(sym)
            day_df = df[df["_date"] == trade_date] if df is not None else pd.DataFrame()

            if phase == "PRE_OPEN":
                status = "NOT BOUGHT YET"
            elif phase in ("LIVE", "CLOSED"):
                if day_df.empty:
                    status = "NO DATA"
                else:
                    entry_price = float(day_df["Open"].iat[0])
                    current_price = float(day_df["Close"].iat[-1])
                    status = "LIVE" if phase == "LIVE" else "CLOSED"

            est_qty = int(CAPITAL_PER_TRADE // prev_close) if prev_close > 0 else 0

            if entry_price and current_price:
                pnl_pct = (current_price - entry_price) / entry_price * 100
                pnl_rs = round((current_price - entry_price) * est_qty, 2)
            else:
                pnl_pct = 0.0
                pnl_rs = 0.0

            rows.append({
                "symbol": sym,
                "status": status,
                "prev_close": prev_close,
                "entry_price": round(entry_price, 2) if entry_price else None,
                "current_price": round(current_price, 2) if current_price else None,
                "qty": est_qty,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_rs": pnl_rs,
            })
        except Exception as e:
            log(f"[WARN] compute_pnl failed for {sym}: {e}")
            rows.append({
                "symbol": sym,
                "status": "ERROR",
                "prev_close": prev_close,
                "entry_price": None,
                "current_price": None,
                "qty": 0,
                "pnl_pct": 0.0,
                "pnl_rs": 0.0,
            })

    log(f"[COMPUTE_PNL] Done — {len(rows)} rows computed")
    return pd.DataFrame(rows)


# ── MAIN ENTRYPOINT USED BY THE BACKGROUND THREAD ──────────────────────────

def run_scan():
    log(f"[SCAN] run_scan() called. pandas={pd.__version__}")
    now = get_ist_now()
    trade_date = now.date()
    phase = market_phase(now, trade_date)

    result = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "trade_date": str(trade_date),
        "phase": phase,
        "positions": [],
        "total_pnl_rs": 0.0,
        "winners": 0,
        "losers": 0,
        "message": None,
    }

    if phase == "WEEKEND":
        result["message"] = f"{trade_date} is a weekend. Market closed."
        return result
    if phase == "OTHER":
        result["message"] = ("Outside tracking hours "
                              "(7-8AM pre-open / 9:15AM-3:29PM live / 3:30-5PM final).")
        return result
    if not os.path.exists(CSV_PATH):
        result["message"] = f"'{CSV_PATH}' not found on server."
        return result

    try:
        scan_date = prev_trading_day(trade_date)
        symbols = load_symbols(CSV_PATH)

        log(f"[SCAN] Starting scan for {len(symbols)} symbols "
            f"(scan_date={scan_date}, trade_date={trade_date}, phase={phase})")

        all_data = fetch_intraday(symbols, scan_date, trade_date)
        log(f"[SCAN] fetch_intraday() returned, {len(all_data)} symbols in all_data")

        if not all_data:
            result["message"] = "No data fetched from Yahoo Finance. Will retry automatically."
            return result

        scan_df = scan_window(all_data, scan_date)
        log(f"[SCAN] scan_window() returned, shape={scan_df.shape}")

        if scan_df.empty:
            result["message"] = f"No stocks passed filters on {scan_date}. Nothing to track today."
            return result

        buylist = build_buylist(scan_df)
        log(f"[SCAN] build_buylist() returned, shape={buylist.shape}")

        pnl_df = compute_pnl(buylist, all_data, trade_date, phase)
        log(f"[SCAN] compute_pnl() returned, shape={pnl_df.shape}")

        result["positions"] = pnl_df.to_dict(orient="records")
        result["total_pnl_rs"] = round(float(pnl_df["pnl_rs"].sum()), 2)
        result["winners"] = int((pnl_df["pnl_rs"] > 0).sum())
        result["losers"] = int((pnl_df["pnl_rs"] < 0).sum())
        result["scan_date"] = str(scan_date)

        log(f"[SCAN] Complete — {len(result['positions'])} positions, "
            f"total P&L Rs {result['total_pnl_rs']}")

    except Exception as e:
        err = traceback.format_exc()
        log(f"[SCAN ERROR] Exception inside run_scan(): {err}")
        result["message"] = f"Scan error: {e} (will retry next cycle in ~60s)"

    log(f"[SCAN] run_scan returning: phase={result['phase']}, "
        f"positions={len(result['positions'])}, message={result['message']}")

    return result
