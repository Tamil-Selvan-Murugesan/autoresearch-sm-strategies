from typing import TypedDict


class StrategyDef(TypedDict):
    """A single strategy as produced by a planner node."""

    name: str  # function name, e.g. "gap_down_reversal"
    params: dict  # full param dict for logging
    code: str  # the Python function body as a string


class GraphState(TypedDict):
    """State for the MAIN loop only. Refinement runs independently."""

    # Current iteration's work
    pending_5min: list[StrategyDef]
    pending_daily: list[StrategyDef]
    pending_weekly: list[StrategyDef]

    # Execution results for current iteration (reset each loop)
    results_5min: list[tuple[dict, dict]]  # list of (params, {horizon: summary_stats})
    results_daily: list[tuple[dict, dict]]
    results_weekly: list[tuple[dict, dict]]

    # Cumulative counters (increment across iterations)
    count_5min: int
    count_daily: int
    count_weekly: int

    # Control
    iteration: int
    should_stop: bool
