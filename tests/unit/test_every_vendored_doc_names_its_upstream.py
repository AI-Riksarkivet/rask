"""Every vendored `lance_docs/` file names the upstream revision it can be checked against.

[[LH-047]]. This tree is cited as the authority for what is idiomatic to lance-namespace, and the
standing rule is to read it and cite it. A bundle with no upstream revision cannot be checked by the
reader it is cited to, so the citation rests on nothing — and this estate has already been bitten by
that: `file_format.md` names five feature flags where `rust/lance-table/src/feature_flags.rs`
allocates eleven, which is why `service_kit.lakehouse.features` reads the Rust source instead.

DERIVED FROM THE DIRECTORY, NOT A LIST. A bundle dropped in tomorrow is covered by existing, which is
the failure this closes — the previous table named the files someone remembered to add.

OFFLINE BY CONSTRUCTION. It asserts that a revision is RECORDED, never that it is current: drift
against upstream is measured in weeks and belongs to the network-gated spec conformance suite, and a
gate that fails on a laptop with no network is one people learn to skip.

`lance_sdk.md` is rendered from a docs SITE, which has no revision to name. That is a property of the
source, so the accepted answer there is the explicit admission rather than a commit — recorded so the
weakest citation in the tree is the one that says so.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "lance_docs"
PROVENANCE = DOCS / "PROVENANCE.md"

#: A 7+ character hex sha in a table cell.
_COMMIT = re.compile(r"`[0-9a-f]{7,40}`")
#: The accepted answer for a source that HAS no revision.
_NO_REVISION = "**no commit exists**"


def _vendored() -> list[pathlib.Path]:
    """The bundles a reader cites directly. `ns_catalog/` moves with `spec.yaml` and is covered by it."""
    return sorted(p for p in DOCS.iterdir() if p.is_file() and p.name != "PROVENANCE.md")


def test_every_vendored_document_is_named_in_the_provenance_file() -> None:
    text = PROVENANCE.read_text(encoding="utf-8")

    unnamed = sorted(p.name for p in _vendored() if p.name not in text)

    assert not unnamed, f"these vendored documents are cited by nothing that says where they came from: {unnamed}"


def test_every_named_document_carries_a_revision_or_says_why_it_cannot() -> None:
    """The row's own close condition — a name without a revision is the defect, not the fix."""
    rows = [ln for ln in PROVENANCE.read_text(encoding="utf-8").splitlines() if ln.startswith("| `")]
    offenders = [ln.split("|")[1].strip() for ln in rows if not _COMMIT.search(ln.split("|", 2)[-1]) and _NO_REVISION not in ln]

    assert not offenders, f"these rows name a document but no upstream revision, so a citation from them cannot be checked against anything: {offenders}"


def test_the_walk_actually_found_the_bundles() -> None:
    """Without this, an empty or moved directory would make both gates above pass vacuously."""
    found = {p.name for p in _vendored()}

    assert {"file_format.md", "guide.md", "namespace.md", "ray.md", "lance_sdk.md"} <= found, f"only found {sorted(found)}"
