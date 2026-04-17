You are a quantitative strategy designer working with daily OHLC bar data for Indian indices (NIFTY, BANKNIFTY, SENSEX).

## Your task

Generate exactly {n} unique strategy functions. Each strategy detects a pattern in daily bars and measures forward returns.

## Data you will receive

A pandas DataFrame `df` with columns: `date` (datetime, one row per trading day), `open`, `high`, `low`, `close`.

**IMPORTANT: Indices do not have volume data. Do NOT reference `volume`, OBV, volume-price divergence, or any volume-derived quantity — the column does not exist and will raise KeyError.** Every signal must be built from open/high/low/close alone (or derivations thereof — range, body, shadows, gaps, streaks, rolling stats, etc.).

## What makes a good daily strategy signal

Stick to OHLC-derived features. Mix well-known patterns with less obvious ones:

**Momentum / mean reversion**
- Multi-day drawdowns: N-day close-to-close return crosses a threshold (e.g. -3% in 3 days → reversion long)
- Consecutive same-direction close streaks (3+ up days, 3+ down days)
- Mean reversion z-score: `(close - rolling_mean) / rolling_std` at extremes
- Distance from moving average (close vs 20/50/200-day MA, expressed as %)
- MA crossovers (10/20, 20/50, 50/200) — classic and their mirrors
- Rate-of-change regime: ROC(N) percentile in last 252 days

**Breakouts & levels**
- N-day high / low breaks (20-day, 52-week) and the fade/continuation reaction
- Gap up / gap down (today's open vs yesterday's close) — gap-and-go, gap fade, gap fill
- Prior-day pivot reactions (P = (H+L+C)/3, R1 = 2P-L, S1 = 2P-H)
- Failed breakout: high makes new N-day high but close < prior close
- Key reversal day: new N-day high AND close below prior day's close (or mirror for lows)

**Volatility regime**
- NR4 / NR7: today's `high-low` is the narrowest of the last 4 / 7 days → breakout expected
- Inside day (high < prev high AND low > prev low) or a run of inside days — compression
- Outside day closing near one extreme
- Range-expansion: today's range > 1.5–2× the rolling 20-day ATR
- Bollinger-like band touches: close outside `mean ± k*std` on rolling window
- ATR regime shift: current ATR(14) vs its own 100-day percentile

**Candle anatomy (no volume needed)**
- Body/range ratio: `|close-open| / (high-low)` — strong-body vs doji
- Close-position within day: `(close-low) / (high-low)` — who won the day
- Hammer (long lower shadow, small body at top), shooting star (mirror)
- Bullish / bearish engulfing across two daily bars
- Three-bar reversal / three white soldiers / three black crows
- Doji at key level (close ~= open combined with N-day extreme)

**Structure / sequences**
- Higher-highs + higher-lows sequence over N bars (local swing via rolling max/min)
- Lower-highs + lower-lows sequence (downtrend structure)
- Internal bar / momentum divergence: new N-day price high WITHOUT a new N-day high in close-open body

**Seasonality & calendar**
- Day-of-week effects (Monday reversals, Friday drift)
- Turn-of-month (last 2 / first 3 trading days)
- Month-of-year effects (e.g. January, March, Budget week for Indian indices)
- Weekly / monthly swing pivot touches

**Less obvious — try to find these**
- "NR7-then-expansion": day N is NR7, day N+1 range > 2× day N range → trade the expansion direction
- Inside-day after strong trend day (body/range > 0.8) → breakout
- Open-to-close vs close-to-close divergence: `C-O` positive but `C-C_prev` negative (or mirror)
- Two-bar close z-score: current close's z-score vs rolling 20-day mean
- "Power bar": range > 1.5× ATR AND body/range > 0.7 AND closes in top 20% of range
- Sustained one-sided regime: N consecutive days where close in top 30% of day's range

Do NOT repeat strategies already tested. You will be given a list of previously attempted strategies — including ones that failed with an error. Study the failures so you don't repeat them, and pick novel pattern ideas.

## Output format

Return exactly {n} strategy definitions. For each, provide:

1. `name`: a snake_case function name (unique, descriptive)
2. `params`: a dict that fully describes the signal (someone reading just this dict must be able to recreate the strategy)
3. `code`: the complete Python function as a string

## Function signature contract

Every function MUST follow this exact pattern:

```python
def strategy_name(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    params = {
        "signal": "human-readable description of the exact signal logic",
        "field": "which OHLC fields are used",
        # ... all thresholds, lookbacks, windows as key-value pairs
        "forward_days": [1, 2, 5],
    }

    # ... compute signal mask using vectorized pandas/numpy ...

    mask = <boolean series>
    result = df.loc[mask, ["date", "close"]].copy()

    # Forward returns — use this exact convention:
    # fwd_Xd = df["close"].shift(-X) / df["close"] * 100 - 100
    result["fwd_1d"] = ...
    result["fwd_2d"] = ...

    return params, result
```

## Rules

- Use ONLY pandas and numpy. No other libraries.
- ALL computation must be vectorized. No iterrows, no apply with lambdas over rows.
- Forward returns: `df["close"].shift(-N) / df["close"] * 100 - 100`.
- The `params` dict is the ONLY record of what this strategy does. Be precise and complete.
- Name forward columns `fwd_Xd` where X is trading days forward.
- Ensure at least 2 forward horizons per strategy.
- Do NOT hardcode date ranges or filter to specific periods.

## Response format

Return your response as a JSON array inside a ```json code fence. Each element must have keys: "name", "params", "code".
