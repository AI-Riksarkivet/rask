//! Lineage's store: the commit feed, the edge list, the run bindings, the refusals and the cursors, kept as Lance tables
//! in the catalog's provenance namespace and reached only through the catalog's doors, as this service's own identity.
//!
//! What is decided lives in the submodules as functions of values ([`commit::plan`], [`window::Cursor`],
//! [`walk::walk`], [`prune_predicate`]); [`Store`] is the shell that reads and writes.

pub mod commit;
pub mod group;
pub mod rows;
pub mod tables;
pub mod walk;
pub mod window;

use crate::feed::feed_key;
use crate::intake::Recording;
use crate::model::{Event, parse_event};
use commit::Admitted;
use group::{Committer, Group};
use lakehouse_core::service::problem::ErrorCode;
use lakehouse_tables::filters::{in_lists, sql_literal};
use lakehouse_tables::{Catalog, CatalogError, Rows, Scan, Table};
use rows::EventRecord;
use serde_json::{Map, Value};
use std::collections::{BTreeSet, HashMap};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use walk::{DerivedTable, Edge};
use window::{FeedReader, FeedWindow};

/// The most bytes of rows one group commit takes, a sixteenth of the catalog's default body cap (256 MiB), and always
/// at least one event. A catalog configured below it refuses a larger write, which then lands in parts.
pub const BATCH_BYTES: usize = 16 * 1024 * 1024;

const SUMMARY_COLUMNS: [&str; 9] = [
    "event_type",
    "event_time",
    "job",
    "author",
    "inputs",
    "outputs",
    "table_uuids",
    "namespace_uuids",
    "unresolved_tables",
];
/// What a bounds probe projects: one fixed-width column, so the catalog sizes its answer without sampling a row.
const PROBE_COLUMNS: [&str; 1] = ["received_at"];

/// Now, in microseconds since the epoch: the estate's clock, which every row the store writes is stamped with.
pub fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_micros()).unwrap_or(i64::MAX))
}

/// `micros` since the epoch as ISO 8601 in UTC with microseconds, as Python's `isoformat(timespec="microseconds")` writes it.
pub fn iso_micros(micros: i64) -> String {
    let (days, of_day) = (micros.div_euclid(86_400_000_000), micros.rem_euclid(86_400_000_000));
    // The civil date of a day count (Howard Hinnant's `civil_from_days`).
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted.rem_euclid(146_097);
    let year_of_era = (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 { month_index + 3 } else { month_index - 9 };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    let (seconds, fraction) = (of_day / 1_000_000, of_day % 1_000_000);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{fraction:06}+00:00",
        seconds / 3600,
        seconds / 60 % 60,
        seconds % 60
    )
}

/// The delete a prune makes: rows received more than `retention_days` before `now`, and, where a forwarder follows the
/// feed, only rows it already delivered, since the feed is the only copy it sends from. A row's seq is its row id plus
/// one, so `_rowid < delivered_through` keeps every row after the last one delivered.
pub fn prune_predicate(now: i64, retention_days: u32, delivered_through: Option<i64>) -> String {
    let cutoff = iso_micros(now - i64::from(retention_days) * 86_400_000_000);
    let old = format!("received_at < arrow_cast({}, 'Timestamp(Microsecond, Some(\"UTC\"))')", sql_literal(&cutoff));
    match delivered_through {
        Some(seq) => format!("{old} AND _rowid < {seq}"),
        None => old,
    }
}

/// The filter that reads the deliveries the ingest lane parked, of one run or of every run.
pub fn parked_filter(run_id: Option<&str>) -> String {
    let parked = format!("starts_with(outbox_key, {})", sql_literal(tables::PARKED_KEY_PREFIX));
    match run_id {
        Some(run_id) => format!("{parked} AND run_id = {}", sql_literal(run_id)),
        None => parked,
    }
}

/// The feed row a delivered CloudEvent would write, or `None` when no event parses out of it.
pub fn delivery_feed_key(cloud_event: &Value) -> Option<String> {
    parse_event(cloud_event.get("data").unwrap_or(&Value::Null)).ok().map(|event| feed_key(&event))
}

/// A delivery the ingest lane's park kept: its key in `refusals`, the CloudEvent as it arrived, and the feed row it
/// would write.
#[derive(Debug, Clone, PartialEq)]
pub struct ParkedDelivery {
    pub key: String,
    pub run_id: Option<String>,
    pub cloud_event: Value,
    pub feed_key: Option<String>,
}

/// The tables, and how a batch of admitted events lands in them.
struct Tables {
    catalog: Catalog,
    namespace: Vec<String>,
    refuse_foreign_runs: bool,
}

impl Tables {
    fn table(&self, name: &str) -> Table {
        let mut id = self.namespace.clone();
        id.push(name.to_owned());
        self.catalog.table(id)
    }

    async fn read(&self, table: &str, columns: &[&str], filter: Option<String>) -> Result<Rows, CatalogError> {
        let scan = Scan {
            columns: Some(columns.iter().map(|column| (*column).to_owned()).collect()),
            filter,
            ..Scan::default()
        };
        self.table(table).read(&scan).await
    }

    /// The author the table binds each of `runs` to, for those of them it binds.
    async fn authors_of(&self, runs: &BTreeSet<String>) -> Result<HashMap<String, Option<String>>, CatalogError> {
        let mut authors = HashMap::new();
        for filter in in_lists("run_id", runs) {
            let bound = self.read(tables::RUNS, &["run_id", "author"], Some(filter)).await?;
            let (runs, by) = (rows::text_column(&bound, "run_id")?, rows::text_column(&bound, "author")?);
            authors.extend(runs.into_iter().zip(by).filter_map(|(run, author)| Some((run?, author))));
        }
        Ok(authors)
    }
}

fn unwritable(error: &dyn std::fmt::Display) -> CatalogError {
    CatalogError::local(ErrorCode::Internal, format!("the rows could not be built: {error}"))
}

impl Committer for Tables {
    type Item = Admitted;

    fn weight(item: &Admitted) -> usize {
        item.weight
    }

    /// Land a batch in one commit per table, in the order of `specs/lineage/EventPath.tla`: the run bindings, the
    /// edges, the feed rows. Each is an insert-only merge on the table's key, so a write that lands late, or twice, lands
    /// nothing new, and the feed row is written last, so a row in the feed says the rest is there.
    async fn commit(&self, batch: &[Admitted]) -> Result<Vec<usize>, CatalogError> {
        let bound = self.authors_of(&commit::runs_of(batch)).await?;
        let plan = commit::plan(batch, &bound, self.refuse_foreign_runs).map_err(|error| unwritable(&error))?;
        let writes = [
            (tables::RUNS, "run_id", plan.runs),
            (tables::EDGES, "edge_key", plan.edges),
            (tables::FEED, "event_key", plan.feed),
        ];
        for (table, key, rows) in writes {
            if let Some(rows) = rows {
                self.table(table).merge_insert_only(&Rows::from(rows), key).await?;
            }
        }
        Ok(plan.foreign)
    }
}

/// The feed at one version, read through the catalog: what a [`FeedWindow`] reads.
pub struct TableFeed {
    table: Table,
    version: u64,
    summary: bool,
}

impl FeedReader for TableFeed {
    async fn count(&self, outside: Option<(i64, i64)>) -> Result<u64, CatalogError> {
        let filter = outside.map(|(lo, hi)| format!("_rowid < {lo} OR _rowid > {hi}"));
        self.table.count_rows(filter.as_deref(), Some(self.version)).await
    }

    async fn ids(&self, limit: Option<usize>, offset: Option<usize>) -> Result<Vec<i64>, CatalogError> {
        let scan = Scan {
            columns: Some(PROBE_COLUMNS.map(str::to_owned).to_vec()),
            limit,
            offset,
            with_row_id: true,
            version: Some(self.version),
            ..Scan::default()
        };
        rows::row_ids(&self.table.read(&scan).await?)
    }

    async fn range(&self, lo: i64, hi: i64, limit: usize) -> Result<Vec<EventRecord>, CatalogError> {
        let mut columns: Vec<String> = SUMMARY_COLUMNS.map(str::to_owned).to_vec();
        if !self.summary {
            columns.push("event".to_owned());
        }
        let scan = Scan {
            columns: Some(columns),
            filter: Some(format!("_rowid >= {lo} AND _rowid <= {hi}")),
            limit: Some(limit),
            with_row_id: true,
            version: Some(self.version),
            ..Scan::default()
        };
        rows::records(&self.table.read(&scan).await?, self.summary)
    }
}

/// The store. Cloning shares one group commit: one writer at a time per process.
#[derive(Clone)]
pub struct Store {
    group: Arc<Group<Tables>>,
    /// The dataset namespace the catalog names its tables under: a dataset under another is an outside source.
    dataset_namespace: Arc<str>,
}

impl Store {
    /// The store whose tables live in `namespace` of `catalog`. `refuse_foreign_runs` is the doors' rule, on when they
    /// authorize: a commit refuses an event of a run another author founded.
    pub fn new(catalog: Catalog, namespace: Vec<String>, dataset_namespace: &str, refuse_foreign_runs: bool) -> Result<Self, CatalogError> {
        if namespace.is_empty() {
            return Err(CatalogError::local(
                ErrorCode::InvalidInput,
                "the provenance store needs a namespace to keep its tables in",
            ));
        }
        let tables = Tables {
            catalog,
            namespace,
            refuse_foreign_runs,
        };
        Ok(Store {
            group: Group::new(tables, BATCH_BYTES),
            dataset_namespace: dataset_namespace.into(),
        })
    }

    fn tables(&self) -> &Tables {
        self.group.committer()
    }

    /// A table this store keeps, by name.
    pub fn table(&self, name: &str) -> Table {
        self.tables().table(name)
    }

    /// Create each table this store keeps, keeping one that already exists.
    pub async fn ensure(&self) -> Result<(), CatalogError> {
        for (name, schema) in tables::all() {
            self.table(name).create(schema).await?;
        }
        Ok(())
    }

    /// Fails unless the feed table answers: the one thing every door of this service needs.
    pub async fn ready(&self) -> Result<(), CatalogError> {
        self.table(tables::FEED).version().await.map(drop)
    }

    /// Record one admitted event: its run's binding, its edges, then its feed row, each landing once whatever is
    /// delivered again. `aliased` are the uuids of the tables its datasets alias, which its feed row is scoped by.
    ///
    /// The event joins the group commit the writer makes next and is answered once that commit landed it. A failure
    /// between two tables leaves the earlier ones; the caller retries, and the retry lands what is missing.
    pub async fn record(&self, event: &Event, aliased: &[String]) -> Recording {
        self.group.record(commit::admitted(event, aliased, &self.dataset_namespace, now_micros())).await
    }

    /// Keep a settled governance refusal of a staged event, as an upsert on its key: a crash between this write and the
    /// staged object's delete refuses the object again into the same row.
    pub async fn record_refusal(
        &self,
        outbox_key: &str,
        run_id: Option<&str>,
        author: Option<&str>,
        reason: &str,
        event_json: &str,
    ) -> Result<(), CatalogError> {
        let row = rows::refusal_batch(outbox_key, run_id, author, reason, event_json, now_micros()).map_err(|error| unwritable(&error))?;
        self.table(tables::REFUSALS).merge_upsert(&Rows::from(row), "outbox_key", None).await
    }

    /// Each delivery the ingest lane's park kept, of `run_id` or of every run.
    pub async fn parked(&self, run_id: Option<&str>) -> Result<Vec<ParkedDelivery>, CatalogError> {
        let rows = self
            .tables()
            .read(tables::REFUSALS, &["outbox_key", "run_id", "event"], Some(parked_filter(run_id)))
            .await?;
        let (keys, runs, events) = (
            rows::text_column(&rows, "outbox_key")?,
            rows::text_column(&rows, "run_id")?,
            rows::text_column(&rows, "event")?,
        );
        let mut parked = Vec::new();
        for ((key, run_id), event) in keys.into_iter().zip(runs).zip(events) {
            let cloud_event: Value = serde_json::from_str(event.as_deref().unwrap_or_default())
                .map_err(|error| CatalogError::local(ErrorCode::Internal, format!("a parked delivery is no JSON: {error}")))?;
            parked.push(ParkedDelivery {
                key: key.unwrap_or_default(),
                run_id,
                feed_key: delivery_feed_key(&cloud_event),
                cloud_event,
            });
        }
        Ok(parked)
    }

    /// Which of the feed row keys `keys` the feed holds.
    pub async fn events_recorded<S: AsRef<str>>(&self, keys: impl IntoIterator<Item = S>) -> Result<BTreeSet<String>, CatalogError> {
        let mut held = BTreeSet::new();
        for filter in in_lists("event_key", keys) {
            let rows = self.tables().read(tables::FEED, &["event_key"], Some(filter)).await?;
            held.extend(rows::text_column(&rows, "event_key")?.into_iter().flatten());
        }
        Ok(held)
    }

    /// Delete the kept deliveries `keys` name, once the feed holds what each would write.
    pub async fn forget_parked<S: AsRef<str>>(&self, keys: impl IntoIterator<Item = S>) -> Result<(), CatalogError> {
        for filter in in_lists("outbox_key", keys) {
            self.table(tables::REFUSALS).delete(&filter).await?;
        }
        Ok(())
    }

    /// Delete every kept delivery whose feed row the feed holds, however it got there; answer how many.
    pub async fn retire_recovered_parks(&self) -> Result<usize, CatalogError> {
        let kept = self.parked(None).await?;
        let held = self.events_recorded(kept.iter().filter_map(|delivery| delivery.feed_key.as_deref())).await?;
        let recovered: Vec<&str> = kept
            .iter()
            .filter(|delivery| delivery.feed_key.as_ref().is_some_and(|key| held.contains(key)))
            .map(|delivery| delivery.key.as_str())
            .collect();
        if !recovered.is_empty() {
            self.forget_parked(&recovered).await?;
        }
        Ok(recovered.len())
    }

    /// Delete the feed rows received more than `retention_days` ago and, with a forwarder, already delivered through
    /// `delivered_through`; answer how many it found to delete.
    pub async fn prune_events(&self, retention_days: u32, delivered_through: Option<i64>) -> Result<u64, CatalogError> {
        let predicate = prune_predicate(now_micros(), retention_days, delivered_through);
        let feed = self.table(tables::FEED);
        let doomed = feed.count_rows(Some(&predicate), None).await?;
        if doomed > 0 {
            feed.delete(&predicate).await?;
        }
        Ok(doomed)
    }

    /// One read of the feed at its current version: newest first, older than `after`, or oldest first after `since`.
    pub async fn window(&self, after: Option<i64>, since: Option<i64>, summary: bool) -> Result<FeedWindow<TableFeed>, CatalogError> {
        if after.is_some() && since.is_some() {
            return Err(CatalogError::local(ErrorCode::InvalidInput, "a feed read takes `after` or `since`, not both"));
        }
        let table = self.table(tables::FEED);
        let version = table.version().await?;
        FeedWindow::open(TableFeed { table, version, summary }, since, after).await
    }

    /// Feed rows: newest first, older than `after`, or oldest first after `since` (the follower's read).
    pub async fn list_events(&self, limit: usize, after: Option<i64>, since: Option<i64>, summary: bool) -> Result<Vec<EventRecord>, CatalogError> {
        self.window(after, since, summary).await?.take(limit).await
    }

    /// The oldest and the newest seq the feed holds, or `None` when it holds nothing: zero would read as "everything
    /// since the beginning is still here", the opposite of what an empty feed means after retention.
    pub async fn feed_bounds(&self) -> Result<Option<(i64, i64)>, CatalogError> {
        self.window(None, None, true).await?.bounds().await
    }

    /// The payload the feed holds under the row key `key`; `None` when it holds none.
    pub async fn recorded_event(&self, key: &str) -> Result<Option<Map<String, Value>>, CatalogError> {
        let rows = self
            .tables()
            .read(tables::FEED, &["event"], Some(format!("event_key = {}", sql_literal(key))))
            .await?;
        let first = rows::text_column(&rows, "event")?.into_iter().next().flatten();
        Ok(match first.and_then(|text| serde_json::from_str::<Value>(&text).ok()) {
            Some(Value::Object(event)) => Some(event),
            _ => None,
        })
    }

    /// Every table derived from the table keyed by `sources`, nearest first, and whether the walk reached its end; read
    /// off the edge list at one version.
    pub async fn derived(&self, sources: &BTreeSet<String>, max_depth: u32) -> Result<(Vec<DerivedTable>, bool), CatalogError> {
        let edges = self.table(tables::EDGES);
        let version = edges.version().await?;
        let read = async |inputs: &BTreeSet<String>| {
            let mut found = Vec::new();
            for filter in in_lists("in_table", inputs) {
                let scan = Scan {
                    columns: Some(walk::COLUMNS.map(str::to_owned).to_vec()),
                    filter: Some(filter),
                    version: Some(version),
                    ..Scan::default()
                };
                let rows = edges.read(&scan).await?;
                let (tables, names, at) = (
                    rows::text_column(&rows, "out_table")?,
                    rows::text_column(&rows, "out_name")?,
                    rows::int_column(&rows, "received_at")?,
                );
                for ((out_table, out_name), received_at) in tables.into_iter().zip(names).zip(at) {
                    found.push(Edge {
                        out_table: out_table.unwrap_or_default(),
                        out_name,
                        received_at: received_at.unwrap_or_default(),
                    });
                }
            }
            Ok::<_, CatalogError>(found)
        };
        let walked = walk::walk(read, sources, Some(max_depth)).await?;
        Ok((walk::derived_tables(&walked), walked.complete))
    }

    /// The seq of the last feed row `follower` handled; 0 before its first.
    pub async fn cursor(&self, follower: &str) -> Result<i64, CatalogError> {
        let rows = self
            .tables()
            .read(tables::CURSORS, &["seq"], Some(format!("follower = {}", sql_literal(follower))))
            .await?;
        Ok(rows::int_column(&rows, "seq")?.into_iter().flatten().max().unwrap_or(0))
    }

    /// Move `follower`'s cursor to `seq`, never backward: a replica's slower tick cannot undo a faster one's progress.
    pub async fn advance_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        self.write_cursor(follower, seq, Some("source.seq > target.seq")).await
    }

    /// Set `follower`'s cursor to `seq` whichever way it moves: for a feed whose bytes were put back from before it.
    pub async fn reset_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        self.write_cursor(follower, seq, None).await
    }

    async fn write_cursor(&self, follower: &str, seq: i64, matched_when: Option<&str>) -> Result<(), CatalogError> {
        let row = rows::cursor_batch(follower, seq, now_micros()).map_err(|error| unwritable(&error))?;
        self.table(tables::CURSORS).merge_upsert(&Rows::from(row), "follower", matched_when).await
    }

    /// Which feed table this is: the uuid the catalog minted when it created it, which a feed created again does not
    /// share, so a cursor is kept under the feed it was taken on.
    pub async fn feed_incarnation(&self) -> Result<String, CatalogError> {
        let described = self.table(tables::FEED).describe().await?;
        let missing = || {
            CatalogError::local(
                ErrorCode::ServiceUnavailable,
                "the catalog described the feed table without the uuid it mints for every table",
            )
        };
        described.uuid().map(str::to_owned).ok_or_else(missing)
    }

    /// The verified author the run is bound to, or `None` when no event of it was recorded or its first named none.
    pub async fn run_author(&self, run_id: &str) -> Result<Option<String>, CatalogError> {
        let rows = self
            .tables()
            .read(tables::RUNS, &["author"], Some(format!("run_id = {}", sql_literal(run_id))))
            .await?;
        Ok(rows::text_column(&rows, "author")?.into_iter().flatten().find(|author| !author.is_empty()))
    }
}
