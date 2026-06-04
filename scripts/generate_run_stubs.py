"""
Generate meta.json + status.json stubs for all existing by_community outputs
so the Railway brief viewer can find them in app/runs/.

Run from repo root: python3 scripts/generate_run_stubs.py
"""

import json
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).parent.parent
OUTPUTS_DIR = REPO_ROOT / "outputs" / "by_community"
RUNS_DIR = REPO_ROOT / "app" / "runs"

def main():
    communities = sorted([d.name for d in OUTPUTS_DIR.iterdir() if d.is_dir()])
    print(f"Found {len(communities)} communities in outputs/by_community/")

    for slug in communities:
        run_id = f"scan-{slug}-001"
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        meta_path = run_dir / "meta.json"
        status_path = run_dir / "status.json"

        if meta_path.exists():
            print(f"  skip {run_id} (already exists)")
            continue

        meta = {
            "run_id": run_id,
            "target": slug,
            "flags": {
                "target": slug,
                "state": "NM",
                "depth": "standard",
                "preset": "maturity_adjusted",
                "mode": "2",
                "all": False,
                "dry_run": False,
                "mock": False,
                "batch": False,
                "no_cache": False,
                "force_refresh": False,
                "extra_args": ""
            },
            "command": [
                "python3", "main.py", slug,
                "--state", "NM",
                "--depth", "standard",
                "--preset", "maturity_adjusted",
                "--mode", "2"
            ],
            "start_time": "2026-06-02T10:00:00-06:00",
            "pid": 0
        }

        status = {
            "state": "done",
            "exit_code": 0
        }

        meta_path.write_text(json.dumps(meta, indent=2))
        status_path.write_text(json.dumps(status, indent=2))
        print(f"  created {run_id}")

    print(f"\nDone. Run stubs in app/runs/ — upload to Railway volume next.")

if __name__ == "__main__":
    main()
