"""The rendered manifest stays inside a budget, because Helm's release Secret has a HARD 1 MiB cap.

[[XC-054]] AND IT STOPPED BEING THEORETICAL. Measured 2026-09-21: `make k3s-up` failed outright with
`Secret "sh.helm.release.v1.rask.v194" is invalid: data: Too long: may not be more than 1048576 bytes`,
and revision 193 decoded to **1,041,774 bytes of 1,048,576 — 99.35%, 6,802 bytes of headroom**. Every
`helm upgrade` was refused, by anyone, for any change. The estate was undeployable and the next commit
was always going to be the one that did it.

THE CAP IS NOT NEGOTIABLE. Helm stores each release in a Kubernetes Secret; Kubernetes stores Secrets
in etcd and refuses any object past 1 MiB. There is no chart value, no flag and no storage backend
setting that raises it — only making the release smaller works.

WHY THE MANIFEST IS THE RIGHT THING TO GATE. Decoding the live release object:

    manifest (rendered YAML)   2,414,246   62%
    chart (source)             1,454,216   37%
    hooks                         57,638    1%

The manifest dominates, and it is the half this repo grows every time a template gains prose — because
Helm COPIES a `#` comment into the rendered output, where a `{{/* */}}` comment is stripped. That is
also why the fix cost no rationale: converting the spelling removed 38,540 gzipped bytes and left every
word in the template file. `.helmignore` records the SAME failure at revision 34 on 2026-08-18, fixed
then by moving 1.19 MB of CNPG CRDs out; it regrew in a month, which is why a measurement without a
gate was not enough.

THE BUDGET IS THE MEASUREMENT PLUS ROOM TO WORK, not a number chosen to pass. After the conversion the
manifest gzips to 263,848 bytes and the live release holds 43,858 bytes of headroom. The cap below is
set ~14% above the current figure: large enough that ordinary work never trips it, small enough that
the ~66 KB of drift that consumed the last margin cannot happen unnoticed.

WHAT THIS CANNOT SEE, stated rather than implied: the Secret also holds the chart source and the
hooks, and this gate measures only the manifest. A chart that doubled its non-template files would
slip past. The manifest is where this estate's growth has actually happened, twice.
"""

from __future__ import annotations

import gzip

from tests.unit.chart_render import DEFAULT_ARGS, render, render_text


#: Gzipped bytes the rendered default-overlay manifest may occupy. Measured 2026-09-21 at 263,848.
MANIFEST_GZIP_BUDGET = 300_000

#: `#` comment bytes the rendered manifest may carry. Measured at 88,253 after the conversion, against
#: 240,362 before it. This is the number that regrew twice, so it is gated directly rather than only
#: through its compressed effect — gzip hides prose drift by compressing repetition very well.
MANIFEST_COMMENT_BUDGET = 110_000


def _manifest() -> str:
    """Helm's RAW output — never re-serialized from parsed documents.

    `yaml.safe_load` drops comments, so a manifest rebuilt from parsed docs carries none and both
    budgets below would measure a document that is not what Helm stores. Measured 2026-09-21: the first
    version of this file did exactly that and passed with three un-converted templates restored, which
    is a gate that cannot fail.
    """
    return render_text(*DEFAULT_ARGS)


def test_the_render_is_big_enough_to_be_worth_gating() -> None:
    """Without this, a render that collapsed to nothing would pass both budgets silently."""
    assert len(render(*DEFAULT_ARGS)) > 100, "the chart rendered almost nothing; these budgets check nothing"


def test_the_rendered_manifest_fits_its_gzip_budget() -> None:
    """The compressed size is what actually consumes the Secret, so it is what the budget is set on."""
    size = len(gzip.compress(_manifest().encode(), 9))

    assert size <= MANIFEST_GZIP_BUDGET, (
        f"the rendered manifest gzips to {size:,} bytes against a budget of {MANIFEST_GZIP_BUDGET:,}. "
        "Helm's release Secret is capped at 1,048,576 bytes by etcd and cannot be raised; at 99.35% of it "
        "every `helm upgrade` was refused. Convert `#` comments in templates to `{{/* */}}` — the prose "
        "stays in the file and stops being copied into the manifest."
    )


def test_the_rendered_manifest_does_not_refill_with_comment_prose() -> None:
    """Gated directly because gzip HIDES this: repeated prose compresses away, then stops compressing.

    `frontends.yaml` carried 48,641 comment bytes into the render because its prose is emitted once per
    zone, yet removing all of it saved only 3,263 gzipped bytes. Watching the compressed number alone
    would let prose accumulate for a long time and then move the total suddenly.
    """
    comment_bytes = sum(len(line) + 1 for line in _manifest().splitlines() if line.lstrip().startswith("#"))

    assert comment_bytes <= MANIFEST_COMMENT_BUDGET, (
        f"the rendered manifest carries {comment_bytes:,} bytes of `#` comments against a budget of "
        f"{MANIFEST_COMMENT_BUDGET:,}. Helm copies these into the release Secret forever; a `{{/* */}}` "
        "comment is stripped at render time and costs nothing."
    )
