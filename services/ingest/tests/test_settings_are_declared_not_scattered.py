"""ING-07 / ingest-flow-09 — this service reads its configuration from ONE declaration.

The plane had no operational settings model at all: `IngestAuthSettings` covered the auth half and
every other knob was a bare `os.getenv` at the point of use — 44 reads across 15 of 27 modules,
several of them frozen at module import. Three things followed, and all three are in the tree's own
history:

* one convention with THREE readers — `RASK_CATALOG_DELIMITER` was read by `naming.delimiter()`, by a
  dead `lineage._delimiter()`, and by an import-frozen `catalog_service.DELIMITER`. A delimiter the
  writers disagree about addresses a different table rather than failing.
* knobs fixed per POD, invisible to `kubectl set env` and to any test that sets the variable after
  the module was first imported. `sizing.py` and `workflow.RunLimits` had already been dragged off
  that pattern, one module at a time.
* nothing enumerating what the service reads, so the chart and the code could only be compared by
  grep.

The gate below is the one that keeps it closed: a NEW `os.getenv` anywhere in the plane fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ingest import config as config_mod


SRC = Path(config_mod.__file__).parent

#: The two modules that may still touch `os.environ`, each for a stated reason.
#:
#: `config.py` is the declaration itself — pydantic-settings reads the environment, which is the
#: point. `auth.py` reads `APP_API_TOKEN` at CALL TIME on purpose: `get_auth_settings` is
#: `lru_cache`d (ING-13), so folding the service token onto that model would strand a rotated secret
#: for the life of the process — the exact hazard the field's own comment names.
_ALLOWED = {"config.py", "auth.py"}


def _env_reads(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"))


def test_no_module_outside_the_declaration_reads_the_environment() -> None:
    offenders = {p.name: lines for p in sorted(SRC.glob("*.py")) if p.name not in _ALLOWED and (lines := _env_reads(p))}
    assert offenders == {}, (
        f"{offenders} read the environment directly. Declare the knob on `ingest.config.IngestSettings` "
        f"and read it through `settings()`, so one place names the variable and nothing freezes it at import."
    )


def test_auth_reads_only_the_service_token_directly() -> None:
    """The one allowed read is allowed for a NAMED reason; it must not become a hatch for others."""
    source = (SRC / "auth.py").read_text(encoding="utf-8")
    reads = [line.strip() for line in source.splitlines() if "os.environ" in line and not line.strip().startswith("#")]
    assert reads == ['expected = os.environ.get("APP_API_TOKEN")'], reads


def test_the_catalog_delimiter_has_ONE_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The convention that was read three ways, and one of the three was frozen at import.

    Moving the variable AFTER import must move every reader, or two writers of one convention name
    two different tables.
    """
    import pyarrow as pa

    from ingest.catalog_service import CatalogServiceClient
    from ingest.naming import bronze_table_id, delimiter

    monkeypatch.setenv("RASK_CATALOG_DELIMITER", "~")
    monkeypatch.delenv("MEDALLION_BRONZE_NAMESPACE", raising=False)

    client = CatalogServiceClient(pa.schema([pa.field("id", pa.string())]), base_url="http://catalog.test", token="t")

    assert delimiter() == "~"
    assert client.table_id("bronze", "pages") == "bronze~pages"
    assert bronze_table_id("", "pages") == "bronze~pages"


def test_a_tunable_moved_after_import_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch`'s retry budget and the queue's ack ceiling were both module-level `int(os.getenv(...))`."""
    from ingest.config import settings

    monkeypatch.setenv("RASK_INGEST_HTTP_ATTEMPTS", "7")
    monkeypatch.setenv("RASK_INGEST_MAX_ACK_PENDING", "99")

    assert settings().http_attempts == 7
    assert settings().max_ack_pending == 99

    from ingest.sizing import SizingRefused, resolve

    with pytest.raises(SizingRefused):
        resolve(__import__("ingest.sizing", fromlist=["IngestSizing"]).IngestSizing(fragment_rows=99))


def test_the_replay_GATE_covers_a_settings_read_in_a_workflow_body() -> None:
    """Moving a read behind a function call must not move it out of the determinism gate's sight.

    `RunLimits` records what a workflow body reading live configuration costs: `if max_run_hours > 0`
    decides whether a durable timer exists, so a rolling deploy between a run's first execution and
    its replay produces an action stream the history does not match. The gate caught `os.getenv`; a
    `settings()` call is the same hazard in new clothes.
    """
    from ingest.replay_guard import env_reads_in_workflow_bodies

    source = "from ingest.config import settings\n\n\ndef ingest_run(ctx, payload):\n    if settings().max_units:\n        yield ctx.call_activity(x)\n"
    assert env_reads_in_workflow_bodies(source, {"ingest_run"}) == ["ingest_run"]


def test_the_replay_gate_still_lets_an_ACTIVITY_read_settings() -> None:
    """The sanctioned asymmetry: an activity's result is recorded, so every replay sees it."""
    from ingest.replay_guard import env_reads_in_workflow_bodies

    source = (
        "from ingest.config import settings\n\n\n"
        "def resolve_limits(ctx, payload):\n    return {'u': settings().max_units}\n\n\n"
        "def ingest_run(ctx, payload):\n    yield resolve_limits(ctx, payload)\n"
    )
    assert env_reads_in_workflow_bodies(source, {"ingest_run"}, {"resolve_limits"}) == []


def test_a_blanked_knob_falls_back_to_its_default_rather_than_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`kubectl set env RASK_INGEST_MAX_UNITS=` leaves an EMPTY STRING, and `int("")` raises.

    `RunLimits.from_env` carried its own `or 0` for exactly this, and it was the only read that
    remembered. The guarantee now lives on the model, so every knob has it.
    """
    from ingest.config import settings

    monkeypatch.setenv("RASK_INGEST_MAX_UNITS", "")
    monkeypatch.setenv("RASK_INGEST_HTTP_ATTEMPTS", "")
    monkeypatch.setenv("RASK_CATALOG_URL", "")

    config = settings()
    assert config.max_units == 0
    assert config.http_attempts == 3
    assert config.catalog_url == "http://rask-catalog:2333"


def test_a_MISTYPED_catalog_flag_refuses_instead_of_silently_writing_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag whose false reading is a data incident.

    `os.getenv("RASK_INGEST_USE_CATALOG").lower() in ("1", "true", "yes")` read `"ture"` as FALSE, and
    a false reading means the run writes governed bytes no catalog knows about and no mover will ever
    be told of — the silent local fallback `catalog_enabled`'s own docstring forbids.
    """
    from pydantic import ValidationError

    from ingest.config import settings

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "ture")
    with pytest.raises(ValidationError):
        settings()

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    assert settings().use_catalog is True
