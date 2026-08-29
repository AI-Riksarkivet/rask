"""SK-16 — the annotation emitter logged under another service's logger, and wrote past logging entirely.

`lineage_emit` took `logging.getLogger("lineage")`. That name is the LINEAGE SERVICE's package tree —
the one `obs.configure_app_logging` raises to INFO — while this module runs inside the ANNOTATOR. So
its level was governed by a service that is not in the process, and every record it wrote was
attributed to a service that did not produce it: an operator grepping `lineage` for a lineage-service
problem found annotator output mixed in, and vice versa.

The second half was `MEDIA_LINEAGE_SINK=stdout`, which did `sys.stdout.write(...)`. That put the one
record describing a governed write past the request-id correlation filter, past the level gate and
past the OTLP export — onto the exact stream `setup_logging()` already writes to. The option is gone
(`LineageSink` now holds `log` and `none`); nothing in the estate selected it.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from service_kit.lancekit import lineage_emit
from service_kit.media.config import LineageSink


class _FakeDataset:
    """Enough of a `lance.LanceDataset` for the event builder: a schema, a version, a row count."""

    version = 7
    schema = pa.schema([pa.field("id", pa.string()), pa.field("shapes", pa.string())])

    def count_rows(self) -> int:
        return 3


def _emit(sink: str) -> dict[str, Any] | None:
    return lineage_emit.emit_save(
        # cast: the builder reads only `schema`, `version` and `count_rows()`, and constructing a real
        # `lance.LanceDataset` here would make a logger-name test depend on writing a Lance table.
        ds=cast("lance.LanceDataset", _FakeDataset()),
        table_uri="s3://bucket/annotations.lance",
        table_name="annotations",
        unit_key="unit-1",
        sink=sink,
    )


def test_the_emitter_logs_under_its_own_module_not_the_lineage_service() -> None:
    assert lineage_emit.logger.name == "service_kit.lancekit.lineage_emit"
    assert lineage_emit.logger is not logging.getLogger("lineage")


def test_the_record_carries_the_module_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="service_kit.lancekit.lineage_emit"):
        event = _emit(LineageSink.log)
    assert event is not None
    records = [r for r in caplog.records if r.message.startswith("openlineage ")]
    assert len(records) == 1
    assert records[0].name == "service_kit.lancekit.lineage_emit", "the record still claims another service produced it"


def test_nothing_writes_past_the_logging_system(capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("service_kit.lancekit.lineage_emit").setLevel(logging.CRITICAL)
    try:
        _emit(LineageSink.log)
    finally:
        logging.getLogger("service_kit.lancekit.lineage_emit").setLevel(logging.NOTSET)
    assert capsys.readouterr().out == "", "a record suppressed by the level gate still reached stdout"
    assert not hasattr(lineage_emit, "sys"), "the module still imports sys — the only thing it ever used it for was the stdout bypass"


def test_the_none_sink_still_emits_nothing() -> None:
    assert _emit(LineageSink.none) is None
