"""The development store's bucket-events Job configures every bucket it lists, whichever goes between the listing and its put.

The Job lists the store's buckets and gives each the notification configuration that publishes its writes on
`store.events`. A bucket deleted after the listing answers its put `NoSuchBucket`; the run must go on to the buckets after
it rather than end there, and any other refusal still fails the Job. The rendered script runs here against an `aws`
that answers as RustFS does.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from tests.chart.chart_render import render_fixture


_AWS = """#!/bin/sh
case "$*" in
  *list-buckets*) echo "acme gone zeta"; exit 0 ;;
  *"--bucket gone"*) echo "An error occurred (NoSuchBucket) when calling the PutBucketNotificationConfiguration operation" >&2; exit 254 ;;
  *"--bucket zeta"*) [ -n "${REFUSE_ZETA:-}" ] && { echo "An error occurred (AccessDenied)" >&2; exit 254; } ;;
esac
echo "$*" >> "$CALLS"
"""


def _run(tmp_path: Path, **env: str) -> tuple[int, list[str]]:
    [config] = [doc for doc in render_fixture("rustfs") if doc and doc.get("kind") == "ConfigMap" and "configure.sh" in (doc.get("data") or {})]
    script = tmp_path / "configure.sh"
    script.write_text(config["data"]["configure.sh"])
    aws = tmp_path / "bin" / "aws"
    aws.parent.mkdir()
    aws.write_text(_AWS)
    aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls"
    calls.touch()
    done = subprocess.run(  # noqa: S603
        ["/bin/sh", str(script)],
        env={"PATH": f"{aws.parent}:{os.environ['PATH']}", "CALLS": str(calls), **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, [line.split("--bucket ")[1].split()[0] for line in calls.read_text().splitlines()]


def test_a_bucket_deleted_after_the_listing_is_skipped_and_the_rest_configured(tmp_path: Path) -> None:
    assert _run(tmp_path) == (0, ["acme", "zeta"])


def test_any_other_refusal_still_fails_the_job(tmp_path: Path) -> None:
    code, configured = _run(tmp_path, REFUSE_ZETA="1")

    assert (code != 0, configured) == (True, ["acme"])
