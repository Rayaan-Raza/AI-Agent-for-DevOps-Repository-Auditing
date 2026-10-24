"""Build the local test repositories, run the agent on them, and score precision and recall.

python eval/score.py                 # build, run all, score
python eval/score.py --only repo_b_subtle
python eval/score.py --score-only    # score existing reports in eval/results/
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.fixtures import REPOS  # noqa: E402
from tests.conftest import make_git_repo  # noqa: E402

REPOS_DIR = ROOT / "eval" / "repos"
RESULTS_DIR = ROOT / "eval" / "results"


def build(name: str) -> Path:
    path = REPOS_DIR / name
    if path.exists():
        shutil.rmtree(path, onerror=lambda f, p, e: (Path(p).chmod(0o700), f(p)))
    make_git_repo(path, REPOS[name][0])
    return path


def matches(pred: dict, exp: dict) -> bool:
    if pred["category"] != exp["category"]:
        return False
    files = [exp["file"]] + exp.get("alt_files", [])
    pf = pred["file"]
    if not any(
        (f is None and pf is None) or (f is not None and pf is not None and fnmatch.fnmatch(pf, f))
        for f in files
    ):
        return False
    lines = exp.get("lines")
    if lines and pred["line"] is not None and not any(abs(pred["line"] - n) <= 2 for n in lines):
        return False
    return True


def score(report: dict, expected: list[dict]) -> dict:
    preds = report["violations"]
    used: set[int] = set()
    hits, misses = [], []
    for exp in expected:
        idx = next((i for i, p in enumerate(preds) if i not in used and matches(p, exp)), None)
        if idx is None:
            misses.append(exp["label"])
        else:
            used.add(idx)
            hits.append((exp["label"], preds[idx]["rule_id"]))
    false_pos = [
        f"{p['rule_id']} {p['file']}:{p['line']} {p['description']}"
        for i, p in enumerate(preds)
        if i not in used
    ]
    precision = len(used) / len(preds) if preds else 1.0
    recall = len(hits) / len(expected) if expected else 1.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "reported": len(preds),
        "expected": len(expected),
        "hits": hits,
        "missed": misses,
        "unmatched_reports": false_pos,
    }


def run_agent(name: str, path: Path, extra: list[str]) -> tuple[int, float]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{name}.json"
    cmd = [
        sys.executable,
        str(ROOT / "agent.py"),
        "--repo",
        str(path),
        "--output",
        str(out),
        "--allow-local",
        *extra,
    ]
    t0 = time.time()
    code = subprocess.call(cmd, cwd=ROOT)
    return code, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=list(REPOS))
    ap.add_argument("--score-only", action="store_true")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("agent_args", nargs="*", help="extra arguments for agent.py (after --)")
    args = ap.parse_args()
    names = args.only or list(REPOS)
    summary = {}
    for name in names:
        if not args.score_only:
            path = build(name)
            if args.build_only:
                print(f"built {path}")
                continue
            code, secs = run_agent(name, path, args.agent_args)
            print(f"{name}: agent exit {code} in {secs:.0f} s", flush=True)
            if code != 0:
                summary[name] = {"error": f"exit {code}"}
                continue
        report_path = RESULTS_DIR / f"{name}.json"
        if not report_path.exists():
            continue
        summary[name] = score(json.loads(report_path.read_text()), REPOS[name][1])
    if args.build_only:
        return 0
    print(json.dumps(summary, indent=2))
    tp = sum(len(s.get("hits", [])) for s in summary.values())
    reported = sum(s.get("reported", 0) for s in summary.values())
    expected = sum(s.get("expected", 0) for s in summary.values())
    if reported or expected:
        print(
            f"OVERALL precision {tp / reported if reported else 1:.2f}  recall {tp / expected if expected else 1:.2f}"
        )
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
