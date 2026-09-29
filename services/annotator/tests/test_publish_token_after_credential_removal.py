"""The publish saga must still mint an identity after the shared service credential was deleted.

REGRESSION, found by an adversarial re-audit of this drain's own closures — not by the suite, which
was green.

Closing the finding "`open_reader`/`open_writer` fall back to the estate's catalog service
credential" deleted `catalog_token` from the media settings, and its closing note asserted that "the
credential had no other consumer in the tree". That was false. `publish_token` read
`settings.catalog_token` as its FIRST precedence step, so after the field was removed every call
raised `AttributeError: 'AnnotatorSettings' object has no attribute 'catalog_token'` — and
`run_publish_for` calls it on every publish with a configured catalog. The annotator's publish path
was broken and nothing failed.

Nothing failed because no test called `publish_token` with a real `AnnotatorSettings`. That is the
gap this file closes: the function is exercised against the settings object it is actually handed in
production, so a field it depends on cannot be deleted from under it again.

The FIX is to delete the branch, not restore the field. A pinned estate-wide service credential as a
publish identity is precisely what the original finding refused — every published row would carry the
platform's identity instead of the publisher's. The remaining precedence (mint from the IdP, else
anonymous) is the shape that finding argued for.
"""

from __future__ import annotations

from annotator.core.config import AnnotatorSettings
from annotator.projects import lakehouse


def test_publish_token_survives_the_settings_it_is_actually_given() -> None:
    """The regression itself: a plain settings object made the publish path raise."""
    assert lakehouse.publish_token(AnnotatorSettings()) is None, "an auth-off stack must publish anonymously, as the docstring promises"
