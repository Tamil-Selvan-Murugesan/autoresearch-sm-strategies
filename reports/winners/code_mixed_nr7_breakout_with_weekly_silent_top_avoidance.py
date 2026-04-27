def nr7_breakout_with_weekly_silent_top_avoidance(df_5min, df_daily, df_weekly):
    import numpy as np
    import pandas as pd

    params = {
        "signal": "NR7 + range expansion breakout long, avoiding weekly silent-top-exhaustion regimes, with weekly > 10w SMA filter",
        "entry_timeframe": "daily",
        "exit_timeframe": "daily",
        "entry_condition": "NR7 day N-1; day N range>2x prior, close top 30%; prior week NOT silent top exhaustion; weekly close > 10w SMA",
        "exit_condition": "forward 1,2,5,10 days",
        "derived_from": ["nr7_then_range_expansion_breakout", "nr7_expansion_breakout_with_weekly_uptrend_filter", "weekly_silent_top_exhaustion"],
        "refinement_type": "filtered",
        "forward_days": [1, 2, 5, 10],
    }

    d = df_daily.sort_values("date").reset_index(drop=True).copy()
    w = df_weekly.sort_values("date").reset_index(drop=True).copy()

    # Daily NR7
    d["range"] = d["high"] - d["low"]
    d["min7"] = d["range"].rolling(7).min()
    d["is_nr7"] = d["range"] == d["min7"]

    # Expansion day (today): range > 2x prior, close in top 30%
    prior_range = d["range"].shift(1)
    prior_nr7 = d["is_nr7"].shift(1).fillna(False)
    close_pos = (d["close"] - d["low"]) / d["range"].replace(0, np.nan)
    expansion = (d["range"] > 2.0 * prior_range) & (close_pos >= 0.7) & prior_nr7

    # Weekly silent top exhaustion
    w["w_range"] = w["high"] - w["low"]
    w["w_body"] = (w["close"] - w["open"]).abs()
    w["hi52"] = w["high"].rolling(52).max()
    w["new_high"] = w["high"] >= w["hi52"]
    w["range_pct"] = w["w_range"].rolling(26).rank(pct=True)
    w["body_ratio"] = w["w_body"] / w["w_range"].replace(0, np.nan)
    w["silent_top"] = w["new_high"] & (w["range_pct"] <= 0.30) & (w["body_ratio"] < 0.30)
    w["sma10"] = w["close"].rolling(10).mean()
    w["above_sma10"] = w["close"] > w["sma10"]

    # Map weekly to daily via ffill on date
    w_idx = w.set_index("date")
    silent_series = w_idx["silent_top"].astype(float)
    above_series = w_idx["above_sma10"].astype(float)

    daily_dates = d["date"]
    silent_aligned = silent_series.reindex(
        pd.Index(daily_dates).union(silent_series.index)
    ).sort_index().ffill().reindex(daily_dates).values
    above_aligned = above_series.reindex(
        pd.Index(daily_dates).union(above_series.index)
    ).sort_index().ffill().reindex(daily_dates).values

    silent_mask = (silent_aligned == 1.0)
    above_mask = (above_aligned == 1.0)

    entry_mask = expansion.fillna(False).values & (~silent_mask) & above_mask

    # Forward returns
    for h in [1, 2, 5, 10]:
        d[f"fwd_{h}d"] = d["close"].shift(-h) / d["close"] * 100 - 100

    result = d.loc[entry_mask, ["date", "close", "fwd_1d", "fwd_2d", "fwd_5d", "fwd_10d"]].copy()
    result = result.dropna(subset=["fwd_1d", "fwd_2d", "fwd_5d", "fwd_10d"]).reset_index(drop=True)

    return params, result
