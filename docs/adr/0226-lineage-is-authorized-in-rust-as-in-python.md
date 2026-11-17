# 0226. Lineage is authorized in Rust as in Python (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and its third zero-trust code after ADR 0220 and 0223. It
decides who may read lineage's records and who may write an event into them, and asks OpenFGA or the catalog's decision
door. No door or route calls it yet. Source: the owner, 2026-11-17 ("try to port the code to rust following these
principles, keep ADR of features").

## Context

Lineage protects its feed and its edge list itself: the route in front of it checks nothing. In Python that is
`lakehouse.governed.decider` (the questions and the two places they are answered), the parts of
`lakehouse.governed.fga` a decider reads the store with, `lineage.api.fga_deps` (every gate, what an event names, the
page filter) and `lineage.services.names` (an outside producer's names resolved to uuids, as the caller). A port that
admits one event Python refuses forges provenance; one that answers an outage as a verdict either leaks the estate or
destroys an honest event.

## Decision

1. **`lakehouse_core::authz` is the decider seam**, for every service, since the catalog and maintenance ask the same
   questions:

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `AccessDecider` | the six questions (`check`, `batch_check`, `check_tables`, `list_users`, `governed`, `locate`) and whether names are resolved | `decider.AccessDecider` |
   | `openfga::OpenFga` | the store's answers over its HTTP API, batches of `CHECKS_PER_BATCH` (50) up to `MOST_PARALLEL_BATCHES` (20) at once, the retry, the circuit, `list_objects` | `decider.OpenFgaDecider`, `fga.check`, `batch_check`, `list_objects`, `list_users`, `read_object_tuples`, `_guarded`, `_Circuit` |
   | `catalog::CatalogDecider` | the catalog's decision door, questions of at most `MAX_OBJECTS_PER_QUESTION` (1,000), an answer that is not exactly what was asked failing closed | `decider.CatalogDecider` |
   | `table_object`, `is_object_id`, `read_token` | an object's id, which ids OpenFGA accepts, a projected token read now | `fga.table_object`, `fga.is_object_id`, `machine_identity.read_identity_token` |

   Every outage is `Unavailable`, whose text a caller never reads: a 503, never an allow and never a deny. Both clients
   read their bearer from the projected token file on every request, mark it sensitive and follow no redirect. Every
   read of the store skips OpenFGA's caches and carries the check's clock, which a time-boxed grant's condition needs.

2. **`lineage::authz` is the gates**, in three modules. `scope` holds the decisions as functions of values: which objects
   an event's inputs and outputs govern by uuid, which names stay unresolved, the questions alias resolution asks
   (`alias_questions`) and what the answers make of them (`AliasQuestions::answered`), the two-relation rule for
   maintenance (`relations_for_operation`), the names a refusal quotes, and what a staged event names for a reader.
   `gates` asks: `require_event_feed_reader`, `require_parks_reader`, `require_parks_replayer` and
   `require_metadata_access` for the read doors, `ingest_authz` for the HTTP ingest door, `bus_authz` for the bus, the
   outbox and the replay of a park, and `DatasetFilter` for a reader's pages. A refusal is a `Denial` of one of eight
   classes, named as the Python names them; `Denial::ns_error` is the HTTP door's answer and `authorizing` the bus
   route's `intake::Authorizing`. `names` resolves an outside producer's names through the catalog's DescribeTable and
   DescribeNamespace under the caller's own bearer, or in one question at the decision door, and stamps the answer.
   A describe has three outcomes (`names::Unresolved`): a uuid or none (200, and 401, 403 or 404, which name nothing
   the caller may see); the name refused (400 or 422), which is a verdict on the caller's own input, since lineage fixes
   every other part of the request, answered as the Python's `_described_as_the_callers` answers it, an
   `InvalidInputError` (400, code 13) whose detail quotes the caller's name, cut at 256 characters, and nothing of the
   catalog's answer, at every door that resolves a name this way (the ingest, `GET /events?table=` and `?namespace=`,
   `/tables/{name}/derived`, the metadata gate); or an outage, for any other answer or none. The bus lane and the relay
   describe no name: they resolve names at the decider (`check_tables`), as the Python's do, so a name the catalog
   cannot resolve reaches them as one with no uuid, refused by the class an unresolved output or input has.

3. **Whose authority an event is judged under is the bus gate's first decision**: the signature verdict of ADR 0223
   comes in as an `Admission`; a refused one is final, keys that could not be read are asked about again, a drop the
   catalog attests is admitted on that signature, and any other event is judged as the subject it stamps, which must
   be a principal. A refusal of any permission class is admitted when the feed already holds the event: a redelivery.

4. **Parity is shown by vectors through the real Python** (`scripts/_port_authz_vectors.py` →
   `rust/vectors/authz.json`). The real `fga_deps` and `names` functions run with a decider that answers from the case
   and records each question in order (which method, which relation, on which objects, as which user), a feed that
   answers a run's author and whether the event is recorded, and a describe door that answers from the case; the
   signature verdict is the case's, since `signing.json` holds its own. Recorded: 46 ingest cases (every refusal class,
   external sources, another catalog's names, unresolved names, outputs and inputs named by a location, symlinks the
   caller sees and may not see, column upstreams, the maintenance rule both ways, attested events, each question's
   outage), 31 bus cases (unsigned, keys unavailable, attested and unattested drops, no author, a blank author, one that
   is no principal, replays, foreign runs, each maintenance operation, delegated signatures), 29 gate cases (a name the
   catalog refuses among them, one cut where it is quoted), 9 page filters (hidden, visible, unresolved and empty items,
   the per-request cache, ids OpenFGA refuses), 6 staged-event scopes, 8 stamps (two with a name the catalog refuses),
   10 author stamps, 7 governed-reference cases, 8 operations, and 37 catalog-decider exchanges over
   httpx's mock transport (2,105 objects in three questions, every way the door can fail). The Rust asks the same
   questions in the same order and comes to the same class and message for every case; a 5xx's message, which a client
   never reads, and a signature refusal's (ADR 0223) are left out of the comparison. The generator writes the same file
   on every run.

5. **The clients are shown against real servers.** `tests/openfga.rs` starts the pinned OpenFGA release (v1.18.3, the
   chart's, `scripts/openfga_server.sh`, digests in `scripts/toolchain.sha256`) as a subprocess on the in-memory
   datastore with a preshared key, writes the estate's `model.json`, and proves through the Rust decider: a grant
   allows and its absence denies across the parent cascade and the rungs; a time-boxed grant is judged on the check's
   clock; 157 objects with two ids OpenFGA refuses are answered whole; a listing holds what the principal reaches, and
   `list_users`, `governed`, `check_tables` and `locate` answer as the store can; the token is read on every request; a
   stopped server is unavailable for every question and then opens the circuit. `lineage/tests/authz_openfga.rs` runs
   the page filter and the ingest and bus gates over that decider. `authz_catalog.rs` and `names.rs` serve the decision
   door and the describe doors in-process. Seven planted defects each failed: an outage on the output check read as
   allowed (1 ingest and 2 bus cases), the input check skipped (3 ingest cases), maintenance asking only the
   maintainer's rung (2 operations, 4 bus cases), an unverified event judged on its claimed author (2 bus cases), a
   batch sent as one request (the batch test), no clock on a check (the time-boxed test), and an incomplete answer from
   the catalog taken (10 of 37 exchanges).

## What differs from the Python, on purpose

Each is outside the corpus:

- **A failed feed lookup is an outage.** Python lets the exception from a run's author or a recorded event escape, which
  the HTTP door answers 500 and the bus asks about again; Rust answers 503 and the bus asks again.
- **A retry waits without jitter**: 0.2 s, doubled, at most 2 s, within the same attempts and the same 15 s budget. The
  circuit belongs to the client, where Python keeps one per process; a service holds one client.
- **A subject that is no principal, asked of the store, is an outage**, where Python refuses it 400. Only a verified
  caller or a stamped author already checked to be a principal reaches a decider.
- **`governed` reads one tuple per object**, where Python reads them all; the answer is the same. A batch sends each
  object once.
- **The catalog's describe doors**: a connection that does not open is not tried again, where Python's client tried
  three times. Each name of an event is described in turn, where Python describes them at once; both answer the first
  refusal in name order.
- **An unprintable character an author claim quotes** is escaped as Python's `repr` escapes it only for the ASCII and
  Latin-1 controls, the no-break space and the soft hyphen; other unprintable code points are written as themselves.
- **An operation that is no string is no operation.** Python's maintenance check skips it and its drop check fails with
  an outage.
- **The catalog decider may run without a bearer**, as the store's client may, for an estate with authentication off.
- **The producer URIs stamped on an author and on resolved uuids are the Python modules'**, so a record does not change
  with the implementation that wrote it.

## The four facts

- **GitOps:** nothing here is configured outside what the chart renders: the decider is the catalog's door or OpenFGA's,
  by the variables the chart sets.
- **Cloud native:** the catalog and OpenFGA are named services; an outage of either is a 503, never a verdict.
- **Lance-namespace/table-idiomatic:** a name is resolved by the catalog's own DescribeTable and DescribeNamespace, and
  its refusal is answered with the spec's InvalidInput code.
- **Zero trust:** every question is asked as the caller; a refusal of the caller's name quotes the caller's own input
  and nothing of the catalog's answer, and a verdict is never answered as an outage, nor an outage as a verdict.

## Not ported yet

- The rest of `fga`: writes and revokes, provisioning a store and resolving its model by name, `expand`, the filters of
  `list_users` other than bare users, contextual tuples, and the audit of a pinned model.
- The doors and routes that call the gates (the ingest door, the read doors, the dead-letter list and replay, the bus
  lane and the outbox drain), and the audit trail's export: a refusal is logged under the `lance.audit` target.

## Consequences

- `lakehouse-core` is 3,216 of its 11,234 budgeted lines, 608 of them this feature's; `lineage` is 1,568 of 2,621, 830
  of them this feature's.
- A change to which question a gate asks, or what an answer leads to, changes a vector, and `make rust-check` fails
  until the Rust agrees.
- The OpenFGA tests need the pinned release, fetched once from github.com, and no container.
