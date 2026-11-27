"""`scripts/alert_rules_drill.py` against a datasource that can plan no rule, and one that can plan one.

A rule whose metric has no series in the store is inconclusive, not refused (the drill's own docstring has the
measurement). A run in which every rule is inconclusive has proved nothing about the engine, so it fails, and still names
each rule it could not judge. The datasource is a loopback HTTP server answering the PromQL query endpoint the way
GreptimeDB answers a plan over a metric it has never seen; the script runs as `make alert-rules-drill` runs it.
"""

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_ABSENT = json.dumps({"status": "error", "error": 'No field named "sum(prom_rate(x))"'}).encode()
_EVALUATED = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}}).encode()


class _Store(ThreadingHTTPServer):
    """Answers the first ``evaluable`` queries as evaluated and every later one as a plan over an absent metric."""

    evaluable = 0
    answered = 0


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        store = self.server
        assert isinstance(store, _Store)
        evaluated = store.answered < store.evaluable
        store.answered += 1
        body = _EVALUATED if evaluated else _ABSENT
        self.send_response(200 if evaluated else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the stdlib's parameter name
        return


@pytest.fixture
def store() -> Iterator[_Store]:
    server = _Store(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


@pytest.mark.parametrize(
    ("evaluable", "exit_code"),
    [
        pytest.param(0, 1, id="no-rule-reached-a-verdict"),
        pytest.param(1, 0, id="one-rule-evaluated"),
    ],
)
def test_the_drill_fails_only_when_no_rule_reached_a_verdict(store: _Store, evaluable: int, exit_code: int) -> None:
    store.evaluable = evaluable
    host, port = store.server_address[:2]

    done = subprocess.run(
        [sys.executable, "scripts/alert_rules_drill.py"],
        cwd=_ROOT,
        env={"PATH": "/usr/bin:/bin", "RASK_GREPTIME_URL": f"http://{host}:{port}"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    inconclusive = [line.strip() for line in done.stdout.splitlines() if line.strip().startswith("? ")]
    assert (done.returncode, len(inconclusive)) == (exit_code, store.answered - evaluable), done.stdout + done.stderr
