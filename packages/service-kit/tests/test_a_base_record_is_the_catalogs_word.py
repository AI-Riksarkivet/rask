"""[[LH-279]] The one predicate every door, the pre-pass and the reconcile judge a declared base with.

A base is honoured when it is inside the table's own root (a branch, a same-root clone), inside an
operator-configured external blob base declared as a plain base, or named by the catalog's record for
the table — path AND root-ness. Everything else is unrecorded. Each case below is one branch of that
decision, as measured on pylance 12.0.0: ``<own>/../victim`` is textually inside the own root and
pylance reads the victim through it (lh279 s4), and a name that merely extends the table's is a sibling.
A spelling the object store resolves elsewhere before opening is not the own root either: pylance
percent-decodes a base once and resolves its dot segments, and strips an ASCII control character, so
``<own>/%2e%2e/<victim>`` and ``<own>/.\\t./<victim>`` open the victim and a configured
``<models>/%2e%2e/<victim>`` opens through the configured prefix (lh279 r3 a_escape) — each is unrecorded.
The same single decode is why a ``%`` alone decides nothing: the catalog percent-encodes a table's name
into its location (``räksmörgås`` -> ``r%C3%A4ksm%C3%B6rg%C3%A5s``, ``growth%`` -> ``growth%25``) and a
branch of that table declares the root spelled so (lh279 r3b names), and ``%252e%252e`` decodes once to
the literal ``%2e%2e`` that pylance opens as a name (lh279 r5 escape). A decode that adds a ``/`` or ``\\`` to a segment
changes where the segments fall, and escapes that decode to bytes that are not UTF-8 are a path pylance
refuses to open (lh279 r5 not_utf8), so neither names a location.
A base resolves in the store its OWN spelling names, not the table's: on an S3 table ``file://`` and a
bare ``/path`` read the process's local filesystem and ``S3://`` reads the same store as ``s3://``
(lh279fx probe_scheme_resolve), so a path is own, configured or recorded only in the same store.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse import base_registry
from service_kit.lakehouse.features import BasePathRef


_TABLE = "s3://lance-catalog/aa11bb22_ns$t"
#: Tables named ``räksmörgås`` and ``growth%``, at the locations the catalog's create door gives them.
_SWEDISH = "s3://lance-catalog/0e387b68_ns$r%C3%A4ksm%C3%B6rg%C3%A5s"
_PERCENT = "s3://lance-catalog/27e373bc_ns$growth%25"
_CONFIGURED = ["s3://lance-catalog/models/"]
_RECORD = base_registry.BaseRecord(
    location=_TABLE,
    entries=[base_registry.RecordedBase(path="s3://second-store/data", role=base_registry.BaseRole.DATA, origin=base_registry.BaseOrigin.CREATE)],
)

_OWN = base_registry.BaseStanding.OWN
_CONFIGURED_STANDING = base_registry.BaseStanding.CONFIGURED
_RECORDED = base_registry.BaseStanding.RECORDED
_UNRECORDED = base_registry.BaseStanding.UNRECORDED


@pytest.mark.parametrize(
    ("table", "path", "is_dataset_root", "standing"),
    [
        pytest.param(_TABLE, f"{_TABLE}/tree/work", True, _OWN, id="own"),
        pytest.param(_TABLE, "S3://lance-catalog/aa11bb22_ns$t/tree/work", True, _OWN, id="own-scheme-in-another-case"),
        pytest.param(_TABLE, "file:///lance-catalog/aa11bb22_ns$t/tree/work", True, _UNRECORDED, id="own-path-in-another-store"),
        pytest.param(_TABLE, "/lance-catalog/aa11bb22_ns$t/tree/work", True, _UNRECORDED, id="own-path-with-no-scheme"),
        pytest.param(_TABLE, f"{_TABLE}-evil", True, _UNRECORDED, id="own-name-extended"),
        pytest.param(_TABLE, f"{_TABLE}/../bb33cc44_ns$victim", True, _UNRECORDED, id="dotdot"),
        pytest.param(_TABLE, f"{_TABLE}/%2e%2e/bb33cc44_ns$victim", True, _UNRECORDED, id="own-encoded-dotdot"),
        pytest.param(_TABLE, f"{_TABLE}/.\t./bb33cc44_ns$victim", True, _UNRECORDED, id="own-control-char-dotdot"),
        pytest.param(_TABLE, "s3://lance-catalog/models/some-model", False, _CONFIGURED_STANDING, id="configured"),
        pytest.param(_TABLE, "s3://lance-catalog/models/%2e%2e/bb33cc44_ns$victim", False, _UNRECORDED, id="configured-encoded-dotdot"),
        pytest.param(_TABLE, "s3://lance-catalog/models/some-model", True, _UNRECORDED, id="configured-as-dataset-root"),
        pytest.param(_TABLE, "gs://lance-catalog/models/some-model", False, _UNRECORDED, id="configured-path-in-another-store"),
        pytest.param(_TABLE, "s3://second-store/data", False, _RECORDED, id="recorded"),
        pytest.param(_TABLE, "s3://second-store/data", True, _UNRECORDED, id="recorded-other-root-ness"),
        pytest.param(_TABLE, "https://second-store/data", False, _UNRECORDED, id="recorded-path-in-another-store"),
        pytest.param(_TABLE, "s3://lance-catalog", False, _UNRECORDED, id="unrecorded-bucket-root"),
        pytest.param(_SWEDISH, _SWEDISH, True, _OWN, id="own-root-named-non-ascii"),
        pytest.param(_PERCENT, _PERCENT, True, _OWN, id="own-root-named-with-a-percent"),
        pytest.param(_TABLE, f"{_TABLE}/%252e%252e/bb33cc44_ns$victim", True, _OWN, id="own-double-encoded-dots-decoded-once"),
        pytest.param(_TABLE, f"{_TABLE}/a%2Fb", True, _UNRECORDED, id="own-encoded-slash"),
        pytest.param(_TABLE, f"{_TABLE}/a%5Cb", True, _UNRECORDED, id="own-encoded-backslash"),
        pytest.param(_TABLE, f"{_TABLE}/t%FF", True, _UNRECORDED, id="own-escapes-not-utf8"),
    ],
)
def test_a_declared_base_has_the_standing_its_location_earns(table: str, path: str, is_dataset_root: bool, standing: base_registry.BaseStanding) -> None:
    judged = base_registry.judge_base(table, BasePathRef(path=path, is_dataset_root=is_dataset_root), configured=_CONFIGURED, record=_RECORD)

    assert judged.standing is standing
