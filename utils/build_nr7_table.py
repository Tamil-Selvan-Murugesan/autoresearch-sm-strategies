"""Per-signal return trajectory for nr7_then_range_expansion_breakout (fwd 1..10)."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CFG
from backtest import load

OUT = Path("reports/winners/nr7_fwd10_detail.csv")


def main() -> None:
    df = load(CFG["index"], "daily").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    rng = df["high"] - df["low"]
    is_nr7 = rng == rng.rolling(7, min_periods=7).min()
    prev_is_nr7 = is_nr7.shift(1)
    prev_range = rng.shift(1)
    range_expansion = rng > 2.0 * prev_range
    close_pos = (df["close"] - df["low"]) / rng.replace(0, pd.NA)
    bullish_close = close_pos >= 0.7
    mask = prev_is_nr7.fillna(False) & range_expansion.fillna(False) & bullish_close.fillna(False)

    rows = []
    for i in df.index[mask]:
        nr7_idx = i - 1                       # day N (NR7 bar)
        lookback_start = i - 7                # day N-6 (first day of the 7-day NR7 window)
        lookback_end = nr7_idx                # day N
        signal_close = df.at[i, "close"]
        row = {
            "signal_date": df.at[i, "date"].date(),
            "signal_close": round(float(signal_close), 2),
            "nr7_window_start": df.at[lookback_start, "date"].date() if lookback_start >= 0 else None,
            "nr7_window_end": df.at[lookback_end, "date"].date(),
        }
        for h in range(1, 11):
            j = i + h
            if j < len(df):
                fwd_date = df.at[j, "date"].date()
                fwd_close = df.at[j, "close"]
                ret = round(float(fwd_close / signal_close * 100 - 100), 3)
            else:
                fwd_date, ret = None, None
            row[f"fwd_{h}d_date"] = fwd_date
            row[f"fwd_{h}d_return_pct"] = ret
        rows.append(row)

    out = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    print(f"Wrote {OUT}  ({len(out)} signals, {len(out.columns)} columns)")
    print(out.head().to_string(index=False))


if __name__ == "__main__":
    main()
