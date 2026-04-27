def weekly_silent_top_exhaustion(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    import numpy as np
    import pandas as pd

    params = {
        "signal": "Silent top exhaustion: current week prints a new 52-week high, but the weekly range is in the bottom 30th percentile of the last 26 weeks AND the weekly body (|close-open|) is less than 30% of the weekly range. Indicates a new high made on narrow-range, small-body (indecisive) action — potential exhaustion top.",
        "field": "open, high, low, close",
        "high_lookback_weeks": 52,
        "range_percentile_lookback_weeks": 26,
        "range_percentile_threshold": 0.30,
        "body_to_range_max_ratio": 0.30,
        "forward_weeks": [1, 2, 4, 8],
    }

    df = df.copy()
    weekly_range = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()

    # New 52-week high condition: current high is the max of the last 52 weeks (inclusive)
    rolling_max_high = df["high"].rolling(window=52, min_periods=52).max()
    new_52w_high = df["high"] >= rolling_max_high

    # Range percentile rank over last 26 weeks (rank of current range; <= threshold means narrow)
    range_pct_rank = weekly_range.rolling(window=26, min_periods=26).apply(
        lambda x: (x <= x[-1]).sum() / len(x), raw=True
    )
    narrow_range = range_pct_rank <= 0.30

    # Small body relative to range
    body_ratio = body / weekly_range.replace(0, np.nan)
    small_body = body_ratio < 0.30

    mask = new_52w_high & narrow_range & small_body
    mask = mask.fillna(False)

    result = df.loc[mask, ["date", "close"]].copy()
    result["fwd_1d"] = (df["close"].shift(-1) / df["close"] * 100 - 100).loc[mask]
    result["fwd_2d"] = (df["close"].shift(-2) / df["close"] * 100 - 100).loc[mask]
    result["fwd_4d"] = (df["close"].shift(-4) / df["close"] * 100 - 100).loc[mask]
    result["fwd_8d"] = (df["close"].shift(-8) / df["close"] * 100 - 100).loc[mask]

    return params, result
