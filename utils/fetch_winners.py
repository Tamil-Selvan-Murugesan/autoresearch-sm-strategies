import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CFG
from backtest import query_winners

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
pd.set_option("display.max_colwidth", 80)

winners = query_winners(
    min_win_rate=CFG["min_win_rate"],
    min_abs_mean=CFG["min_abs_mean"],
    min_signals=CFG["min_signals"],
    limit=CFG.get("max_winners", 50),
)

print(f"DB: {CFG['db_path']}")
print(f"Filter: win_rate>={CFG['min_win_rate']}%, |mean|>={CFG['min_abs_mean']}%, "
    f"signals>={CFG['min_signals']}")
print(f"Found {len(winners)} winners\n")

if not winners.empty:
    cols = ["timeframe", "strategy", "horizon", "direction",
            "signals", "win_rate", "mean", "avg_win", "avg_loss", "expectancy"]
    print(winners[cols].to_string(index=False))
    winners.to_csv("winners.csv", index=False)
    print(f"\nFull rows (incl. params) written to winners.csv")
else:
    print("No winners.")