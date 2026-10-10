# 0057. A dependency revert that leaves the range open reverts nothing (2026-09-07)

Bumping `lance-namespace` 0.11.0 → 0.12.0 (A10) broke `merge_insert` on the catalog's native path,
and both halves of what happened next are worth keeping.

**The break is a MUTUAL incompatibility between two halves of the same project, and it is invisible
from either side alone.** lance-namespace 0.12.0 types `MergeInsertIntoTableRequest.on` as
`List[str]` and REJECTS a string; pylance 10.0.0's native backend types it as a string and raises
`TypeError: unexpected type: 'list' object is not an instance of 'str'` from `lance/namespace.py:580`.
Neither library is wrong on its own terms. Measured in both directions, because a single direction
would have read as a rask defect: with that pair, `merge_insert` through the native path cannot work
for ANY value of `on`, not merely for the composite key the bump was wanted for.

**The catalog's own 320 tests passed on the broken pair.** They drive a `TestClient` against a mocked
namespace, so the layer that refuses is the layer they replace. Only the integration suite — which
drives the real dataplane over moto — could see it. A green service suite is not evidence about a
dependency bump; the suite that exercises the dependency is.

**Then the revert did not revert.** Putting the floor back to `>=0.11.0` left 0.12.0 inside the
allowed range, so the very next resolve took it again and the tests failed identically. A version
range is not a pin: reverting a bump means moving the CEILING, and the pins now carry
`>=0.11.0,<0.12` with the measurement written beside them. Anything else is a revert that survives
exactly until the next `uv lock`.

**What the row became.** A10 was written as "bump the pin and re-vendor the docs". It is a JOINT bump
of lance-namespace and pylance (11.0.0 exists), which is a major-version move of the core columnar
format library across every service that reads a dataset — a change with its own verification, not a
pin edit. Measuring the row changed what the row was.
