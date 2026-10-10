# 0056. A control's NAME is not evidence that it exists (2026-09-07)

Draining §F2 produced three findings in one day that are the same mistake wearing different clothes,
and none of them was settled by reading the thing that carried the name:

  * **A field that WAS passed and was read by nothing.** `MEDALLION_RAY_S3_ACCESS_KEY_ID=rask-ray-compute`
    sat on three live Deployments and was quoted by `open_goal.md`, by a backlog row and by a test's
    own docstring as proof the Ray lane ran scoped. No service read it; no template rendered it. The
    control was real, on the Ray POD, and the variable was residue.
  * **A field NEVER passed that is stamped anyway.** "Audit records carry no request or trace id" is
    true of all 118 `audit()` call sites and false of every record, because `CorrelationFilter` is
    installed on the ROOT handler and stamps both.
  * **A FUNCTION NAMED for the control it does not implement.** `.dagger/images.go`'s `provenance()`
    emits three OCI labels — `BUILD_DATE`, `VCS_REF`, `VERSION`. No SBOM, no signature, no attestation.

**The rule: verify a control where its value LANDS, not where its name appears.** For a credential,
that is the request on the wire; for an env var, the settings field that binds it; for a log field,
the record; for provenance, the artifact. Every one of these took a single command to settle — a
grep for the reader, a render of the seed, a look at the filter's install point — and each had stood
unchallenged in prose for weeks.

**The corollary is about counting.** A sweep that greps for a name produces a count, and the count is
what gets scheduled. Two of these rows were scheduled work that did not need doing; one was a control
credited to the estate that was not there. Measuring a row before scheduling it changed the answer
about as often as fixing it did — which is the case for demanding a verdict per row rather than a
status.
