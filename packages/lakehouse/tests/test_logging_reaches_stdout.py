"""What a lakehouse process writes to stdout, and at which level: the one setup every process runs.

Every module in every service logs through ``logging.getLogger(__name__)``, so the handler sits on the ROOT logger and
every one of them inherits it. A record that reaches no handler is discarded without a trace, which is how a swallowed
diagnostic (a failure caught by design and reported with a WARNING) stays invisible while the path fails for a week
(measured live 2026-08-17: a lineage ingest that dropped a malformed event logged nothing at all).

``RASK_LOG_LEVEL`` is the one lever, and the lakehouse's own loggers follow it whatever level an import left on them:
``WARNING`` quiets the lakehouse's INFO, ``DEBUG`` opens its DEBUG, and a value that names no level stops the process at
boot rather than leaving it at a level nobody chose. These tests read the formatted line the installed handler writes,
never a probe handler of their own: a handler attached to root receives propagated records whether or not the setup did
its job.
"""

import io
import logging
import re
from collections.abc import Iterator

import pytest

from lakehouse.process.log_handler import setup_logging


#: The loggers these tests move: the root, the lakehouse's own packages and a third-party library's.
_TOUCHED = ("", "catalog", "lineage", "maintenance", "lakehouse", "botocore")
#: The timestamp a line opens with, which is not the claim.
_TIMESTAMP = re.compile(r"^\S+ \S+ ")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Leave global logging exactly as found: this module mutates it, and pytest's own capture handlers live on root."""
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in _TOUCHED}
    yield
    for name, (handlers, level) in saved.items():
        logging.getLogger(name).handlers, logging.getLogger(name).level = handlers, level


def _stdout_after(record: tuple[str, int], *, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the setup over a stdout of the test's own, emit ``record`` (logger name, level), and return each line written
    without its timestamp and with its padding collapsed."""
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    logging.getLogger().handlers = []

    setup_logging()

    name, level = record
    logging.getLogger(name).log(level, "canary")
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [" ".join(_TIMESTAMP.sub("", line).split()) for line in stream.getvalue().splitlines()]


def test_a_service_module_record_reaches_stdout_at_the_default_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the default level a lakehouse INFO record comes out on stdout, and so reaches the root's other handlers too
    (the OTLP copy the audit and lifecycle tier rides)."""
    monkeypatch.delenv("RASK_LOG_LEVEL", raising=False)

    assert _stdout_after(("lineage.services.consumer", logging.INFO), monkeypatch=monkeypatch) == ["INFO lineage.services.consumer [-] — canary"]


@pytest.mark.parametrize(
    ("setting", "record", "lines"),
    [
        pytest.param("WARNING", ("catalog.api", logging.INFO), [], id="warning-quiets-the-lakehouse-info"),
        pytest.param("WARNING", ("catalog.api", logging.WARNING), ["WARNING catalog.api [-] — canary"], id="warning-keeps-a-warning"),
        pytest.param("debug", ("lakehouse.storage", logging.DEBUG), ["DEBUG lakehouse.storage [-] — canary"], id="debug-opens-the-lakehouse-debug"),
        pytest.param("DEBUG", ("botocore.endpoint", logging.DEBUG), [], id="debug-leaves-a-library-at-info"),
    ],
)
def test_the_lakehouse_loggers_follow_the_level_whatever_an_import_left(
    setting: str, record: tuple[str, int], lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lever decides the lakehouse's loggers even where an import pinned them at INFO before the setup ran. A library
    stays at INFO under ``DEBUG``: botocore's DEBUG writes each request's headers, a session token among them."""
    monkeypatch.setenv("RASK_LOG_LEVEL", setting)
    for name in ("catalog", "lineage", "maintenance", "lakehouse"):
        logging.getLogger(name).setLevel(logging.INFO)

    assert _stdout_after(record, monkeypatch=monkeypatch) == lines


def test_a_level_that_names_no_level_stops_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the lever fails the boot, naming the variable and the levels it takes."""
    monkeypatch.setenv("RASK_LOG_LEVEL", "VERBOSE")

    with pytest.raises(ValueError, match=r"RASK_LOG_LEVEL\n  Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'"):
        setup_logging()


def test_setup_is_idempotent_so_repeated_app_builds_do_not_duplicate_lines() -> None:
    """The app factory runs it per app, and a test process may build several."""
    setup_logging()
    first = len(logging.getLogger().handlers)
    setup_logging()
    setup_logging()

    assert len(logging.getLogger().handlers) == first, "handlers accumulated — each log line would be emitted N times"
