"""Plot NIFTY close with signal markers for each winning strategy."""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CFG
from backtest import load

OUT = Path("reports/winners")

PLOTS = [
    ("daily", "nr7_then_range_expansion_breakout", "daily"),
    ("mixed", "nr7_breakout_with_weekly_silent_top_avoidance", "daily"),
    ("mixed", "nr7_breakout_with_weekly_silent_top_avoidance_and_compression_quality", "daily"),
    ("mixed", "nr7_expansion_breakout_with_weekly_uptrend_filter", "daily"),
    ("weekly", "weekly_silent_top_exhaustion", "weekly"),
]


def main() -> None:
    daily = load(CFG["index"], "daily")
    weekly = load(CFG["index"], "weekly")
    daily["date"] = pd.to_datetime(daily["date"])
    weekly["date"] = pd.to_datetime(weekly["date"])

    for tf, name, base_tf in PLOTS:
        sigfile = OUT / f"signals_{tf}_{name}.csv"
        sig = pd.read_csv(sigfile)
        sig["date"] = pd.to_datetime(sig["date"])
        base = daily if base_tf == "daily" else weekly

        fig, ax = plt.subplots(figsize=(13, 5))
        ax.plot(base["date"], base["close"], color="#444", lw=0.7, label=f"{CFG['index']} {base_tf}")
        ax.scatter(sig["date"], sig["close"], color="red", s=18, zorder=5,
                   label=f"signals (n={len(sig)})")
        ax.set_title(f"{tf}/{name}")
        ax.set_xlabel("Date")
        ax.set_ylabel("Close")
        ax.legend(loc="upper left")
        ax.grid(alpha=0.3)
        out = OUT / f"plot_{tf}_{name}.png"
        fig.tight_layout()
        fig.savefig(out, dpi=110)
        plt.close(fig)
        print(f"Wrote {out}")


if __name__ == "__main__":
    main()
