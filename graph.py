"""LangGraph graph definition and main loop node functions."""

import json
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from backtest import (
    count_strategies,
    execute_strategies,
    get_attempts,
    log_run,
    publish,
    summarize,
)
from config import CFG
from state import GraphState, StrategyDef

_INDEX = CFG["index"]
_MAX_PER_TF = CFG["max_strategies_per_timeframe"]
_STRATS_PER_ITER = CFG["strategies_per_iteration"]
_TIMEFRAMES = CFG["timeframes"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_prompt(timeframe: str, n: int) -> str:
    """Read the system prompt for a planner node, injecting strategy count."""
    template = Path(f"prompts/planner_{timeframe}.md").read_text()
    return template.replace("{n}", str(n))


def _get_tested_strategies(timeframe: str) -> str:
    """Summarize prior attempts for the planner's memory.

    Includes successes, zero-signal runs, and failures. Failures are marked
    with their error so the LLM doesn't repeat the same mistake.
    """
    attempts = get_attempts(timeframe, limit=500)
    if attempts.empty:
        return "No strategies attempted yet for this timeframe."

    lines = []
    for _, row in attempts.iterrows():
        try:
            params = json.loads(row["params"])
        except (TypeError, json.JSONDecodeError):
            params = {}
        signal = str(params.get("signal", ""))[:180]
        if row["status"] == "error":
            lines.append(f"- {row['strategy']} [FAILED: {row['error']}] — {signal}")
        elif row["status"] == "zero_signals":
            lines.append(f"- {row['strategy']} [0 signals] — {signal}")
        else:
            lines.append(f"- {row['strategy']} [{row['signals']} signals] — {signal}")
    return "\n".join(lines)


def _parse_strategies(response_text: str) -> list[StrategyDef]:
    """Parse LLM response into list of StrategyDef.

    Expect the LLM to return a JSON array:
    [
        {
            "name": "strategy_name",
            "params": { ... },
            "code": "def strategy_name(df: pd.DataFrame) -> ..."
        },
        ...
    ]
    """
    # Extract JSON from response (may be wrapped in markdown code fences)
    text = response_text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    strategies = json.loads(text)
    return [StrategyDef(**s) for s in strategies]


# ---------------------------------------------------------------------------
# Planner nodes
# ---------------------------------------------------------------------------


def make_planner_node(timeframe: str, llm: BaseChatModel):
    """Factory: returns a planner node function for the given timeframe."""

    def planner_node(state: GraphState) -> dict:
        # Skip if this timeframe has reached the limit
        if count_strategies(timeframe) >= _MAX_PER_TF:
            print(f"[planner/{timeframe}] Reached {_MAX_PER_TF} strategies, skipping.")
            return {f"pending_{timeframe}": []}

        system_prompt = _load_prompt(timeframe, _STRATS_PER_ITER)
        tested = _get_tested_strategies(timeframe)

        response = llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(
                    content=(
                        f"Previously tested strategies:\n{tested}\n\n"
                        f"Generate {_STRATS_PER_ITER} new strategies."
                    )
                ),
            ]
        )

        # Parse the LLM response into StrategyDef list
        strategy_defs = _parse_strategies(response.content)
        print(
            f"[planner/{timeframe}] Generated {len(strategy_defs)} strategies: "
            f"{[s['name'] for s in strategy_defs]}"
        )
        return {f"pending_{timeframe}": strategy_defs}

    return planner_node


# ---------------------------------------------------------------------------
# Execution + logging nodes
# ---------------------------------------------------------------------------


def make_execute_and_log_node(timeframe: str):
    """Combined execution + logging node per timeframe."""

    def node(state: GraphState) -> dict:
        pending = state.get(f"pending_{timeframe}", [])
        if not pending:
            return {f"results_{timeframe}": []}

        # Execute
        results, data_from, data_to = execute_strategies(_INDEX, timeframe, pending)
        strategy_names = [s["name"] for s in pending]

        # Log to DB
        iteration = state.get("iteration", 0) + 1
        run_id = log_run(
            _INDEX, timeframe, strategy_names, results,
            iteration=iteration, data_from=data_from, data_to=data_to,
            strategy_defs=pending,
        )
        print(f"[exec/{timeframe}] iter={iteration} run_id={run_id} with {len(strategy_names)} strategies.")

        # Summarize for state (no DataFrames)
        summaries = []
        for name, (params, result_df) in zip(strategy_names, results):
            fwd_cols = [c for c in result_df.columns if c.startswith("fwd_")]
            stats = {col: summarize(result_df[col]) for col in fwd_cols}
            summaries.append({"name": name, "params": params, "stats": stats})

        # Publish to GitHub
        publish(
            timeframe=timeframe,
            strategy_names=strategy_names,
            run_results=results,
        )

        return {f"results_{timeframe}": summaries}

    return node


# ---------------------------------------------------------------------------
# Stop check
# ---------------------------------------------------------------------------


def stop_check(state: GraphState) -> dict:
    """Check if all timeframes have reached the strategy limit."""
    counts = {tf: count_strategies(tf) for tf in _TIMEFRAMES}
    should_stop = all(c >= _MAX_PER_TF for c in counts.values())

    counts_str = ", ".join(f"{tf}={c}" for tf, c in counts.items())
    print(
        f"[stop_check] iteration={state['iteration'] + 1}, "
        f"{counts_str}, stop={should_stop}"
    )

    result = {
        "should_stop": should_stop,
        "iteration": state["iteration"] + 1,
    }
    for tf, c in counts.items():
        result[f"count_{tf}"] = c
    return result


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router(state: GraphState) -> dict:
    """No-op node that fans out to all planners in parallel."""
    return {}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(llm_config: dict[str, BaseChatModel]) -> StateGraph:
    """Assemble and compile the main LangGraph graph."""
    graph = StateGraph(GraphState)

    graph.add_node("router", router)

    for tf in _TIMEFRAMES:
        graph.add_node(f"plan_{tf}", make_planner_node(tf, llm_config[f"planner_{tf}"]))
        graph.add_node(f"exec_{tf}", make_execute_and_log_node(tf))

    graph.add_node("stop_check", stop_check)

    # ── Edges ──

    graph.add_edge(START, "router")
    for tf in _TIMEFRAMES:
        graph.add_edge("router", f"plan_{tf}")
        graph.add_edge(f"plan_{tf}", f"exec_{tf}")
        graph.add_edge(f"exec_{tf}", "stop_check")

    graph.add_conditional_edges(
        "stop_check",
        lambda state: "end" if state["should_stop"] else "continue",
        {"end": END, "continue": "router"},
    )

    return graph.compile()
