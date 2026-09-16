"""A base the create door accepted must survive into the credential the vend door issues.

`_base_is_sanctioned`'s own docstring states the asymmetry it exists to close — "The create door
enforced that list and the vend door did not" — and it closed it for ONE of the estate's two approved-
base allowlists. `LANCE_MULTIBASE_DATA_BASES` reached the vendor; `LANCE_EXTERNAL_BLOB_BASES`, the
allowlist that decides which external `Blob.from_uri` pointers a create may reference at all, did not.

MEASURED ON THE DEPLOYED CATALOG 2026-09-16 (image `main-9e5ff5b3`): the pod carries
`LANCE_EXTERNAL_BLOB_BASES=s3://lance-catalog/models/` and emitted **1,836**
`vend_base_path_unsanctioned` warnings in three hours, every one of them for that same base, against
tables in unrelated buckets (`tracka-wh`, `cslens0d7def-wh`, `acme-bucket`) and always with
`sanctioned_count=0` — because the only list the vendor was given is empty on that pod.

WHY IT IS WORTH A GATE RATHER THAN A ONE-LINE EDIT: `vending.py:271-281` drops the base instead of
raising, deliberately, so the failure is remote from its cause. Its own comment says so — "a dropped
base surfaces later as a read denial at the object store, on whoever used the credential, with nothing
naming the base". A regression here is therefore invisible at the vend and diagnosable only from the
reader's side, which is the shape that survives a release.

The union belongs on the settings object, not at the call site: the two lists live there together, and
a derivation the vendor's caller performs inline is one nobody can test without standing up a lifespan.
"""

from __future__ import annotations

from catalog.core.config import Settings


def test_an_approved_external_blob_base_is_sanctioned_for_vending() -> None:
    """The deployed shape: a models root the create door approves and the vend door dropped."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_EXTERNAL_BLOB_BASES="s3://lance-catalog/models/")

    assert "s3://lance-catalog/models/" in settings.vend_sanctioned_bases


def test_the_operator_data_base_allowlist_still_reaches_the_vendor() -> None:
    """The half that already worked has to keep working — this widens the union, it does not move it."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_MULTIBASE_DATA_BASES="s3://other-wh/data/")

    assert "s3://other-wh/data/" in settings.vend_sanctioned_bases


def test_both_allowlists_arrive_together() -> None:
    settings = Settings(
        LANCE_S3_ACCESS_KEY_ID="k",
        LANCE_S3_SECRET_ACCESS_KEY="s",
        LANCE_MULTIBASE_DATA_BASES="s3://other-wh/data/",
        LANCE_EXTERNAL_BLOB_BASES="s3://lance-catalog/models/",
    )

    assert set(settings.vend_sanctioned_bases) == {"s3://other-wh/data/", "s3://lance-catalog/models/"}


def test_sanctioning_nothing_stays_the_default() -> None:
    """Empty must remain empty: the guard's value is that an unlisted foreign base is refused, and a
    union that invented an entry would open the data-exfil door the allowlist exists to close."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")

    assert list(settings.vend_sanctioned_bases) == []
