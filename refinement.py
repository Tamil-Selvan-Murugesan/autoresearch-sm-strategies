"""Independent refinement loop — runs concurrently with the main LangGraph graph."""

import asyncio
import json
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from backtest import count_strategies, get_attempts, log_run, publish, query_winners, summarize
from backtest_multi import execute_multi_strategies
from config import CFG
from graph import _parse_strategies

_INDEX = CFG["index"]
_TIMEFRAMES = CFG["timeframes"]
_STRATS_PER_REFINE = CFG["strategies_per_refinement"]
_POLL_INTERVAL = CFG["refinement_poll_interval"]
_THRESHOLD_STEP = CFG["refinement_threshold_step"]
_MIN_WIN_RATE = CFG["min_win_rate"]
_MIN_ABS_MEAN = CFG["min_abs_mean"]
_MIN_SIGNALS = CFG["min_signals"]
_MAX_WINNERS = CFG["max_winners"]


def _count_main_strategies() -> int:
    """Count total strategies from the main timeframes (excludes mixed)."""
    return sum(count_strategies(tf) for tf in _TIMEFRAMES)


def _get_winner_descriptions() -> list[dict]:
    """Fetch winners and format them for the LLM."""
    winners = query_winners(
        min_win_rate=_MIN_WIN_RATE,
        min_abs_mean=_MIN_ABS_MEAN,
        min_signals=_MIN_SIGNALS,
        limit=_MAX_WINNERS,
    )
    if winners.empty:
        return []

    descriptions = []
    for _, row in winners.iterrows():
        descriptions.append(
            {
                "strategy": row["strategy"],
                "timeframe": row["timeframe"],
                "params": json.loads(row["params"]),
                "win_rate": row["win_rate"],
                "avg_win": row["avg_win"],
                "avg_loss": row["avg_loss"],
                "expectancy": row["expectancy"],
                "direction": row["direction"],
                "signals": row["signals"],
            }
        )
    return descriptions


def _refine_once(llm: BaseChatModel, winner_descriptions: list[dict]) -> None:
    """Generate cross-timeframe strategies, execute, log, and publish."""
    prompt = Path("prompts/planner_refinement.md").read_text().replace("{n}", str(_STRATS_PER_REFINE))

    # Planner memory: every prior mixed attempt, including failures, so the
    # LLM doesn't regenerate something that was already tried or that crashed.
    attempts = get_attempts("mixed", limit=500)
    tested_summary = "No mixed strategies attempted yet."
    if not attempts.empty:
        lines = []
        for _, row in attempts.iterrows():
            try:
                p = json.loads(row["params"])
            except (TypeError, json.JSONDecodeError):
                p = {}
            signal = str(p.get("signal", ""))[:180]
            if row["status"] == "error":
                lines.append(f"- {row['strategy']} [FAILED: {row['error']}] — {signal}")
            elif row["status"] == "zero_signals":
                lines.append(f"- {row['strategy']} [0 signals] — {signal}")
            else:
                lines.append(f"- {row['strategy']} [{row['signals']} signals] — {signal}")
        tested_summary = "\n".join(lines)

    response = llm.invoke(
        [
            SystemMessage(content=prompt),
            HumanMessage(
                content=(
                    f"Winning strategies:\n{json.dumps(winner_descriptions, indent=2)}\n\n"
                    f"Previously tested mixed strategies:\n{tested_summary}\n\n"
                    f"Generate {_STRATS_PER_REFINE} new refined strategies."
                )
            ),
        ]
    )

    strategy_defs = _parse_strategies(response.content)

    # Execute with backtest_multi.py — all 3 timeframes
    results, data_from, data_to = execute_multi_strategies(_INDEX, strategy_defs)
    strategy_names = [s["name"] for s in strategy_defs]

    # Log to DB
    log_run(
        _INDEX, "mixed", strategy_names, results,
        data_from=data_from, data_to=data_to,
    )

    # Publish to GitHub
    publish(
        timeframe="mixed",
        strategy_names=strategy_names,
        run_results=results,
    )

    # Print summary
    for name, (params, result_df) in zip(strategy_names, results):
        fwd_cols = [c for c in result_df.columns if c.startswith("fwd_")]
        stats = {col: summarize(result_df[col]) for col in fwd_cols}
        print(f"  [refinement] {name}: signals={len(result_df)}, stats={stats}")


async def refinement_loop(
    llm: BaseChatModel,
    stop_event: asyncio.Event,
) -> None:
    """Independent refinement loop. Runs concurrently with the main graph."""
    next_threshold = _THRESHOLD_STEP

    while not stop_event.is_set():
        total = _count_main_strategies()

        if total < next_threshold:
            print(
                f"[refinement] {total} strategies logged, waiting for {next_threshold}. "
                f"Polling again in {_POLL_INTERVAL}s."
            )
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        # Threshold reached — refresh winner pool
        print(
            f"[refinement] Threshold {next_threshold} reached ({total} strategies). "
            f"Refreshing winner pool."
        )
        winner_descriptions = _get_winner_descriptions()

        if not winner_descriptions:
            print("[refinement] No winners yet. Staying idle.")
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        # Inner loop: keep generating+executing batches until next threshold
        while not stop_event.is_set():
            print(
                f"[refinement] Generating {_STRATS_PER_REFINE} cross-timeframe strategies "
                f"from {len(winner_descriptions)} winners..."
            )
            _refine_once(llm, winner_descriptions)

            # Check if main loop has produced enough for the next threshold
            current_total = _count_main_strategies()
            if current_total >= next_threshold + _THRESHOLD_STEP:
                next_threshold += _THRESHOLD_STEP
                print(
                    f"[refinement] Main loop hit {current_total}. "
                    f"New threshold: {next_threshold}. Refreshing winners."
                )
                break

            # Small pause to avoid hammering the LLM
            await asyncio.sleep(5)

    print("[refinement] Main loop finished. Refinement loop exiting.")
