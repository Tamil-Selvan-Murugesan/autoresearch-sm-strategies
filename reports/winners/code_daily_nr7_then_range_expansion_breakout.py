def nr7_then_range_expansion_breakout(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    import pandas as pd
    import numpy as np

    params = {
        "signal": "Day N is an NR7 bar (its high-low range is the narrowest of the last 7 days, inclusive). Day N+1 confirms with a range > 2.0x day N's range AND closes in the top 30% of day N+1's range (bullish expansion). Signal fires on day N+1 (close of expansion day), trading the breakout direction long.",
        "field": "high, low, close",
        "nr7_lookback": 7,
        "expansion_multiplier": 2.0,
        "close_position_threshold": 0.7,
        "direction": "long on bullish expansion (close in top 30% of day's range)",
        "forward_days": [1, 2, 5, 10],
    }

    rng = df["high"] - df["low"]
    # NR7: today's range is the smallest of last 7 bars (inclusive)
    min_range_7 = rng.rolling(window=7, min_periods=7).min()
    is_nr7 = rng == min_range_7

    # Yesterday was NR7
    prev_is_nr7 = is_nr7.shift(1)
    prev_range = rng.shift(1)

    # Today's range > 2x yesterday's range
    range_expansion = rng > (params["expansion_multiplier"] * prev_range)

    # Today's close position within today's range (0=at low, 1=at high)
    close_pos = (df["close"] - df["low"]) / rng.replace(0, np.nan)
    bullish_close = close_pos >= params["close_position_threshold"]

    mask = prev_is_nr7.fillna(False) & range_expansion.fillna(False) & bullish_close.fillna(False)

    result = df.loc[mask, ["date", "close"]].copy()
    result["fwd_1d"] = (df["close"].shift(-1) / df["close"] * 100 - 100).loc[mask]
    result["fwd_2d"] = (df["close"].shift(-2) / df["close"] * 100 - 100).loc[mask]
    result["fwd_5d"] = (df["close"].shift(-5) / df["close"] * 100 - 100).loc[mask]
    result["fwd_10d"] = (df["close"].shift(-10) / df["close"] * 100 - 100).loc[mask]

    return params, result
