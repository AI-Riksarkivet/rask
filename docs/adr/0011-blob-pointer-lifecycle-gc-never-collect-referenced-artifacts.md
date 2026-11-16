# 0011. blob-pointer-lifecycle GC — never collect referenced artifacts

**Status:** retired: the model artifact tree and its janitor is gone from the lakehouse (the catalog serves no model registry, LH-558); kept as the record of the decision.

**Decision.** GC of model/artifact objects (`models/<m>/<token>/` left by crashed runs) must **never**
collect an object still referenced by the registry; only orphaned crashed-run tokens are swept.
`scripts/model_artifact_janitor.py` ships dry-run-by-default with a `referenced ⇒ never-collected` unit pin.

**Rationale.** Pointer-aware GC is the safety property that keeps background maintenance from deleting live
data. The live drive of the broader pointer-aware posture (external-base blobs, AutoCleanupConfig-vs-sweep)
remains a §9 residual.
