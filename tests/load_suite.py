"""Run pytest repeatedly in concurrent, independent processes; retain per-run logs and JUnit results."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("nodes", nargs="*")
    args = parser.parse_args()
    if args.repeat < 1 or args.workers < 1:
        parser.error("--repeat and --workers must be positive")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}

    def run(number):
        start = time.monotonic()
        xml = output / f"run-{number:02}.xml"
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={xml}", *args.nodes]
        with (output / f"run-{number:02}.log").open("w") as log:
            result = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        record = {"run": number, "exit": result.returncode, "seconds": round(time.monotonic() - start, 2),
                  "tests": []}
        if xml.exists():
            for case in ET.parse(xml).iter("testcase"):
                outcome = next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), "passed")
                record["tests"].append({"class": case.get("classname"), "name": case.get("name"), "outcome": outcome})
        print(f"run {number}: exit={result.returncode}, {record['seconds']}s", flush=True)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, range(1, args.repeat + 1)))
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    return int(any(result["exit"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
