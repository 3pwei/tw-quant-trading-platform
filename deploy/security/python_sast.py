"""Run all Bandit checks, fail on High findings or incomplete scans; log counts only."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def enforce(report: dict) -> int:
    if not isinstance(report, dict) or report.get("errors") != []:
        raise ValueError("Incomplete scan")
    results = report.get("results")
    metrics = report.get("metrics")
    if not isinstance(results, list) or not isinstance(metrics, dict):
        raise ValueError("Invalid report")
    if metrics.get("_totals", {}).get("loc", 0) <= 0:
        raise ValueError("Empty scan")
    counts = Counter({"LOW": 0, "MEDIUM": 0, "HIGH": 0})
    for result in results:
        severity = result.get("issue_severity")
        if severity not in counts or result.get("issue_confidence") not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("Unknown finding severity/confidence")
        counts[severity] += 1
    print(f"Python SAST: low={counts['LOW']} medium={counts['MEDIUM']} high={counts['HIGH']} blocked={counts['HIGH']}")
    return int(counts["HIGH"] > 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.paths or [ROOT / name for name in ("tw_quant", "tools", "deploy")]
    try:
        if any(not path.exists() for path in paths):
            raise ValueError("Missing scan target")
        scan = subprocess.run(
            [sys.executable, "-m", "bandit", "--quiet", "--recursive", "--ignore-nosec", "--format", "json", *map(str, paths)],
            check=False, capture_output=True, text=True,
        )
        if scan.returncode not in (0, 1):
            raise ValueError("Scanner failed")
        return enforce(json.loads(scan.stdout))
    except (OSError, ValueError, TypeError, AttributeError):
        print("Python SAST: scanner/report failure; blocked", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
