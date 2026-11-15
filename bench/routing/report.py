"""Summarise bench/routing/results/results.csv as markdown tables.

    uv run python bench/routing/report.py [--results ...] [--out bench/routing/results/report.md]

Gap = (ref - score) / ref in %, negative when a solver beats the collected reference. For ILS
"avg" is the mean over seeds and "best" the best seed; OR-Tools runs once, so both are the same.
Decision rule (agreed before the run): with 100% feasible solutions, take ILS unless its mean
avg gap at the time limit is at least DECISION_MARGIN_PP points worse than OR-Tools'.
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

DECISION_MARGIN_PP = 1.0
RESULTS = Path(__file__).with_name("results")


def family(instance: str) -> str:
    if instance.startswith("pr"):
        return "pr01-10" if int(instance[2:]) <= 10 else "pr11-20"
    return re.match(r"[a-z]+\d", instance).group(0)  # c1, c2, r1, r2, rc1, rc2


def gap(ref: int, score: str) -> float:
    return 100 * (ref - int(score or 0)) / ref  # no solution yet counts as score 0


def summarise(rows: list[dict]) -> dict[str, float]:
    by_case: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        by_case[(row["instance"], row["m"], row["set"])].append(row)
    avg, best, at1, at3, hits = [], [], [], [], 0
    for runs in by_case.values():
        ref = int(runs[0]["ref"])
        scores = [int(r["score"]) for r in runs]
        avg.append(mean(gap(ref, s) for s in map(str, scores)))
        best.append(gap(ref, str(max(scores))))
        at1.append(mean(gap(ref, r["score_1s"]) for r in runs))
        at3.append(mean(gap(ref, r["score_3s"]) for r in runs))
        hits += max(scores) >= ref
    return {
        "cases": len(by_case),
        "avg_gap": mean(avg),
        "best_gap": mean(best),
        "gap_1s": mean(at1),
        "gap_3s": mean(at3),
        "hit_ref": hits,
        "feasible_pct": 100 * mean(int(r["feasible"]) for r in rows),
        "over_proven": sum(int(r["over_proven"]) for r in rows),
        "time_to_best": mean(float(r["time_to_best_s"] or 0) for r in rows),
    }


def table(groups: dict[tuple, list[dict]], key_names: list[str]) -> list[str]:
    """One row per group key (*key_names, solver)."""
    header = [
        *key_names,
        "solver",
        "cases",
        "avg gap %",
        "best gap %",
        "gap@1s",
        "gap@3s",
        "hit ref",
        "feasible %",
        "t best (s)",
    ]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key in sorted(groups):
        s = summarise(groups[key])
        cells = [
            *map(str, key),
            f"{s['cases']}",
            f"{s['avg_gap']:.2f}",
            f"{s['best_gap']:.2f}",
            f"{s['gap_1s']:.2f}",
            f"{s['gap_3s']:.2f}",
            f"{s['hit_ref']}",
            f"{s['feasible_pct']:.1f}",
            f"{s['time_to_best']:.2f}",
        ]
        out.append("| " + " | ".join(cells) + " |")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, default=RESULTS / "results.csv")
    parser.add_argument("--out", type=Path, default=RESULTS / "report.md")
    args = parser.parse_args()
    rows = list(csv.DictReader(args.results.open(encoding="utf-8")))

    overall: dict[tuple, list[dict]] = defaultdict(list)
    by_family: dict[tuple, list[dict]] = defaultdict(list)
    by_m: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        overall[(row["set"], row["solver"])].append(row)
        by_family[(row["set"], family(row["instance"]), row["solver"])].append(row)
        if row["set"] == "inst_m":
            by_m[(row["set"], row["m"], row["solver"])].append(row)

    lines = [f"# TOPTW benchmark: ILS vs OR-Tools ({len(rows)} runs)", ""]
    lines += ["## Overall", "", *table(overall, ["set"]), ""]
    lines += ["## By instance family", "", *table(by_family, ["set", "family"]), ""]
    lines += ["## INST-M by number of tours", "", *table(by_m, ["set", "m"]), ""]

    flagged = [r for r in rows if r["over_proven"] == "1" or r["feasible"] == "0"]
    lines += ["## Problems", "", f"Infeasible or above a proven optimum: {len(flagged)}", ""]
    lines += [
        f"- {r['solver']} {r['instance']} m={r['m']} seed={r['seed']}: {r['errors']}"
        for r in flagged[:20]
    ]

    lines += ["", "## Decision", ""]
    for set_name in sorted({r["set"] for r in rows}):
        stats = {s: summarise(v) for (st, s), v in overall.items() if st == set_name}
        if {"ils", "ortools"} <= stats.keys():
            diff = stats["ils"]["avg_gap"] - stats["ortools"]["avg_gap"]
            feasible = all(v["feasible_pct"] == 100 for v in stats.values())
            winner = "ILS" if feasible and diff < DECISION_MARGIN_PP else "OR-Tools"
            lines.append(
                f"- {set_name}: ILS avg gap - OR-Tools avg gap = {diff:+.2f} pp, "
                f"all feasible: {feasible} -> {winner}"
            )
    text = "\n".join(lines) + "\n"
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
