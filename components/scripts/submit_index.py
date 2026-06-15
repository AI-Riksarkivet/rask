"""Submit the search indexer as a Ray Job with a tidy submission_id.

Without this the indexer connects via `ray.init(address="auto")` and shows up
in the dashboard with an auto-generated driver-job ID like `f5000000`. This
wrapper hands the same `index_alto.py index-all` invocation to
JobSubmissionClient with `submission_id=search-index-<timestamp>` so it
groups alongside the htr-/prefetch- jobs the orchestrator submits.

Usage:
    uv run python scripts/submit_index.py                        # full re-index
    uv run python scripts/submit_index.py --skip-existing        # incremental
    uv run python scripts/submit_index.py --concurrency 32       # tune fan-out
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv


REPO = Path(__file__).resolve().parents[2]
DEFAULT_DASHBOARD = "http://localhost:8265"


def main() -> int:
    load_dotenv(REPO / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-existing", action="store_true", help="skip batches already in the Lance dataset")
    ap.add_argument("--concurrency", type=int, default=16, help="parallel S3 fetches per batch")
    ap.add_argument(
        "--dashboard-url",
        default=os.environ.get("RAY_DASHBOARD_URL", DEFAULT_DASHBOARD),
        help="Ray dashboard HTTP URL",
    )
    ap.add_argument(
        "--submission-id",
        default=None,
        help="override submission_id (default: search-index-<UTC timestamp>)",
    )
    args = ap.parse_args()

    from ray.job_submission import JobSubmissionClient

    sid = args.submission_id or f"search-index-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    entrypoint_parts = [
        "uv run python components/scripts/index_alto.py index-all",
        f"--concurrency {args.concurrency}",
    ]
    if args.skip_existing:
        entrypoint_parts.append("--skip-existing")
    entrypoint = " ".join(entrypoint_parts)

    client = JobSubmissionClient(args.dashboard_url)
    submission_id = client.submit_job(
        entrypoint=entrypoint,
        submission_id=sid,
        runtime_env={
            "working_dir": str(REPO),
            # Workers spawn fresh and don't see our shell env. Forward only the
            # storage-related vars (AWS/HCP) so the indexer can reach the
            # ALTO/image/search buckets.
            "env_vars": {k: v for k, v in os.environ.items() if k.startswith(("AWS_", "HCP_"))},
        },
        metadata={"kind": "search-index"},
    )
    print(f"submitted: {submission_id}")
    print(f"  dashboard: {args.dashboard_url}/#/jobs/{submission_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
