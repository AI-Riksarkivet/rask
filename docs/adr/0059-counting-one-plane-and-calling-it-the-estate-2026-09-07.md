# 0059. Counting one plane and calling it the estate (2026-09-07)

§F2-5 — Dapr access control — carried a measurement that made it look small: *"only TWO
service-invocation callers exist, so a `defaultAction: deny` needs 11 allow entries."* The two are
real. They are also the callers on ONE of three planes, because the count came from grepping
`/v1.0/invoke`, and that string can only see the HTTP one.

    HTTP /v1.0/invoke   gateway, notifications
    ActorProxy          annotator, notifications      never counted
    Dapr Workflow       flows, ingest, medallion      never counted; it IS actors

Dapr's own documentation settles half of it and opens the other half: *"Service invocation access
control does not cover cross-app workflow scheduling"* — there is a separate `WorkflowAccessPolicy`,
which this estate has never heard of. Actor-to-actor invocation is documented neither way, and the
estate has 28 `ActorProxy` references behind the notifications inbox and the annotator's projects.

**Deploying the eleven-entry policy on that count would have been an outage** — a deny-by-default
keyed on the HTTP callers alone, against an undocumented actor behaviour, with the cascade's workflow
plane in the blast radius. The row is not wrong that the control is missing; it is wrong about the
size, and the size was what made it look ready.

**The rule this adds to "verify where a control's value lands":** when a measurement is a COUNT of
call sites, ask what surface the count could see. A grep sees one spelling of one transport. Three
findings in one day rest on this — a field passed and read by nothing, a credential the transport
overwrites, and now a caller count taken on a third of the callers.
