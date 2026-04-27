"""Entry point — launches main graph + refinement loop concurrently."""

import asyncio

from config import CFG
from graph import build_graph
from refinement import refinement_loop
from state import GraphState

# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

_LLM_CFG = CFG["llm"]


def _make_llm():
    provider = _LLM_CFG["provider"]
    temperature = _LLM_CFG.get("temperature", 1)
    api_key = _LLM_CFG.get("api_key") or None  # None → fall back to env var

    if provider == "azure_openai":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_deployment=_LLM_CFG["model"],
            api_version=_LLM_CFG.get("api_version", "2024-12-01-preview"),
            azure_endpoint=_LLM_CFG.get("azure_endpoint") or None,
            api_key=api_key,
            temperature=temperature,
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=_LLM_CFG["model"],
            api_key=api_key,
            temperature=temperature,
        )
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=_LLM_CFG["model"],
            api_key=api_key,
            temperature=temperature,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


# ---------------------------------------------------------------------------
# Build config
# ---------------------------------------------------------------------------

llm = _make_llm()
_timeframes = CFG["timeframes"]

llm_config = {f"planner_{tf}": llm for tf in _timeframes}
llm_config["planner_refinement"] = llm

graph = build_graph(llm_config)

initial_state: GraphState = {
    "iteration": 0,
    "should_stop": False,
}
# Initialise per-timeframe state fields
for tf in _timeframes:
    initial_state[f"pending_{tf}"] = []
    initial_state[f"results_{tf}"] = []
    initial_state[f"count_{tf}"] = 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    stop_event = asyncio.Event()

    async def run_main_graph():
        loop = asyncio.get_event_loop()
        final_state = await loop.run_in_executor(None, graph.invoke, initial_state)
        print(f"Main loop completed in {final_state['iteration']} iterations")
        counts = ", ".join(f"{tf}={final_state.get(f'count_{tf}', '?')}" for tf in _timeframes)
        print(f"Strategies: {counts}")
        stop_event.set()

    await asyncio.gather(
        run_main_graph(),
        refinement_loop(
            llm=llm_config["planner_refinement"],
            stop_event=stop_event,
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
