def nr7_expansion_breakout_with_weekly_uptrend_filter(df_5min, df_daily, df_weekly):
    import numpy as np
    import pandas as pd

    params = {
        "signal": "NR7 + next-day range expansion breakout (long), filtered by weekly uptrend regime (weekly close > 20w SMA and > 4 weeks ago).",
        "entry_timeframe": "daily",
        "exit_timeframe": "daily",
        "entry_condition": "Day N-1 NR7; Day N range > 2x prior range; close in top 30%; weekly close > 20w SMA and > weekly close 4w ago",
        "exit_condition": "forward horizons 1,2,5,10 days",
        "derived_from": ["nr7_then_range_expansion_breakout", "weekly_silent_top_exhaustion"],
        "refinement_type": "filtered",
        "nr7_lookback": 7,
        "expansion_multiplier": 2.0,
        "close_position_threshold": 0.7,
        "weekly_ma_lookback": 20,
        "weekly_momentum_lookback": 4,
        "forward_days": [1, 2, 5, 10],
    }

    d = df_daily.sort_values("date").reset_index(drop=True).copy()
    w = df_weekly.sort_values("date").reset_index(drop=True).copy()

    # Daily NR7 + expansion breakout
    d["range"] = d["high"] - d["low"]
    d["nr7"] = d["range"] == d["range"].rolling(7).min()
    prev_range = d["range"].shift(1)
    prev_nr7 = d["nr7"].shift(1).fillna(False)
    close_pos = np.where(d["range"] > 0, (d["close"] - d["low"]) / d["range"], 0.0)
    expansion = (d["range"] > 2.0 * prev_range) & (close_pos >= 0.7)
    daily_signal = prev_nr7 & expansion

    # Weekly regime
    w["sma20"] = w["close"].rolling(20).mean()
    w["close_4w_ago"] = w["close"].shift(4)
    w["weekly_bull"] = (w["close"] > w["sma20"]) & (w["close"] > w["close_4w_ago"])

    # Align weekly regime to daily dates: use most recent COMPLETED week (shift by 1 to avoid lookahead)
    w_aligned = w[["date", "weekly_bull"]].copy()
    w_aligned["weekly_bull_prev"] = w_aligned["weekly_bull"].shift(1)
    weekly_series = w_aligned.set_index("date")["weekly_bull_prev"]

    # Reindex weekly to daily via merge_asof (last weekly date <= daily date)
    d_sorted = d[["date"]].copy()
    w_sorted = weekly_series.reset_index().rename(columns={"date": "wdate"}).sort_values("wdate")
    merged = pd.merge_asof(
        d_sorted.sort_values("date"),
        w_sorted,
        left_on="date",
        right_on="wdate",
        direction="backward",
    )
    d["weekly_bull"] = merged["weekly_bull_prev"].fillna(False).values

    mask = daily_signal & d["weekly_bull"].astype(bool)

    for n in [1, 2, 5, 10]:
        d[f"fwd_{n}d"] = d["close"].shift(-n) / d["close"] * 100 - 100

    cols = ["date", "close", "fwd_1d", "fwd_2d", "fwd_5d", "fwd_10d"]
    result = d.loc[mask, cols].copy().reset_index(drop=True)

    return params, result
