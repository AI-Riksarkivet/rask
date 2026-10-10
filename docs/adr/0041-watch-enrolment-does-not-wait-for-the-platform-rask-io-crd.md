# 0041. Watch enrolment does not wait for the `platform.rask.io` CRD (2026-08-16)

**Decision.** The notifications page reads its project list from `me.projects`, not from the
controlplane. The `platform.rask.io` Project CRD is **ABANDONED as a rask-repo concern** — it lives in
the separate `rask-operator` repo, and landing the CRD here alone would be a regression rather than a
fix.

**What was broken, and it was worse than "no list".** The page loaded projects with
`getProjects()` → `/api/projects/` → controlplane → the Kubernetes API. (This paragraph named
`/capi/v1/projects` until 2026-08-29; that path proxies to the CATALOG and was never the one at
fault — `@rask/api`'s deleted `projects.ts` fetched `/api/projects/`, the gateway's controlplane
row.) Verified live: `kubectl get crd` returns 71 CRDs and none is `projects.platform.rask.io`, so
an in-cluster `GET /api/projects/` answered `503 {"detail":"cannot reach kubernetes api"}` — since
2026-08-29 it answers `501` naming the unregistered type instead, which is the same refusal told
truthfully. `onMount` awaited
`Promise.all([readWatches(), getProjects()])`, so that 503 rejected the pair, `watches` stayed `null`,
and the page rendered *"Watching is unavailable on this stack… the notification service is not
reachable"*. An outage in the CONTROLPLANE, reported as an outage in NOTIFICATIONS, on the one surface
a person uses to turn watching on. WATCH is one of the plane's targeting sources and its **only**
enrolment door, so the estate could not enrol anybody — for a reason it named wrongly.

**Why the CRD is not the fix.** Installing the CRD without its controller gives unreconciled CRs, which
render exactly like a project stuck mid-provision — trading an honest 503 for a page that lies more
quietly. The controller is not in this repo and cannot be. Meanwhile the identity plane already
answers the question the page is asking: `/v1/me` carries `projects`, it is the frozen contract every
zone's layout already resolves, and it is what `$lib/gallery.ts` reads for the project gallery. The
page was asking the wrong service.

**The third state comes with it.** `me === null` means BOTH "signed out" and "signed in but `/v1/me`
did not answer", and those are opposite situations: one is fixed by signing in, the other cannot be.
`hasSession` separates them, exactly as `gallery.ts` already does, so a user whose identity the catalog
could not confirm is told that — not sent round a sign-in loop that cannot help.

This does not close the OTHER half of the controlplane gap: a watch is still keyed by a project id
whose namespace nothing joins to the FGA tenant id. That remains open and is not addressed here.
