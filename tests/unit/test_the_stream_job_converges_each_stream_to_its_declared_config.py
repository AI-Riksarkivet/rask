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

from tests.unit.chart_render import DEFAULT_ARGS, bus_jobs, render


#: The stand-in CLI: it answers only a call presenting the token in `token` as the connection `admin`, as the signer
#: admits the stream Job; `stream info` finds the streams named in `exists/`, `stream edit` records the config it is handed and
#: refuses it when `refuse` holds a reason, a stream's info request answers the config it was last added or edited with, a
#: consumer the bus holds is the one `held/<stream>-<consumer>.json` describes (none otherwise), and a consumer add or edit
#: records the config it is handed.
_NATS = r"""#!/bin/sh
server=$2
[ "$1 $3 $4" = "--server --connection-name admin" ] && shift 4 || { echo "nats: error: Authorization Violation" >&2; exit 1; }
case "$server" in tls://*) [ "$1" = "--tlsfirst" ] && shift || { echo "nats: error: tls: first record does not look like a TLS handshake" >&2; exit 1; } ;; esac
[ "$NATS_TOKEN" = "$(cat "$FAKE_NATS/token")" ] || { echo "nats: error: Authorization Violation" >&2; exit 1; }
case "$1 $2" in
  "stream ls") exit 0 ;;
  "stream info") [ -f "$FAKE_NATS/exists/$3" ] || { echo "nats: error: stream not found" >&2; exit 1; }; exit 0 ;;
  "stream edit")
    cp "$5" "$FAKE_NATS/edited-$3.json"
    if [ -s "$FAKE_NATS/refuse" ]; then echo "nats: error: could not edit Stream $3: $(cat "$FAKE_NATS/refuse")" >&2; exit 1; fi
    echo "Stream $3 was updated"; exit 0 ;;
  "stream add") cp "$4" "$FAKE_NATS/added-$(basename "$4")"; exit 0 ;;
  "consumer ls"|"consumer rm") exit 0 ;;
  "consumer edit"|"consumer add") cp "$6" "$FAKE_NATS/declared-$3-$4.json"; exit 0 ;;
  "consumer info") [ -f "$FAKE_NATS/held/$3-$4.json" ] && exit 0; echo "nats: error: consumer not found" >&2; exit 1 ;;
  'req $JS.API.CONSUMER.INFO.'*)
    held="$FAKE_NATS/held/$(echo "${2#\$JS.API.CONSUMER.INFO.}" | tr . -).json"
    if [ -f "$held" ]; then printf '{"config":%s}' "$(cat "$held")"; else echo '{"err_code":10014}'; fi; exit 0 ;;
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


def _run_declare(tmp_path: Path, *render_args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The declare phase's rendered script run against the stand-in, on a bus holding what ``tmp_path / "nats"`` lays out."""
    script = bus_jobs(render(*DEFAULT_ARGS, *render_args))["declare"]
    bin_dir, state = tmp_path / "bin", tmp_path / "nats"
    (state / "exists").mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "nats").write_text(_NATS)
    (bin_dir / "nats").chmod(0o755)
    (state / "token").write_text("the stream Job's projected bus token")
    done = subprocess.run(
        ["sh", "-c", script],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "FAKE_NATS": str(state), "BUS_TOKEN_FILE": str(state / "token")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, state


@pytest.mark.parametrize(
    ("held", "begins"),
    [pytest.param(None, 1, id="a-new-consumer-begins-at-the-first-message"), pytest.param(7, 7, id="a-held-consumer-keeps-its-start")],
)
def test_lineages_consumer_is_declared_at_the_start_it_holds_or_at_the_streams_first_message(tmp_path: Path, held: int | None, begins: int) -> None:
    """JetStream refuses to change a consumer's start in place, so the Job declares `lineage-ingest` back at the start
    the bus holds for it, and a consumer the bus does not hold yet begins at the stream's first message."""
    if held is not None:
        (tmp_path / "nats" / "held").mkdir(parents=True)
        (tmp_path / "nats" / "held" / "LINEAGE-lineage-ingest.json").write_text(json.dumps({"durable_name": "lineage-ingest", "opt_start_seq": held}))

    done, state = _run_declare(tmp_path)

    declared = json.loads((state / "declared-LINEAGE-lineage-ingest.json").read_text()) if (state / "declared-LINEAGE-lineage-ingest.json").exists() else {}
    assert (done.returncode, declared.get("opt_start_seq"), declared.get("deliver_policy")) == (0, begins, "by_start_sequence"), f"{done.stdout}{done.stderr}"


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
    state = tmp_path / "nats"
    (state / "exists").mkdir(parents=True)
    (state / "exists" / "LINEAGE").touch()
    (state / "refuse").write_text(refusal)

    done, state = _run_declare(tmp_path, "--set", "nats.streamMaxAgeSeconds=259200")

    edited = {path.name.removeprefix("edited-").removesuffix(".json"): json.loads(path.read_text()) for path in state.glob("edited-*.json")}
    assert edited.get("LINEAGE") == LINEAGE, f"the Job edited {sorted(edited)}:\n{done.stdout}"
    assert (done.returncode, reported in done.stdout.splitlines()) == (exit_code, True), f"{done.stdout}{done.stderr}"
