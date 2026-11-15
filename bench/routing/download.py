"""Download the TOPTW benchmark instances from KU Leuven (https://www.mech.kuleuven.be/en/mim/op).

    uv run python bench/routing/download.py [--out data/benchmarks/toptw]

Four archives, 76 instances, each solved with m = 1..4 tours (304 cases): Righini & Salani
c/r/rc-100-100 (Solomon) and pr01-10 (Cordeau), Montemanni & Gambardella c/r/rc-200-100 and
pr11-20. The site states no licence, so the files are not committed (data/benchmarks is ignored).
"""

import argparse
import io
import sys
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://www.mech.kuleuven.be/en/mim/op/instances/"
ARCHIVES = {
    "solomon_100": "righiniTOPTW2",
    "cordeau_01_10": "righiniTOPTW3",
    "solomon_200": "MontemanniTOPTW1",
    "cordeau_11_20": "MontemanniTOPTW2",
}
TIMEOUT_S = 60


def extract(data: bytes, target: Path) -> list[str]:
    """Extract the .txt instance files of an archive, flattened into ``target``."""
    names = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            name = Path(member.filename).name  # flatten: no directories, no path traversal
            if member.is_dir() or not name.lower().endswith(".txt"):
                continue
            (target / name).write_bytes(archive.read(member))
            names.append(name)
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "benchmarks" / "toptw")
    args = parser.parse_args()
    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as client:
        for folder, archive in ARCHIVES.items():
            response = client.get(BASE_URL + archive)
            response.raise_for_status()
            target = args.out / folder
            target.mkdir(parents=True, exist_ok=True)
            names = extract(response.content, target)
            print(f"{folder}: {len(names)} files ({names[0]} .. {names[-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
