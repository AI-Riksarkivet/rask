# 0171. The lakehouse knows no annotation tool: rask's annotator is a client with its own release, state and roles (2026-10-18)

**Status:** accepted. Source: the owner's ruling of 2026-10-18 (XC-165 session): "annotator is not dependent and we need
clear separation from lakehouse. It works on lance-ns and with rustfs but is not a part of the catalog", and "we should
be able to use label studio as well with our lakehouse"; then four `/grill-with-docs` rounds on XC-167 (#661) the same
day, every decision below answered by the owner through the multi-question tool. Folds in CTL-31 (#552) and CTL-32
(#651). Follows ADR 0155's precedent for a client leaving the product chart; relies on ADR 0169 (each app reads only
its own secrets) and ADR 0170 (the catalog vends sessions of its own token).

## Context

The lakehouse is agnostic about tools as it is about modalities, and the test for a shared seam is the same: would it
serve Label Studio unchanged? Measured on 2026-10-18 (a read-only sweep of the tree at `d96445ce`), it knew rask's
annotator in eight places:

- **The control lane.** `service_kit.control_events` names an `annotation_task` object type and five `task_*` actions,
  and `control_signer_role` exempts those actions (R3) and `annotation_project:` grants (R5) from any signature. The
  annotator publishes them unsigned on `catalog.control.v1`, with no outbox (CTL-31). The catalog's own subscriber on
  that lane verifies no signature, so a forged event reaches its change feed and its binding cache (CTL-32).
- **Notifications.** Five task reasons and the targeting that reads `extra.subject` from those events.
- **The authorization model.** `type annotation_project` and `project.can_create_annotation_project`. The annotator
  writes its tuples into the lakehouse's own OpenFGA store as an `fgaWriter`, a credential that can write any tuple.
- **Maintenance.** Its FGA reconcile and repair passes handle `annotation_project` objects.
- **Service-kit.** `governed.fga` names the annotator as an origin, and `media.config` carries its publish identity
  and its annotation table naming.
- **State.** Its projects, tasks, drafts, assignees and leases are Dapr actors on the lakehouse's own state store,
  `lance-statestore`, on the `rask-age` Postgres.
- **Bytes.** It reads its corpus, on the dev estate the governed bucket `bind86-wh` registered as `media$chunks` and
  `media$documents`, straight from the store with the static pair `rask-media`, outside the catalog's per-table
  authorization. Its publish saga mints a Dex token for a dedicated user by password grant (`publisher-oidc`).
- **The product chart.** It renders the annotator from the catalog's image (`explorer.yaml`), routes
  `/api/explorer/annotations` to it, and gives it store reads, Dapr scopes, an app-token row and a control lane.

The catalog itself serves no annotation endpoint; its members door is tenant membership, which the annotation rungs
inherited through the model.

## Decision

1. **No lakehouse module knows an annotation tool.** The control vocabulary drops `annotation_task`, the five `task_*`
   actions and both exemptions; notifications drops the five task reasons; the model drops `annotation_project` and
   `project.can_create_annotation_project`; maintenance's reconcile and repair stop handling them; service-kit's
   annotation-specific code moves into the annotator. An import-linter contract forbids `catalog`, `lineage`,
   `maintenance`, `notifications`, `service_kit` and `lineage_kit` importing `annotator`. A client may import the shared
   library; the lakehouse may not import a client.
2. **The control lane carries only the lakehouse's own signed events.** The annotator publishes nothing on the
   lakehouse's bus (no control publisher, NATS user or pubsub component), and the catalog's control subscriber verifies
   each event's signature as the lakehouse's other doors do (CTL-32).
3. **A tool tells a person about work on its own channel.** Notifications reports the lakehouse's own state changes
   (commits, grants, holds, erasures). A tool's work queue is the tool's, as Label Studio's is.
4. **The annotator is a client with its own release.** Its own image, and its own chart, `deploy/fixtures/annotator`,
   installed after rask for development and CI as compute's is (ADR 0155), reaching rask only by reference: the
   catalog's address, the identity provider, the object store's endpoint. The product chart renders no annotator
   service, store reads, Dapr scopes, app-token row, NATS row or route; `/api/explorer/annotations` moves into the
   annotator's chart with its Service. The `/annotator` zone stays in the product chart, its `ANNOTATOR_API` pointed at
   that Service, as the compute zone stayed when compute left.
5. **Its state is its own.** Its actors keep their code and move to its own Dapr state store, scoped to the annotator
   alone, on a CloudNativePG Postgres its chart renders. The connection credentials are the cluster's generated
   Secret, which only the annotator's ServiceAccount may read.
6. **Its project roles are its own.** Owner, manager, reviewer, annotator and viewer live in each project's own
   state, and the annotator decides them. It holds no OpenFGA credential. Access to data stays the lakehouse's
   decision, made on the person's own token at the catalog's doors, and the annotator learns a person's tenant
   membership from the catalog with that token.
7. **It reaches data through the catalog's public doors only.**
   - A person's reads and writes, the corpus included, go through the catalog as that person: a media table is opened
     with the credential the catalog vends for the person's token (`DescribeTable` with `vend_credentials`, read tier).
   - The publish saga presents the annotator's own projected ServiceAccount token, which the catalog verifies like any
     service's. A project admin grants that principal writer on the target namespace and read on its inputs through
     the catalog's grant door, and the originator names the publishing person (LH-433), so notifications tells them
     their run committed. The password grant and `publisher-oidc` are gone.
   - It reads no secret from the lakehouse's secret store, generates its own Dapr app token, and holds no store policy
     and no decider grant.
8. **Label Studio reaches the same doors.** Its own principal is onboarded with grants as any principal is, and a
   connector job, the tool's integration rather than the lakehouse's, reads its exports and commits them as Lance
   tables through the catalog's doors on vended credentials. This row deploys no Label Studio.

## Considered options

- **A public notify door** any client may call to put work in a person's inbox. Rejected: a new product surface whose
  sender check and anti-spam would have to be designed first, and zero trust is better served by no client writing
  into another person's inbox.
- **Annotation tasks as OpenLineage runs.** Rejected: an assignment is not a run.
- **Keeping CTL-32 parked.** Rejected: an unverifying subscriber keeps "the lane carries only signed events" false.
- **Taking the whole explorer out** (viewer and search with the annotator). Rejected for this row: they are "ignored
  for now", and their static pairs are parked as XC-169 (#663).
- **A second production product chart for the annotator.** Rejected: production renders no annotator today, and a
  client chart follows the one precedent the repo has.
- **Its own OpenFGA store.** Rejected: OpenFGA authenticates per server, not per store, so the credential could write
  the lakehouse's store as well, the product chart's subject allow-list would name the tool, and tenant inheritance
  would still need the catalog.
- **A generic tool type in the lakehouse's model.** Rejected: the lakehouse would host its clients' authorization.
- **Its own store identity for corpus reads.** Rejected: a tool-wide read of governed bytes outside per-table
  authorization, and rask's scoped-users hook prunes any ServiceAccount-named policy it does not declare.
- **Its state as Lance tables through the catalog.** Rejected: frequent small writes of app state make many table
  versions, leases lose their reminders, and the actors would be rewritten.
- **Publishing on the person's token.** Rejected: the crash-safe saga retries after the person's token has expired.
- **Moving the zone into the annotator's release.** Rejected: the frontend is the owner's to rebuild.

## Consequences

- The estate's bell no longer counts annotation work; the annotator shows a person's tasks itself.
- Publishing into a tenant's silver namespace needs that tenant's project admin to grant the annotator's principal,
  as onboarding any client does.
- The annotator's development install needs the CloudNativePG operator fixture, which `make k3s-fixtures` installs
  before it.
- The dev estate keeps inert test data: `annotation_project` tuples in the lakehouse's OpenFGA store, which no check
  reaches once the model drops the type, and the annotator's actor rows in `lance-statestore`. An install from git holds
  neither, and no door removes another app's actor rows.
- Search and the viewer keep their static store pairs until XC-169.
- The row closes when the contract and the vocabularies hold, the product chart renders no annotator, and a live e2e
  drives the annotator from its own release: it creates a project over a governed media table, assigns and annotates
  as the person, reviews, publishes as the annotator's ServiceAccount, and reads each table back through the catalog.
