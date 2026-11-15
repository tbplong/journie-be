"""Tier 2: run both solvers on small Ho Chi Minh City trips built from data/places_1000.parquet.

    uv run --group data --group bench python bench/routing/hcmc.py [--time-limit 6] [--seeds 10]

There is no ground truth; the point is that both solvers handle what the benchmark lacks: a start
point that is not a POI (the traveller's location), several opening windows per day, hours that
differ by weekday, and real distances with a walk-or-ride rule. Score is reported against the
loose upper bound "every POI of the pool".

Placeholder assumptions (data/product will replace them): visit durations per category, a
08:00-21:00 day, the trip starting on a Sunday (so Monday closures matter), road distance =
haversine x DETOUR_FACTOR, walking below WALK_THRESHOLD_KM, a POI without hours is open all day,
and POI scores 1-10 from a hash of the id (retrieval relevance stand-in).
"""

import argparse
import csv
import sys
from pathlib import Path
from statistics import mean

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.retrieval.hours import DAY_MIN, weekly_intervals
from app.services.retrieval.scoring import explore, haversine_km
from app.services.routing import ils
from app.services.routing.instance import Instance, Node
from app.services.routing.validate import validate
from bench.routing import ortools_solver

PARQUET = ROOT / "data" / "places_1000.parquet"
SECONDS = 60  # time unit of these instances: seconds (per minute)
DAY_BEGIN_MIN, DAY_END_MIN = 8 * 60, 21 * 60
START_WEEKDAY = 6  # Sunday: days run Sun, Mon, Tue
DETOUR_FACTOR = 1.3  # road / straight-line distance
WALK_THRESHOLD_KM = 1.0  # agreed: walk below 1 km of road distance
WALK_KMH, RIDE_KMH = 4.5, 20.0
POOL_RADIUS_KM = 8.0  # POIs considered around the start point
MIN_CONFIDENCE = 0.7  # same bar as retrieval suggestions
VISIT_MIN = {  # placeholder until journie-data provides durations per category
    "food.restaurant": 60,
    "food.cafe": 45,
    "attraction.museum": 90,
    "attraction.landmark": 45,
    "attraction.historic_site": 45,
    "attraction.gallery": 60,
    "attraction.theme_park": 180,
    "nature.park": 45,
    "nature.garden": 45,
    "shopping.mall": 90,
    "shopping.market": 60,
    "shopping.department_store": 60,
    "entertainment.nightlife": 90,
    "entertainment.karaoke": 120,
    "entertainment.cinema": 150,
    "entertainment.performing_arts": 120,
}
DEFAULT_VISIT_MIN = 40  # culture_religion.* and anything unlisted
STARTS = {
    "ben_thanh": (10.7725, 106.6980),
    "thao_dien": (10.8030, 106.7340),
    "cho_lon": (10.7530, 106.6610),
}
TRIPS = [  # (start, pool size, days)
    ("ben_thanh", 30, 1),
    ("ben_thanh", 30, 2),
    ("ben_thanh", 60, 1),
    ("ben_thanh", 60, 2),
    ("ben_thanh", 120, 2),
    ("ben_thanh", 120, 3),
    ("thao_dien", 60, 2),
    ("thao_dien", 120, 3),
    ("cho_lon", 60, 1),
    ("cho_lon", 120, 3),
]


def travel_seconds(a: tuple[float, float], b: tuple[float, float]) -> int:
    road_km = haversine_km(*a, *b) * DETOUR_FACTOR
    speed = WALK_KMH if road_km < WALK_THRESHOLD_KM else RIDE_KMH
    return round(road_km / speed * 3600)


def merge(windows: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    out: list[list[int]] = []
    for start, end in sorted(windows):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return tuple((s, e) for s, e in out)


def day_windows(hours: dict | None, service_min: int, days: int) -> tuple[tuple[int, int], ...]:
    """Start-of-service windows on the trip timeline (seconds), one day per tour."""
    intervals = weekly_intervals(hours)
    windows = []
    for day in range(days):
        weekday = (START_WEEKDAY + day) % 7
        lo, hi = weekday * DAY_MIN + DAY_BEGIN_MIN, weekday * DAY_MIN + DAY_END_MIN
        shift = (day - weekday) * DAY_MIN  # week minute -> trip timeline minute
        opened = intervals if intervals is not None else [(lo, hi)]  # no hours: open all day
        for start, end in opened:
            first, last = max(start, lo), min(end, hi) - service_min  # finish before closing
            if last >= first:
                windows.append(((first + shift) * SECONDS, (last + shift) * SECONDS))
    return merge(windows)


def build(rows: list[dict], start: str, size: int, days: int) -> Instance:
    origin = STARTS[start]
    near = sorted(
        (r for r in rows if haversine_km(*origin, r["latitude"], r["longitude"]) <= POOL_RADIUS_KM),
        key=lambda r: explore(size, r["id"]),  # a fixed pseudo-random sample per pool size
    )[:size]
    nodes = [Node(origin[0], origin[1], 0, 0, ((0, days * DAY_MIN * SECONDS),))]
    points = [origin]
    for row in near:
        service = VISIT_MIN.get(row["category"], DEFAULT_VISIT_MIN)
        windows = day_windows(row["opening_hours_normalized"], service, days)
        if not windows:
            continue  # closed on every trip day
        score = 1 + int(9 * explore(0, row["id"]))
        nodes.append(Node(row["latitude"], row["longitude"], service * SECONDS, score, windows))
        points.append((row["latitude"], row["longitude"]))
    travel = tuple(tuple(travel_seconds(a, b) for b in points) for a in points)
    tours = tuple(
        ((d * DAY_MIN + DAY_BEGIN_MIN) * SECONDS, (d * DAY_MIN + DAY_END_MIN) * SECONDS)
        for d in range(days)
    )
    return Instance(f"{start}_{size}x{days}d", tuple(nodes), travel, tours)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--time-limit", type=float, default=6.0)
    parser.add_argument("--seeds", type=int, default=10)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("results") / "hcmc.csv"
    )
    args = parser.parse_args()
    columns = [
        "id",
        "latitude",
        "longitude",
        "category",
        "opening_hours_normalized",
        "recommendable",
        "confidence",
    ]
    rows = [
        r
        for r in pq.read_table(PARQUET, columns=columns).to_pylist()
        if r["recommendable"] and (r["confidence"] or 0) >= MIN_CONFIDENCE
    ]
    results = []
    for start, size, days in TRIPS:
        inst = build(rows, start, size, days)
        upper = sum(n.score for n in inst.nodes)
        multi = sum(len(n.windows) > days for n in inst.nodes[1:])
        runs = {"ils": range(args.seeds), "ortools": [0]}
        for name, seeds in runs.items():
            module = ils if name == "ils" else ortools_solver
            for seed in seeds:
                tours, trace = module.solve(inst, args.time_limit, seed)
                check = validate(inst, tours)
                results.append(
                    {
                        "instance": inst.name,
                        "pois": len(inst.nodes) - 1,
                        "days": days,
                        "multi_window_pois": multi,
                        "solver": name,
                        "seed": seed,
                        "score": check.score,
                        "upper": upper,
                        "visits": sum(map(len, tours)),
                        "feasible": int(check.feasible),
                        "errors": " | ".join(check.errors[:2]),
                        "time_to_best_s": next(
                            (round(t, 3) for t, s in trace if s >= check.score), None
                        ),
                    }
                )
        print(f"{inst.name}: done", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, list(results[0]))
        writer.writeheader()
        writer.writerows(results)

    print(
        "\n| trip | POIs | multi-window POIs | ILS avg | ILS best | OR-Tools | upper | feasible |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for start, size, days in TRIPS:
        name = f"{start}_{size}x{days}d"
        mine = [r for r in results if r["instance"] == name]
        il = [r["score"] for r in mine if r["solver"] == "ils"]
        ort = next(r for r in mine if r["solver"] == "ortools")
        feasible = all(r["feasible"] for r in mine)
        print(
            f"| {name} | {mine[0]['pois']} | {mine[0]['multi_window_pois']} | {mean(il):.1f} | "
            f"{max(il)} | {ort['score']} | {mine[0]['upper']} | {feasible} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
