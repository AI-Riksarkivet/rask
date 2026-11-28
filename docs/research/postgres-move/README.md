# Research behind the Postgres move (ADR 0230)

What the move of the catalog's governance records to the platform's Postgres was decided on (ADR 0230, spec #859,
tickets #860-#875). Each ticket's "Reference answers" section cites these files. Read on 2026-11-26 and 2026-11-27.

| File | What it holds |
|---|---|
| `lakekeeper-postgres-study.md` | Lakekeeper's Postgres layer read from its source at main `2ee5516c`: schema conventions, ids, name and location uniqueness, rename and move, transactions, migrations, connections and credentials, outages, workers, idempotency, listing, caches, tests, and what its authors call unsolved. |
| `lakekeeper.md` | How Lakekeeper keeps each piece of state, compared piece by piece with rask's. |
| `polaris-gravitino-unity.md` | The same questions answered from Polaris, Gravitino and Unity Catalog. |
| `lance-docs-study.md` | What `lance_docs/` says about a catalog server's own store, the directory catalog and `__manifest`, with measurements on pylance 14.0.3. |
| `record-facts.md` | Every record kind the catalog keeps on its control root: who writes it, who reads it, how often. |
| `gap-measurements.md` | Measurements: the name registry's cost to 30,000 tables, concurrent creates, the store requests one commit makes, a Postgres for the tests without docker, short-lived database credentials. |
| `lh613-built-vs-references.md` | The built principals tracer (#862, commit `48bd35aa0`) read point by point against Lakekeeper main `fee1729b` and Polaris main `396dd238` on 2026-11-28: one gap (a commit whose outcome is unknown answered as a retryable outage, fixed since) and the notes later rows take up. |

These are research notes: they compare rask with other catalogs and name them, which code never does (CLAUDE.md). A
design is justified from `lance_docs/`, never from what another catalog chose.
