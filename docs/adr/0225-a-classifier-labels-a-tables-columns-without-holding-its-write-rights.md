# 0225. A classifier labels a table's columns without holding its write rights (2026-11-21)

**Status:** accepted. Source: XC-209's conversion of the classifier tests to a real OpenFGA, decided by the owner on
2026-11-21: "Classify without write rights". The reference row is ADR 0070's on column-level classification
([[LH-058]]): "Independent of `modify` (separation of duties: classify without holding data/DDL rights)".

## Context

A column's classification is field metadata under the platform prefix (`rask.classification`), written through
`update_field_metadata`. The model gives the table a `classifier` rung (`can_classify`), and each value a
`classification:<value>` object whose `apply` delegates it ([[LH-055]], [[LH-207]]). The door asked both.

Measured on a real OpenFGA (`services/catalog/tests/test_only_a_classifier_may_write_a_governance_key.py`): the router
judged `update_field_metadata` as a writer's door, `can_write_data`, before the door asked `can_classify`. A classifier
holding no writer rung was refused 403 code 15, so classifying needed write rights, which the separation of duties
exists to avoid.

## Decision

The rung of `update_field_metadata` is decided by what its body changes.

- A body that changes governance keys and nothing else (every update names at least one key, every key it names is under
  `rask.`, and no update sets `replace`) asks `can_classify` on the table in `can_write_data`'s place, and the door asks
  `can_apply` on each non-null value, as before.
- A body that changes any ordinary key, alone or beside a governance key, or replaces a field's metadata (which drops
  every key it does not name), asks `can_write_data`, and the door asks `can_classify` and `can_apply` when it touches
  the governance namespace, as before.
- A body the router cannot read as one of these keeps the writer's rung.

The router decides it (`catalog.api.fga_deps._write_door`, through `_classifies_only`), as it decides a grant's rung
from the body and a rename's from its destination: the caller is authenticated from its headers before the body is read
(`lakehouse.governed.admission`, ADR 0203), and `authorize` reads the parsed body Starlette caches. The write judge then
judges the write as before: the read-only window and the run rule apply to a classification as to any write.

## Consequences

- A classifier labels and clears labels on a table holding no writer rung; it still reads no row and writes none.
- A writer who is not a classifier is refused every governance key, as before.
- No request is admitted on fewer rights than before except the governance-only one, which needs the classify right
  instead of write. A caller holding nothing on the table is refused 403 code 15 whatever its body; the refusal names
  the rung its body asked, which depends on the body and not on whether the table exists, and a name holding no table
  is refused alike (`_require_rung`).
- A classifier may also CLEAR any governance key with no value grant (`apply` attaches and never detaches,
  [[LH-055]]). That includes removing `rask.classification=restricted`, which makes the table vendable as raw bytes
  again to the readers it already has; the classifier itself still reads no row.
- The request's `branch` is honoured: a classifier labels and clears on any existing branch, as the writer rung did.
- No schema is disclosed that the caller could not already see. Lance's refusal of a missing path lists the table's
  fields and suggests the nearest, and a classifier holds no `can_get_metadata`. So when a path is missing, the door
  asks the caller's `can_get_metadata` (on that refusal only, writing no denial; an outage answers as not seeing), and
  one without it is told 404 code 12 naming only the paths it sent. A caller who sees the table gets Lance's message
  and the field list as before. **The stated limit:** a 200 on an existing path and a 404 on a missing one still let a
  classifier test names one at a time; the door has to tell it whether its write landed. The full answer is a describe
  rung that sees the schema without reading data, parked as #790 (LH-570, "The privilege ladder lacks describe").
- A client sees one change: a governance-only `update_field_metadata` refused names `can_classify required on table:…`
  where it named `can_write_data`.

## What the decision also means (2026-11-22)

An independent review of the merged change (2026-11-22, no blocking finding) named four further consequences of
admitting a caller on `can_classify` alone. Each holds by design; none is a defect this ADR leaves open.

- **What such a caller learns beyond the path limit.** A classifier holding no `can_get_metadata` also learns which
  branches exist (a classify request on an absent branch answers 404 code 22), the table's current version (the 200
  body names the version its write committed), and, when it holds the rung by inheritance from a namespace, which table
  names exist beneath it (a name holding no table answers differently from one that does). What a caller may learn
  about a table without reading it is the describe rung's question, parked as #790 (LH-570, "The privilege ladder lacks
  describe"), and it is answered there for every door at once rather than per door here.
- **Every classify request commits a version.** A label is field metadata, and Lance changes field metadata by
  committing a new version, so a caller holding no write rung adds versions to the table and contends with its writers
  on the commit like any other write (a conflicting commit is retried or refused as for a writer).
- **A label pins its column.** Any `rask.<key>` the caller may apply is accepted with any value it holds `apply` on,
  and every `rask.*` field key is a label the catalog keeps: alter and drop of that column are then refused until an
  owner or a classifier clears it. A classifier can therefore stop a column being altered or dropped, which is what a
  label is for.
- **Lineage takes the catalog's word for the author.** Lineage records the catalog-attested announcement of a
  classify-only commit without re-deriving the author's write rung, because the catalog already decided it. An estate
  that enforced no event signing would make lineage ask `can_write_data` of the author instead, and refuse the
  announcement of a classifier's commit; the chart always lists signers, so the estate it renders signs.
