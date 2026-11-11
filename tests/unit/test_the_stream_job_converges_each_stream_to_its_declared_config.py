"""The NATS stream Job edits a stream that already exists to the config the chart declares, or fails naming why ([[XC-164]]).

A stream created once keeps its creation config while the chart moves on, so a changed max age, replica count or
retention would reach no running bus. The Job therefore edits each declared stream in place on every run, and where JetStream refuses an
edit (storage, a mirror, retention to or from workqueue) it fails with the server's reason rather than leaving a stream the
chart does not describe.

RUN, not read: the Job's rendered script runs under `sh` against a stand-in `nats` CLI on `PATH`, which records the config
each edit submits and answers as natscli 0.5.1 (nats-box 0.20.2) did against nats-server 2.15.2, measured 2026-11-09 (an
accepted edit exits 0; a refused one exits 1, `nats: error: could not edit Stream <name>: <reason> (10052)`). The live bus is the GitOps lane's (`dagger call
gitops-from-empty`), which changes the max age on a running estate and reads the stream back.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, containers, render


#: The stand-in CLI: `stream info` finds the streams named in `exists/`, `stream edit` records the config it is handed and
#: refuses it when `refuse` holds a reason, a stream's info request answers the config it was last added or edited with, a
#: consumer read finds none, and a consumer add is accepted.
_NATS = r"""#!/bin/sh
[ "$1" = "--server" ] && shift 2
case "$1 $2" in
  "stream ls") exit 0 ;;
  "stream info") [ -f "$FAKE_NATS/exists/$3" ] || { echo "nats: error: stream not found" >&2; exit 1; }; exit 0 ;;
  "stream edit")
    cp "$5" "$FAKE_NATS/edited-$3.json"
    if [ -s "$FAKE_NATS/refuse" ]; then echo "nats: error: could not edit Stream $3: $(cat "$FAKE_NATS/refuse")" >&2; exit 1; fi
    echo "Stream $3 was updated"; exit 0 ;;
  "stream add") cp "$4" "$FAKE_NATS/added-$(basename "$4")"; exit 0 ;;
  "consumer ls"|"consumer rm"|"consumer edit"|"consumer add") exit 0 ;;
  "consumer info") echo "nats: error: consumer not found" >&2; exit 1 ;;
  'req $JS.API.STREAM.INFO.'*)
    declared=$(ls "$FAKE_NATS/edited-${2#\$JS.API.STREAM.INFO.}.json" "$FAKE_NATS/added-${2#\$JS.API.STREAM.INFO.}.json" 2>/dev/null | head -n 1)
    printf '{"config":%s}' "$(cat "$declared")"; exit 0 ;;
  "req "*) echo '{"err_code":10014}'; exit 0 ;;
esac
echo "stand-in nats: unexpected call: $*" >&2
exit 2
"""

#: The config the chart declares for LINEAGE with the max age set to three days and the replica count at its default.
LINEAGE = {
    "name": "LINEAGE",
    "subjects": ["lineage.events.>"],
    "retention": "limits",
    "max_consumers": -1,
    "max_msgs": -1,
    "max_bytes": -1,
    "max_msgs_per_subject": -1,
    "max_msg_size": -1,
    "max_age": 259_200_000_000_000,
    "storage": "file",
    "discard": "old",
    "num_replicas": 3,
    "duplicate_window": 120_000_000_000,
    "deny_delete": True,
    "deny_purge": True,
    "allow_rollup_hdrs": False,
    "allow_direct": True,
}

REFUSAL = "stream configuration update can not change retention policy to/from workqueue (10052)"


@pytest.mark.parametrize(
    ("refusal", "exit_code", "reported"),
    [
        pytest.param("", 0, "stream LINEAGE: converged to the declared config", id="edited-in-place"),
        pytest.param(
            REFUSAL,
            1,
            f"!! stream LINEAGE cannot be edited to the declared config in place: nats: error: could not edit Stream LINEAGE: {REFUSAL}",
            id="refused",
        ),
    ],
)
def test_an_existing_stream_is_edited_to_its_declared_config_or_the_job_fails_naming_why(refusal: str, exit_code: int, reported: str, tmp_path: Path) -> None:
    [script] = [
        c["command"][-1] for workload, _, c in containers(render(*DEFAULT_ARGS, "--set", "nats.streamMaxAgeSeconds=259200")) if "-nats-stream-" in workload
    ]
    bin_dir, state = tmp_path / "bin", tmp_path / "nats"
    (state / "exists").mkdir(parents=True)
    bin_dir.mkdir()
    (bin_dir / "nats").write_text(_NATS)
    (bin_dir / "nats").chmod(0o755)
    (state / "exists" / "LINEAGE").touch()
    (state / "refuse").write_text(refusal)

    done = subprocess.run(
        ["sh", "-c", script],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "FAKE_NATS": str(state)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    edited = {path.name.removeprefix("edited-").removesuffix(".json"): json.loads(path.read_text()) for path in state.glob("edited-*.json")}
    assert edited.get("LINEAGE") == LINEAGE, f"the Job edited {sorted(edited)}:\n{done.stdout}"
    assert (done.returncode, reported in done.stdout.splitlines()) == (exit_code, True), f"{done.stdout}{done.stderr}"
