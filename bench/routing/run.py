"""Run TOPTW solvers on the KU Leuven benchmark and append one CSV row per run.

    uv run python bench/routing/download.py            # once
    uv run --group bench python bench/routing/run.py [--solvers ils,ortools] [--seeds 10]
        [--time-limit 6] [--sets inst_m,opt] [--only c101,pr01] [--workers 1]
        [--out bench/routing/results/results.csv]

Cases come from bench/routing/bks.csv: "inst_m" = 76 instances x m = 1..4 (304) against the
best known value we could collect (``ref``: max of the KU Leuven results workbook, Gunawan et
al. 2017 Table 13 and the 2025 survey tables; rows only from the workbook may be below the true
BKS); "opt" = 66 instances with the file's own tour count, where visiting every customer is
optimal. Every solution is re-checked by app.services.routing.validate. A score above a
``proven`` optimum is a model error and is flagged.

Runs already in ``--out`` are skipped, so an interrupted run can be resumed. Workers run in
separate processes; more workers than physical cores distorts the time-limited results.
"""

import argparse
import csv
import os
import platform
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.routing import ils
from app.services.routing.instance import read_benchmark
from app.services.routing.validate import validate

DATA = ROOT / "data" / "benchmarks" / "toptw"
BKS = Path(__file__).with_name("bks.csv")
CHECKPOINTS_S = (1.0, 3.0)  # anytime scores; the final score is the time-limit one
FIELDS = [
    "solver",
    "instance",
    "m",
    "set",
    "seed",
    "time_limit_s",
    "score",
    "ref",
    "gap_pct",
    "feasible",
    "over_proven",
    "time_to_best_s",
    "score_1s",
    "score_3s",
    "errors",
    "cpu",
]


def solver(name: str):  # a module with solve(instance, time_limit_s, seed) -> (tours, trace)
    if name == "ils":
        return ils
    if name == "ortools":
        from bench.routing import ortools_solver  # needs the "bench" dependency group

        return ortools_solver
    raise ValueError(f"unknown solver {name!r}")


def score_at(trace: list[tuple[float, int]], seconds: float) -> int | None:
    reached = [score for at, score in trace if at <= seconds]
    return max(reached) if reached else None


def run_one(job: dict) -> dict:
    path = next(DATA.glob(f"*/{job['instance']}.txt"))
    instance = read_benchmark(path, int(job["m"]))
    tours, trace = solver(job["solver"]).solve(instance, job["time_limit_s"], job["seed"])
    check = validate(instance, tours)
    ref = int(job["ref"])
    best_at = next((at for at, score in trace if score >= check.score), None)
    return {
        **{key: job[key] for key in ("solver", "instance", "m", "set", "seed", "time_limit_s")},
        "score": check.score,
        "ref": ref,
        "gap_pct": round(100 * (ref - check.score) / ref, 3),
        "feasible": int(check.feasible),
        "over_proven": int(job["proven"] == "1" and check.score > ref),
        "time_to_best_s": None if best_at is None else round(best_at, 3),
        "score_1s": score_at(trace, CHECKPOINTS_S[0]),
        "score_3s": score_at(trace, CHECKPOINTS_S[1]),
        "errors": " | ".join(check.errors[:3]),
        "cpu": platform.processor() or platform.machine(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--solvers", default="ils,ortools")
    parser.add_argument("--seeds", type=int, default=10, help="ILS runs per case (OR-Tools: 1)")
    parser.add_argument("--time-limit", type=float, default=6.0)
    parser.add_argument("--sets", default="inst_m,opt")
    parser.add_argument("--only", default="", help="Comma-separated instance names")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("results") / "results.csv"
    )
    args = parser.parse_args()

    sets, only = set(args.sets.split(",")), {n for n in args.only.split(",") if n}
    cases = [
        row
        for row in csv.DictReader(BKS.open(encoding="utf-8"))
        if row["set"] in sets and (not only or row["instance"] in only)
    ]
    done = set()
    if args.out.exists():
        with args.out.open(encoding="utf-8") as handle:
            done = {
                (r["solver"], r["instance"], r["m"], r["set"], r["seed"])
                for r in csv.DictReader(handle)
            }
    jobs = [
        {**case, "solver": name, "seed": seed, "time_limit_s": args.time_limit}
        for case in cases
        for name in args.solvers.split(",")
        for seed in (range(args.seeds) if name == "ils" else [0])
        if (name, case["instance"], case["m"], case["set"], str(seed)) not in done
    ]
    print(
        f"{len(jobs)} runs (~{len(jobs) * args.time_limit / args.workers / 3600:.1f} h), "
        f"{len(done)} already in {args.out}, {os.cpu_count()} logical CPUs",
        flush=True,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    new_file = not args.out.exists()
    with (
        args.out.open("a", newline="", encoding="utf-8") as handle,
        ProcessPoolExecutor(max_workers=args.workers) as pool,
    ):
        writer = csv.DictWriter(handle, FIELDS)
        if new_file:
            writer.writeheader()
        for count, row in enumerate(pool.map(run_one, jobs), 1):
            writer.writerow(row)
            handle.flush()
            if not row["feasible"] or row["over_proven"]:
                print(
                    f"!! {row['solver']} {row['instance']} m={row['m']}: {row['errors']}"
                    f"{' score above proven optimum' if row['over_proven'] else ''}",
                    flush=True,
                )
            if count % 50 == 0:
                print(f"{count}/{len(jobs)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
