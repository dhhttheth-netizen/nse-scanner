# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════════════════
#  NSE NEXT-DAY BUY SCANNER
#
#  Scans a given date's 2:15–3:30 PM candles for stocks that rallied hard
#  into the close, and produces a list of stocks to BUY AT OPEN on the
#  next trading day.
#
#  USAGE (manual):
#      python nse_date_scanner.py 2026-07-01
#      (if no date passed, uses TODAY — this is what runs on a schedule)
#
#  DISCLAIMER: For educational / research use only. Not financial advice.
# ═══════════════════════════════════════════════════════════════════════════

import sys, os, warnings
from datetime import date, datetime, timedelta

print("=" * 65)
print("  NSE NEXT-DAY BUY SCANNER")
print("=" * 65)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("Loading libraries ...", flush=True)
import yfinance as yf
import pandas as pd
import pytz

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

warnings.filterwarnings("ignore")
print("Libraries loaded.\n", flush=True)


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════

CSV_PATH            = "5000.csv"     # your symbols file
GAINER_TOP_N        = 5              # top-N stocks by % gain in window
MIN_WINDOW_GAIN_PCT = 1.5            # minimum % gain in window to qualify
GAP_UP_FILTER_MULT  = 1.02           # skip BUY if next-day open already > prev_close x this
CAPITAL_PER_TRADE   = 50_000         # Rs — used to estimate qty
YF_INTERVAL         = "1m"
YF_BATCH_SIZE       = 50
WINDOW_START        = "14:15"
WINDOW_END          = "15:30"


# ═══════════════════════════════════════════════════════════════════════════
#  DATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _skip_weekends(d: date, step: int) -> date:
    d += timedelta(days=step)
    while d.weekday() >= 5:
        d += timedelta(days=step)
    return d

def next_trading_day(d: date) -> date:
    return _skip_weekends(d, 1)

def get_scan_date() -> date:
    """
    Priority:
      1. Date passed as a command-line argument (YYYY-MM-DD)
      2. Today's date (default — used when run on a schedule)
    """
    if len(sys.argv) > 1:
        try:
            return datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        except ValueError:
            print(f"[WARN] Could not parse '{sys.argv[1]}' as YYYY-MM-DD, using today.")
    return datetime.now(pytz.timezone("Asia/Kolkata")).date()


# ═══════════════════════════════════════════════════════════════════════════
#  LOAD SYMBOLS
# ═══════════════════════════════════════════════════════════════════════════

def load_symbols(path: str) -> list:
    df  = pd.read_csv(path)
    col = df.columns[0]
    syms = (
        df[col].dropna().str.strip().str.upper()
        .pipe(lambda s: s[s != ""])
        .tolist()
    )
    cleaned = [s.replace(".NS", "").replace(".NSE", "") for s in syms]
    print(f"[INFO] {len(cleaned)} symbols loaded from {path}", flush=True)
    return cleaned


# ═══════════════════════════════════════════════════════════════════════════
#  FETCH INTRADAY
# ═══════════════════════════════════════════════════════════════════════════

def fetch_intraday(symbols: list, scan_date: date) -> dict:
    start_str = (scan_date - timedelta(days=1)).strftime("%Y-%m-%d")
    end_str   = (scan_date + timedelta(days=2)).strftime("%Y-%m-%d")

    print(f"\n[FETCH] {YF_INTERVAL} candles for {len(symbols)} symbols", flush=True)
    print(f"        Date range : {start_str} -> {end_str}", flush=True)
    print(f"        Scan date  : {scan_date}\n", flush=True)

    yf_syms  = [s + ".NS" for s in symbols]
    all_data = {}

    for i in range(0, len(yf_syms), YF_BATCH_SIZE):
        batch     = yf_syms[i : i + YF_BATCH_SIZE]
        batch_raw = [s.replace(".NS", "") for s in batch]
        print(f"  Batch {i // YF_BATCH_SIZE + 1}: {batch_raw[0]} ... {batch_raw[-1]}", flush=True)

        try:
            raw = yf.download(
                tickers     = batch,
                start       = start_str,
                end         = end_str,
                interval    = YF_INTERVAL,
                group_by    = "ticker",
                auto_adjust = False,
                progress    = False,
                threads     = True,
            )
        except Exception as e:
            print(f"  [WARN] Batch failed: {e}", flush=True)
            continue

        if raw is None or raw.empty:
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

    print(f"\n[FETCH] Complete — {len(all_data)} symbols with data.\n", flush=True)
    return all_data


# ═══════════════════════════════════════════════════════════════════════════
#  SCAN WINDOW
# ═══════════════════════════════════════════════════════════════════════════

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

        if last_close <= first_open:          # must be bullish
            continue
        if pct_gain < MIN_WINDOW_GAIN_PCT:    # minimum gain filter
            continue

        rows.append({
            "symbol"          : sym,
            "prev_close"      : round(day_close, 2),
            "win_open"        : round(first_open, 2),
            "win_close"       : round(last_close, 2),
            "win_high"        : round(max_high, 2),
            "pct_gain"        : round(pct_gain, 3),
            "gap_skip_above"  : round(day_close * GAP_UP_FILTER_MULT, 2),
        })

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  BUILD BUYLIST  (top gainers only — wick basket removed)
# ═══════════════════════════════════════════════════════════════════════════

def build_buylist(scan_df: pd.DataFrame) -> pd.DataFrame:
    if scan_df.empty:
        return pd.DataFrame()

    buylist = scan_df.nlargest(GAINER_TOP_N, "pct_gain").copy()
    buylist.sort_values("pct_gain", ascending=False, inplace=True)
    buylist.reset_index(drop=True, inplace=True)
    buylist.index += 1
    return buylist


# ═══════════════════════════════════════════════════════════════════════════
#  PRINT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def fmt_table(df, headers=None):
    if HAS_TABULATE:
        return tabulate(
            df, headers=headers or "keys",
            tablefmt="outline", showindex=True, floatfmt=".2f"
        )
    return df.to_string()


def print_trade_cards(buylist: pd.DataFrame, trade_date: date):
    print("\n" + "=" * 65)
    print(f"  *  TRADE CARDS  —  BUY ON  {trade_date}  AT OPEN  *")
    print("=" * 65)

    for _, row in buylist.iterrows():
        sym        = row["symbol"]
        prev_close = row["prev_close"]
        est_qty    = int(CAPITAL_PER_TRADE // prev_close) if prev_close > 0 else "—"

        print(f"\n  [GAINER]  {sym}")
        print(f"  {'─' * 57}")
        print(f"  Action        : BUY at OPEN  (market buy order)")
        print(f"  Exit by       : 3:15 PM IST  (or SL / Target hit)")
        print(f"  Prev Close    : Rs {prev_close:>10,.2f}")
        print(f"  Est. Qty      : ~{est_qty} shares  (Rs {CAPITAL_PER_TRADE:,} capital)")
        print(f"  SKIP TRADE    : If open > Rs {row['gap_skip_above']:,.2f}  "
              f"(gap-up > {round((GAP_UP_FILTER_MULT - 1)*100)}% — already moved)")
        print(f"  Signal        : Gain {row['pct_gain']:+.2f}%")

    print("\n" + "=" * 65)


def print_summary_table(buylist: pd.DataFrame, trade_date: date):
    gap_pct = round((GAP_UP_FILTER_MULT - 1) * 100, 1)

    print("\n" + "─" * 65)
    print(f"  SKIP / QTY SUMMARY TABLE  —  Trade Date: {trade_date}")
    print(f"  Skip trade if next-day open is ABOVE 'Skip If Open Above'")
    print(f"  (means stock already gapped up more than {gap_pct}% from prev close)")
    print("─" * 65)

    summary_rows = []
    for _, row in buylist.iterrows():
        sym        = row["symbol"]
        prev_close = row["prev_close"]
        est_qty    = int(CAPITAL_PER_TRADE // prev_close) if prev_close > 0 else 0

        summary_rows.append({
            "symbol"         : sym,
            "prev_close"     : prev_close,
            "est_qty"        : est_qty,
            "skip_if_above"  : row["gap_skip_above"],
            "skip_condition" : f"Open > {gap_pct}% gap-up",
        })

    summary_df = pd.DataFrame(summary_rows)
    summary_df.index = range(1, len(summary_df) + 1)

    print(fmt_table(
        summary_df,
        headers=[
            "#", "Symbol", "Prev Close (Rs)", "Est. Qty",
            "Skip If Open Above (Rs)", "Skip Condition",
        ],
    ))
    print("─" * 65)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    scan_date  = get_scan_date()
    trade_date = next_trading_day(scan_date)

    print(f"\n  Scan date  : {scan_date}  ({WINDOW_START}–{WINDOW_END} window)")
    print(f"  Trade date : {trade_date}  <- BUY THESE STOCKS AT OPEN")
    print(f"  Config     : GAINER top-{GAINER_TOP_N} | "
          f"Min gain {MIN_WINDOW_GAIN_PCT}% | Gap-up filter x{GAP_UP_FILTER_MULT}")
    print("─" * 65)

    if not os.path.exists(CSV_PATH):
        print(f"\n[ERROR] '{CSV_PATH}' not found.")
        print(f"        Update CSV_PATH at the top of this script.")
        sys.exit(1)

    symbols = load_symbols(CSV_PATH)

    all_data = fetch_intraday(symbols, scan_date)
    if not all_data:
        print("[ERROR] No data fetched. Check your internet connection.")
        sys.exit(1)

    print(f"[SCAN] Analysing {WINDOW_START}–{WINDOW_END} window on {scan_date} ...",
          flush=True)
    scan_df = scan_window(all_data, scan_date)

    if scan_df.empty:
        print(f"\n[RESULT] No stocks passed filters on {scan_date}.")
        print(f"         Nothing to buy on {trade_date}.")
        return

    print(f"[SCAN] {len(scan_df)} stocks passed "
          f"(gain > {MIN_WINDOW_GAIN_PCT}%  &  bullish in window)\n")

    print("─" * 65)
    print(f"  ALL QUALIFYING STOCKS  ({scan_date})")
    print("─" * 65)
    disp = scan_df.sort_values("pct_gain", ascending=False).copy()
    disp.index = range(1, len(disp) + 1)
    print(fmt_table(
        disp[["symbol", "prev_close", "win_open", "win_close",
              "win_high", "pct_gain", "gap_skip_above"]],
        headers=["#", "Symbol", "PrevClose", "WinOpen", "WinClose",
                 "WinHigh", "Gain%", "SkipIfAbove"],
    ))

    buylist = build_buylist(scan_df)

    print("\n" + "=" * 65)
    print(f"  BUYLIST — BUY ON {trade_date}  ({len(buylist)} signals)")
    print("=" * 65)
    print(fmt_table(
        buylist[["symbol", "prev_close", "pct_gain", "gap_skip_above"]],
        headers=["#", "Symbol", "PrevClose", "Gain%", "SkipIfAbove"],
    ))
    print(f"\n  GAINERS : {sorted(set(buylist['symbol']))}")

    print_trade_cards(buylist, trade_date)
    print_summary_table(buylist, trade_date)

    out_file = f"buylist_{scan_date}.csv"
    save_df  = buylist.copy()
    save_df.insert(0, "scan_date",  str(scan_date))
    save_df.insert(1, "trade_date", str(trade_date))
    save_df.to_csv(out_file, index=True)
    print(f"\n[OUTPUT] Saved -> {out_file}  ({len(buylist)} rows)")
    print("[DONE]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Cancelled by user.")
        sys.exit(0)
    except Exception:
        import traceback
        print("\n[FATAL ERROR]", flush=True)
        traceback.print_exc()
        sys.exit(1)
