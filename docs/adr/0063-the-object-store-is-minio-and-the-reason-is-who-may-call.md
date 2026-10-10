# 0063. The object store is MinIO, and the reason is who may call AssumeRole (2026-09-11, LH-133)

P4 resolved the store question as *"RustFS: rask's operator Tenant wins; keep-PVC posture via Tenant
spec"*, and that held for months. It was reopened by a measurement, not a preference.

**What was measured.** The catalog was the last service presenting the storage ROOT key, and the fix
everyone assumed was available — give it a scoped user whose policy the warehouse registry maintains,
and let every STS vend inherit that policy — turned out not to be available at all. Same request
shape, same pod, same endpoint, one run:

    RustFS   scoped user (policy attached)  -> HTTP 403
    RustFS   root (rustfsadmin)             -> ACCEPTED (487-char session token)
    MinIO    scoped user (policy attached)  -> ACCEPTED (484-char session token)
    MinIO    root (minioadmin)              -> ACCEPTED

And the right cannot be granted on RustFS: `mc admin policy create` refuses a statement carrying
`sts:AssumeRole` outright — *"invalid resource, type: 'unknown'"* — so its policy engine has no
vocabulary for the action. On MinIO the narrowing is enforced as well as issued: vending as the scoped
user with a session policy bound to one prefix, in-scope GET allowed, cross-tenant GET and read-tier
PUT both `AccessDenied`.

**Why that decided it.** Under `vending.mode: sts` the catalog's root key was a REQUIREMENT of the
mode rather than an oversight — on a store where only root may vend, no amount of policy work removes
it. This is NOT the ARN-bound-roles feature on RustFS's roadmap: letting a non-root user assume at all
is a different capability, and it is the one the estate needs.

**What the swap cost, measured rather than estimated.** The data plane was already portable — 40
Python files mentioned the old store and all but one config default were prose, because
`packages/storage` is generic by construction. The surface was the chart: 25 templates, 78 values
lines, and the vendored operator.

**A StatefulSet rather than another operator Tenant.** P4 pinned the keep-PVC posture to a Tenant
spec; a StatefulSet's `volumeClaimTemplates` has the identical property, so the guarantee is kept
while the mechanism drops a CRD set to install, version and keep in step. A multi-node topology can
introduce the MinIO operator later without touching a consumer, because every consumer resolves a
Service name.

**Two controls that survived the rename only because they were checked.** The prod-credentials guard
compares the configured secret against the DEFAULT to refuse a real deployment running on it — left
naming `rustfsadmin` it would still render, still read as a guard, and never fire again. And the
network policy selected the store by `component: rustfs`, which after the rename would have matched
no pod, leaving the store's guarded ingress targeting nothing. Both are the estate's signature failure
— a control that cannot fire — and a rename is an unusually quiet way to introduce one.
