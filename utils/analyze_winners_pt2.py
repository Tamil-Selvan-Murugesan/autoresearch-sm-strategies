"""Step 2: dump strategy code, compute baselines, look-ahead checks."""

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CFG
from backtest import load

DB_PATH = CFG["db_path"]
OUT = Path("reports/winners")


def code_for(strategy: str, timeframe: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT r.strategies FROM results res JOIN runs r ON r.run_id = res.run_id
           WHERE res.strategy = ? AND res.timeframe = ?
           ORDER BY r.run_at DESC LIMIT 1""",
        (strategy, timeframe),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    for s in json.loads(row[0]):
        if s["name"] == strategy:
            return s["code"]
    return None


def baseline(df: pd.DataFrame, horizons: list[int]) -> dict:
    """Buy-and-hold-N-days baseline for the same window."""
    out = {}
    for h in horizons:
        fwd = df["close"].shift(-h) / df["close"] * 100 - 100
        s = fwd.dropna()
        wins = s[s > 0]
        out[f"fwd_{h}d"] = {
            "n": int(len(s)),
            "mean": round(float(s.mean()), 3),
            "median": round(float(s.median()), 3),
            "win_rate": round(float(len(wins) / len(s) * 100), 1),
        }
    return out


def main() -> None:
    strategies = [
        ("nr7_then_range_expansion_breakout", "daily"),
        ("nr7_breakout_with_weekly_silent_top_avoidance", "mixed"),
        ("nr7_breakout_with_weekly_silent_top_avoidance_and_compression_quality", "mixed"),
        ("nr7_expansion_breakout_with_weekly_uptrend_filter", "mixed"),
        ("weekly_silent_top_exhaustion", "weekly"),
    ]

    for name, tf in strategies:
        code = code_for(name, tf)
        path = OUT / f"code_{tf}_{name}.py"
        path.write_text(code or "# code not found\n")
        print(f"Wrote {path}")

    df_daily = load(CFG["index"], "daily")
    df_weekly = load(CFG["index"], "weekly")
    print()
    print(f"Daily span: {df_daily['date'].min().date()} → {df_daily['date'].max().date()}, "
          f"{len(df_daily)} bars")
    print(f"Weekly span: {df_weekly['date'].min().date()} → {df_weekly['date'].max().date()}, "
          f"{len(df_weekly)} bars")
    print()
    print("Daily buy-and-hold baseline:")
    for k, v in baseline(df_daily, [1, 2, 5, 10]).items():
        print(f"  {k}: mean={v['mean']}%  win_rate={v['win_rate']}%  n={v['n']}")
    print("Weekly buy-and-hold baseline:")
    for k, v in baseline(df_weekly, [1, 2, 4, 8]).items():
        print(f"  {k}: mean={v['mean']}%  win_rate={v['win_rate']}%  n={v['n']}")

    # Overlap between mixed winners and the daily nr7 — they may all be the same edge
    daily_signals = pd.read_csv(OUT / "signals_daily_nr7_then_range_expansion_breakout.csv")
    daily_dates = set(daily_signals["date"].astype(str).str[:10])

    print()
    print("Signal overlap with daily nr7_then_range_expansion_breakout (74 signals):")
    for name, tf in strategies:
        if name == "nr7_then_range_expansion_breakout":
            continue
        sigfile = OUT / f"signals_{tf}_{name}.csv"
        if not sigfile.exists():
            continue
        s = pd.read_csv(sigfile)
        s_dates = set(s["date"].astype(str).str[:10])
        overlap = s_dates & daily_dates
        print(f"  {tf}/{name}: own={len(s_dates)}, overlap_with_daily_nr7={len(overlap)} "
              f"({100*len(overlap)/max(len(s_dates),1):.0f}%)")


if __name__ == "__main__":
    main()
