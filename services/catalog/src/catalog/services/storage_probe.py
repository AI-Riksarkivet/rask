"""The storage probe's calls on the store: what a warehouse's vended credentials can and cannot do, asked of the store
itself.

`vend_probe` holds the check vocabulary and how a set of results reduces to one verdict; this module makes the calls.
Each step is one call under a credential the catalog vends as it would for a table at the warehouse's location, and
only the store's own answer counts as a verdict: a step the store did not answer is reported as skipped, never passed.
"""

import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from catalog.core.vending import CredentialVendor
from catalog.services.vend_probe import (
    CAS_CHECK,
    CAS_RESERVE,
    ENCRYPTED_WRITE,
    FINAL_MANIFEST_CHECK,
    REF_CHECK,
    SCOPE_CHECK,
    STAGED_MANIFEST,
    STORE_ANSWERED,
    WRITE_TIER_ISSUE,
    ProbeCheck,
)
from lakehouse.storage.client import S3Client, s3_client
from lakehouse.storage.errors import StorageError, StoreUnansweredError, store_call
from lakehouse.storage.options import StorageOptions
from lakehouse.storage.uri import split_s3_uri


#: How much of a failure's text a probe check's detail carries.
_PROBE_DETAIL_CHARS: Final = 160

#: The checks of the probe's write-tier leg, in the order it makes them.
_WRITE_TIER_CHECKS: Final = (WRITE_TIER_ISSUE, STAGED_MANIFEST, FINAL_MANIFEST_CHECK, REF_CHECK)


def _encryption_put_args(options: Mapping[str, str]) -> dict[str, Any]:
    """The ``put_object`` arguments that encrypt as a writer holding vended ``options`` does (``vending.encryption``)."""
    args: dict[str, Any] = {}
    if algorithm := options.get("aws_server_side_encryption"):
        args["ServerSideEncryption"] = algorithm
    if key := options.get("aws_sse_kms_key_id"):
        args["SSEKMSKeyId"] = key
    if (bucket_key := options.get("aws_sse_bucket_key_enabled")) is not None:
        args["BucketKeyEnabled"] = bucket_key == "true"
    return args


def _probe_the_write_tier(
    step: Callable[..., bool],
    vendor: CredentialVendor,
    *,
    bucket: str,
    probe_prefix: str,
    store_endpoint: str,
    session_name: str,
    checks: list[ProbeCheck],
) -> list[str]:
    """Ask the store, under a write-tier credential for ``probe_prefix``, to refuse what that tier is denied; the keys
    the leg landed, for the probe's cleanup.

    The final manifest is put at ``_versions/<n>.manifest-<uuid>.manifest``: a key the tier's staged-commit Allow
    matches as well (``table_layout.WRITER_PUT_GLOBS``), so a store that refuses it is honouring the explicit Deny, and
    one that evaluates Allows alone accepts it. The ref is put under ``_refs/tags/``, which no statement allows. Both
    follow a staged manifest the store accepted, so a refusal is the Deny's and not a credential that cannot write.
    """
    staged = f"{probe_prefix}/_versions/1.manifest-{uuid.uuid4()}"
    final, ref = f"{staged}.manifest", f"{probe_prefix}/_refs/tags/probe.json"
    try:
        vended = vendor.vend(table_location=f"s3://{bucket}/{probe_prefix}", tier="write", session_name=session_name)
        options = None if vended is None else vended.as_storage_options()
    except Exception as exc:  # noqa: BLE001 — any vend failure is one reportable outcome, not a 500
        checks.append(ProbeCheck(name=WRITE_TIER_ISSUE, outcome="fail", detail=f"{type(exc).__name__}: {exc}"))
        options = None
    else:
        checks.append(ProbeCheck(name=WRITE_TIER_ISSUE, outcome="pass" if options is not None else "skip"))
    if options is None:
        checks.extend(ProbeCheck(name=n, outcome="skip", detail="no write-tier credential") for n in _WRITE_TIER_CHECKS[1:])
        return []
    writer = s3_client(store_endpoint or None, key_pair=options.signing_pair(), region=options.region)
    if not step(STAGED_MANIFEST, lambda: writer.put_object(Bucket=bucket, Key=staged, Body=b"scope-probe")):
        checks.extend(ProbeCheck(name=n, outcome="skip", detail="the write tier staged nothing") for n in _WRITE_TIER_CHECKS[2:])
        return []
    landed = [staged]
    if not step(
        FINAL_MANIFEST_CHECK,
        lambda: writer.put_object(Bucket=bucket, Key=final, Body=b"scope-probe"),
        accepted_is="the store accepted a FINAL MANIFEST from a write-tier credential, whose session policy denies it",
    ):
        landed.append(final)
    if not step(
        REF_CHECK,
        lambda: writer.put_object(Bucket=bucket, Key=ref, Body=b"scope-probe"),
        accepted_is="the store accepted a put under _refs/ from a write-tier credential, which no statement of its policy allows",
    ):
        landed.append(ref)
    return landed


def run_scope_probe(root_uri: str, *, vendor: CredentialVendor, store_endpoint: str, session_name: str) -> list[ProbeCheck]:
    """The IO half: vend, write in, read in, write OUT (must fail), ask the write tier's Deny, clean up.

    The probe signs with the vended credential and reaches the store at ``store_endpoint``, the catalog's own
    address: the vended endpoint is the one a client reaches from where it runs ([[LH-489]]), which the catalog's
    pod need not resolve. The credential is named ``session_name`` at the store, for the admin who asked.

    Every step records its own outcome and the ones it makes impossible are SKIPPED rather than
    reported as failures — a probe that could not write has learned nothing about scope, and saying so
    is the difference between "the store is over-permissive" and "the store was unreachable".
    """
    bucket, prefix = split_s3_uri(root_uri)
    probe_prefix = f"{prefix.rstrip('/') + '/' if prefix else ''}_validate/{uuid.uuid4().hex}"
    inside = f"{probe_prefix}/probe"
    # Its own key, inside the same vended prefix: the conditional put must land on a key nothing else
    # wrote, or a leftover from a previous run would make the FIRST put look like a racing writer.
    cas = f"{probe_prefix}/cas"
    sealed = f"{probe_prefix}/encrypted"
    # The PARENT, never a sibling: a credential scoped to `<probe>/` must not reach the warehouse root,
    # and the root is where a policy that was ignored would let it write.
    outside = f"{prefix.rstrip('/') + '/' if prefix else ''}_validate_scope_probe_should_fail"

    vend = _vend_the_probe(vendor, bucket=bucket, probe_prefix=probe_prefix, session_name=session_name)
    options = vend.options
    checks: list[ProbeCheck] = [vend.issued]

    if options is None:
        checks += [
            ProbeCheck(name=n, outcome="skip", detail="no credential")
            for n in ("write_inside", "read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, *_WRITE_TIER_CHECKS, "cleanup")
        ]
        return checks

    s3 = s3_client(store_endpoint or None, key_pair=options.signing_pair(), region=options.region)

    def _step(name: str, fn: Callable[[], object], *, accepted_is: str = "") -> bool:
        # Records the check, and answers whether it got the outcome it wanted.
        check = _probe_step(name, fn, accepted_is=accepted_is)
        checks.extend(_as_reported(check))
        return check.outcome == "pass"

    wrote = _step("write_inside", lambda: s3.put_object(Bucket=bucket, Key=inside, Body=b"scope-probe"))
    if not wrote:
        checks += [
            ProbeCheck(name=n, outcome="skip", detail="nothing was written")
            for n in ("read_inside", SCOPE_CHECK, CAS_RESERVE, CAS_CHECK, *_WRITE_TIER_CHECKS, "cleanup")
        ]
        return checks
    _step("read_inside", lambda: s3.get_object(Bucket=bucket, Key=inside))
    # As the credential tells its writer to encrypt, after a plain write the store took: a store whose role holds no grant
    # on the vended KMS key fails here, a verdict on the store, not the first write to the warehouse's first table
    # ([[LH-262]]). The read back asks the key's decrypt as a reader would.
    _probe_encryption(_step, s3, bucket=bucket, sealed=sealed, options=options)
    _step(
        SCOPE_CHECK,
        lambda: s3.put_object(Bucket=bucket, Key=outside, Body=b"scope-probe"),
        accepted_is="the store accepted a write OUTSIDE the credential's prefix",
    )
    # PUT-IF-NOT-EXISTS, the primitive Lance's commit protocol rests on
    # (`lance_docs/lance/format/table/transaction.md` § "Commit Protocol" -> "Storage Primitives"). Two conditional
    # puts at one key: the first must be ACCEPTED and the second REFUSED. A store that accepts
    # both has no "exactly one writer succeeds" guarantee, so two concurrent Lance writers would
    # each believe they committed the same manifest version — with a 200 on both and no error
    # anywhere. That is the failure this step exists to name, and it is why the SECOND put is the
    # check rather than the first.
    if (unmeasured := _probe_put_if_not_exists(_step, s3, bucket=bucket, cas=cas)) is not None:
        checks.append(unmeasured)

    # [[LH-511]] THE WRITE TIER'S DENY, asked of a write-tier credential for the same prefix. Managed versioning
    # (ADR 0157) rests on the store refusing a writer the final manifest and every ref; a store that takes the
    # session policy and ignores its Deny would let any writer finalize a version past `version/create`.
    landed = _probe_the_write_tier(
        _step, vendor, bucket=bucket, probe_prefix=probe_prefix, store_endpoint=store_endpoint, session_name=session_name, checks=checks
    )

    def _clean() -> None:
        # Under the probe tier: the write tier deletes nothing, what it landed included.
        for key in (inside, cas, sealed, *landed):
            s3.delete_object(Bucket=bucket, Key=key)
        # Only a write the store ACCEPTED, the scope check failed, left an object at the warehouse root, and only then
        # is it deleted: a correctly scoped credential is refused the delete too, and reporting that refusal as a
        # cleanup failure would raise a false alarm about the store that just passed. A write the store never answered
        # is not chased either: nothing says it landed, and a store that took it is found, and the object removed, by
        # the next run.
        if any(check.name == SCOPE_CHECK and check.outcome == "fail" for check in checks):
            s3.delete_object(Bucket=bucket, Key=outside)

    _step("cleanup", _clean)
    return checks


class _ProbeVend(BaseModel):
    """The probe tier's credential, ``None`` when nothing was vended, and the check its issue is."""

    model_config = ConfigDict(frozen=True)

    options: StorageOptions | None
    issued: ProbeCheck


def _vend_the_probe(vendor: CredentialVendor, *, bucket: str, probe_prefix: str, session_name: str) -> _ProbeVend:
    """The probe tier's credential for ``probe_prefix``, ``None`` when nothing was vended, and the check its issue is."""
    try:
        vended = vendor.vend(table_location=f"s3://{bucket}/{probe_prefix}", tier="probe", session_name=session_name)
        # The probe signs as the client a door hands the vend to, so it holds what that door would answer ([[LH-072]]).
        options = None if vended is None else vended.as_storage_options()
    except Exception as exc:  # noqa: BLE001 — any vend failure is one reportable outcome, not a 500
        issued = ProbeCheck(name="issue", outcome="fail", detail=f"{type(exc).__name__}: {exc}")
        options = None
    else:
        issued = (
            ProbeCheck(name="issue", outcome="pass")
            if options is not None
            else ProbeCheck(name="issue", outcome="skip", detail="server_mediated: this warehouse vends no direct credentials")
        )
    return _ProbeVend(options=options, issued=issued)


def _probe_step(name: str, fn: Callable[[], object], *, accepted_is: str = "") -> ProbeCheck:
    """Run one check, ``fn``, which must go through, or which the store must refuse where ``accepted_is`` says why
    accepting it is wrong. The check is ``skip`` where the store did not answer the step, and only there.

    A VERDICT IS ONLY EVER THE STORE'S OWN ANSWER. A request it did not answer measured nothing: read as the refusal a
    check wanted, an outage would pass a control nothing tested, and read as a failure it would fail a store that broke
    no guarantee. The report says beside such a check that the probe did not finish (:func:`_as_reported`).
    """
    expect_refusal = bool(accepted_is)
    try:
        with store_call(f"the probe's {name}"):
            fn()
    except StoreUnansweredError as exc:
        return ProbeCheck(name=name, outcome="skip", detail=str(exc)[:_PROBE_DETAIL_CHARS])
    except StorageError as exc:
        if expect_refusal:
            return ProbeCheck(name=name, outcome="pass")
        return ProbeCheck(name=name, outcome="fail", detail=f"{type(exc).__name__}: {exc}"[:_PROBE_DETAIL_CHARS])
    return ProbeCheck(name=name, outcome="fail", detail=accepted_is) if expect_refusal else ProbeCheck(name=name, outcome="pass")


def _as_reported(check: ProbeCheck) -> list[ProbeCheck]:
    """``check`` as the report holds it: beside a step the store did not answer, a failed :data:`STORE_ANSWERED` carrying
    what the step's own check says of it, so the report fails on a probe that did not finish and on no verdict."""
    if check.outcome != "skip":
        return [check]
    return [check, ProbeCheck(name=STORE_ANSWERED, outcome="fail", detail=check.detail)]


def _probe_encryption(step: Callable[..., bool], s3: S3Client, *, bucket: str, sealed: str, options: StorageOptions) -> None:
    """Write and read ``sealed`` encrypted as the credential tells its writer to, when it tells it to at all."""
    if encrypted := _encryption_put_args(options.options):

        def _round_trip() -> None:
            s3.put_object(Bucket=bucket, Key=sealed, Body=b"scope-probe", **encrypted)
            s3.get_object(Bucket=bucket, Key=sealed)

        step(ENCRYPTED_WRITE, _round_trip)


def _probe_put_if_not_exists(step: Callable[..., bool], s3: S3Client, *, bucket: str, cas: str) -> ProbeCheck | None:
    """Two conditional puts at ``cas``, the first accepted and the second refused; the check left unmeasured, if any."""
    reserved = step(CAS_RESERVE, lambda: s3.put_object(Bucket=bucket, Key=cas, Body=b"first", IfNoneMatch="*"))
    if not reserved:
        # The first conditional put did not go through: the store refused the HEADER, or did not answer. Either is
        # recorded by the step above, and measures nothing about a second writer, so the claim stays UNKNOWN
        # rather than becoming a verdict the probe never reached.
        return ProbeCheck(name=CAS_CHECK, outcome="skip", detail="the first conditional put did not go through")
    step(
        CAS_CHECK,
        lambda: s3.put_object(Bucket=bucket, Key=cas, Body=b"second", IfNoneMatch="*"),
        accepted_is="the store accepted a second conditional put at a key that exists",
    )
    return None
