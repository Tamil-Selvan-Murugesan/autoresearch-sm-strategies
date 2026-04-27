"""Analyse winners from autoresearch.db: re-run code, capture signal dates, sanity-check."""

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CFG
from backtest import query_winners, load, _exec_single
from backtest_multi import load_all

DB_PATH = CFG["db_path"]
OUT = Path("reports/winners")
OUT.mkdir(parents=True, exist_ok=True)


def _fetch_strategy_code(run_id: int, name: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT strategies FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    for s in json.loads(row[0]):
        if s["name"] == name:
            return s["code"]
    return None


def _run_id_for(strategy: str, timeframe: str) -> int | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT res.run_id FROM results res JOIN runs r ON r.run_id = res.run_id
           WHERE res.strategy = ? AND res.timeframe = ?
           ORDER BY r.run_at DESC LIMIT 1""",
        (strategy, timeframe),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _signal_clustering(dates: pd.Series) -> dict:
    """Check whether signals are concentrated in a narrow window."""
    if dates.empty:
        return {}
    dates = pd.to_datetime(dates).sort_values()
    span_days = (dates.iloc[-1] - dates.iloc[0]).days
    return {
        "first_signal": str(dates.iloc[0].date()),
        "last_signal": str(dates.iloc[-1].date()),
        "span_days": span_days,
        "n_signals": len(dates),
        "signals_per_year": round(len(dates) / max(span_days / 365.25, 1e-9), 2) if span_days else None,
    }


def _recompute_horizon(result_df: pd.DataFrame, horizon: str) -> dict:
    s = result_df[horizon].dropna()
    if s.empty:
        return {}
    wins = s[s > 0]
    losses = s[s <= 0]
    return {
        "n": int(len(s)),
        "mean": round(float(s.mean()), 3),
        "median": round(float(s.median()), 3),
        "std": round(float(s.std()), 3),
        "min": round(float(s.min()), 3),
        "max": round(float(s.max()), 3),
        "win_rate": round(float(len(wins) / len(s) * 100), 1),
        "avg_win": round(float(wins.mean()) if len(wins) > 0 else 0.0, 3),
        "avg_loss": round(float(losses.mean()) if len(losses) > 0 else 0.0, 3),
    }


def main() -> None:
    winners = query_winners(
        min_win_rate=CFG["min_win_rate"],
        min_abs_mean=CFG["min_abs_mean"],
        min_signals=CFG["min_signals"],
        limit=CFG.get("max_winners", 50),
    )
    print(f"DB: {DB_PATH}")
    print(f"Winners table: {len(winners)} rows ({winners['strategy'].nunique()} unique strategies)\n")
    if winners.empty:
        return

    winners.to_csv(OUT / "winners_table.csv", index=False)

    # Group by (strategy, timeframe) so we re-execute each strategy once and
    # report per-horizon stats per group.
    cache: dict[tuple[str, str], pd.DataFrame] = {}
    summary_rows: list[dict] = []

    for (strategy, tf), group in winners.groupby(["strategy", "timeframe"]):
        run_id = _run_id_for(strategy, tf)
        code = _fetch_strategy_code(run_id, strategy) if run_id else None
        if code is None:
            print(f"[SKIP] {tf}/{strategy}: no code in runs.strategies")
            continue

        # Build dfs argument — single tf or 3 tfs for 'mixed'.
        if tf == "mixed":
            data = load_all(CFG["index"])
            dfs = [data["5min"], data["daily"], data["weekly"]]
            ref_df = data[group.iloc[0].get("entry_timeframe", "daily")] if False else data["daily"]
        else:
            df = load(CFG["index"], tf)
            dfs = [df]
            ref_df = df

        sdef = {"name": strategy, "params": {}, "code": code}
        params, result_df, status, err = _exec_single(sdef, dfs)
        if status != "ok":
            print(f"[SKIP] {tf}/{strategy}: re-exec status={status} err={err}")
            continue

        cache[(strategy, tf)] = result_df

        # Persist the signals (date + close + every fwd_ column).
        signal_path = OUT / f"signals_{tf}_{strategy}.csv"
        keep = ["date", "close"] + [c for c in result_df.columns if c.startswith("fwd_")]
        result_df[keep].to_csv(signal_path, index=False)

        clust = _signal_clustering(result_df["date"])
        full_span = (ref_df["date"].max() - ref_df["date"].min()).days

        # Per-horizon comparison: claimed vs recomputed
        for _, row in group.iterrows():
            horizon = row["horizon"]
            if horizon not in result_df.columns:
                continue
            recomp = _recompute_horizon(result_df, horizon)
            claimed = {
                "signals": int(row["signals"]),
                "mean": float(row["mean"]),
                "win_rate": float(row["win_rate"]),
                "avg_win": float(row["avg_win"]),
                "avg_loss": float(row["avg_loss"]),
                "expectancy": float(row["expectancy"]),
            }
            summary_rows.append({
                "strategy": strategy,
                "timeframe": tf,
                "horizon": horizon,
                "direction": row["direction"],
                "claimed_signals": claimed["signals"],
                "claimed_mean": claimed["mean"],
                "claimed_win_rate": claimed["win_rate"],
                "claimed_expectancy": claimed["expectancy"],
                "recomputed_n": recomp.get("n"),
                "recomputed_mean": recomp.get("mean"),
                "recomputed_win_rate": recomp.get("win_rate"),
                "recomputed_min": recomp.get("min"),
                "recomputed_max": recomp.get("max"),
                "first_signal": clust.get("first_signal"),
                "last_signal": clust.get("last_signal"),
                "signal_span_days": clust.get("span_days"),
                "data_span_days": full_span,
                "params_json": row["params"],
                "signals_csv": str(signal_path),
            })

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT / "winners_analysis.csv", index=False)
    print(f"\nWrote {len(summary_df)} analysis rows to {OUT/'winners_analysis.csv'}")
    print(f"Per-strategy signal CSVs in {OUT}/")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", 30)
    print()
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
