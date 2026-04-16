# autoresearch-sm-strategies

Inspired by Karpathy's auto-research concept, applied to stock market backtesting. An LLM agent loop that autonomously generates, executes, and refines trading strategies against historical Indian index data (NIFTY, BANKNIFTY, SENSEX).

## How it works

The system runs two concurrent loops built on LangGraph:

**Main loop** — Fans out to 3 parallel planner agents (5-min, daily, weekly). Each planner asks the LLM to generate 5 novel strategy functions per iteration. The strategies are written to Python files, dynamically imported, executed against OHLCV parquet data, and results are logged to SQLite. Repeats until 50 strategies per timeframe are tested.

**Refinement loop** — Polls the database every 3 minutes. Once enough strategies are logged, it queries for winners (strategies with |expectancy| >= 0.15% and >= 30 signals), then generates cross-timeframe strategies that combine insights from the best performers.

All results are committed and pushed to GitHub automatically.

## Setup

```bash
uv sync
```

### Data

Place OHLCV parquet files (columns: `date`, `open`, `high`, `low`, `close`, `volume`) in:

```
data/5min/NIFTY_5min.parquet
data/daily/NIFTY_daily.parquet
data/weekly/NIFTY_weekly.parquet
```

### Environment variables

```bash
export AZURE_OPENAI_API_KEY="your-key"
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_VERSION="2024-12-01-preview"
```

Update the `azure_deployment` in `run.py` if your deployment name differs from `gpt-4.1`.

## Run

```bash
uv run python run.py
```

Results are logged to `logs/backtest.db` (SQLite). Strategy code is written to `strategies/`.

## Project structure

```
backtest.py          Core single-timeframe engine (load, execute, log, publish)
backtest_multi.py    Multi-timeframe engine for refinement strategies
graph.py             LangGraph graph definition and node functions
refinement.py        Independent async refinement loop
state.py             Graph state schema
run.py               Entry point
prompts/             System prompts for each planner agent
data/                OHLCV parquet files (not committed)
logs/                SQLite database
strategies/          Auto-generated strategy files (overwritten each iteration)
```
