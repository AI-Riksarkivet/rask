//! The forwarder: every recorded event, as standard OpenLineage, to the graph backend git names
//! (`lineage.services.forward`). At least once, from the feed: each tick reads the feed forward from the forwarder's
//! cursor, POSTs each event in seq order, and moves the cursor past what the backend answered. The cursor is a row of the
//! catalog-kept `cursors` table under the endpoint and the feed table's uuid, so a backend's progress survives a restart
//! and a feed created again is followed from its first row; it moves only forward, since two replicas' ticks may overlap.
//!
//! A transport failure, or any answer but a success or a refusal of the event, stops the tick at the last event the
//! backend answered: the backend is down, or refuses the forwarder itself. A 400, 413 or 422 refuses that one event, and
//! holding it would hold back every event after it, so it is counted and passed.

use crate::settings::Settings;
use crate::store::Store;
use crate::store::rows::EventRecord;
use crate::store::window::{FeedReader, FeedWindow};
use lakehouse_tables::CatalogError;
use serde_json::{Map, Value};

/// The answers that refuse one event rather than the forwarder.
const REFUSED: [u16; 3] = [400, 413, 422];
/// Events read from the feed at a time: an event may weigh 1 MiB, so a page bounds what one tick holds.
const PAGE: usize = 32;

/// What one tick's forward did; `stalled` says why it stopped short of the feed's head.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Forwarded {
    pub forwarded: u64,
    pub refused: u64,
    pub stalled: Option<String>,
}

/// The feed a forwarder follows, and its cursors there.
pub trait Followed {
    type Reader: FeedReader;
    /// The feed table's uuid, which a feed created again does not share.
    fn incarnation(&self) -> impl Future<Output = Result<String, CatalogError>>;
    fn cursor(&self, follower: &str) -> impl Future<Output = Result<i64, CatalogError>>;
    /// The feed at its current version, oldest first after `since`.
    fn window(&self, since: i64) -> impl Future<Output = Result<FeedWindow<Self::Reader>, CatalogError>>;
    fn reset_cursor(&self, follower: &str, seq: i64) -> impl Future<Output = Result<(), CatalogError>>;
    fn advance_cursor(&self, follower: &str, seq: i64) -> impl Future<Output = Result<(), CatalogError>>;
}

impl Followed for Store {
    type Reader = crate::store::TableFeed;

    async fn incarnation(&self) -> Result<String, CatalogError> {
        self.feed_incarnation().await
    }

    async fn cursor(&self, follower: &str) -> Result<i64, CatalogError> {
        Store::cursor(self, follower).await
    }

    async fn window(&self, since: i64) -> Result<FeedWindow<Self::Reader>, CatalogError> {
        Store::window(self, None, Some(since), false).await
    }

    async fn reset_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        Store::reset_cursor(self, follower, seq).await
    }

    async fn advance_cursor(&self, follower: &str, seq: i64) -> Result<(), CatalogError> {
        Store::advance_cursor(self, follower, seq).await
    }
}

/// POST up to `batch` events of `window`, the feed after `mark`, a page at a time; the seq of the last event the backend
/// answered, which the cursor may move to, and what was sent. `authorization` is read once, before the first POST.
async fn send<R: FeedReader>(
    window: &mut FeedWindow<R>,
    batch: usize,
    mark: i64,
    authorization: impl AsyncFnOnce() -> Result<Option<String>, String>,
    mut post: impl AsyncFnMut(&Map<String, Value>, Option<&str>) -> Result<u16, String>,
) -> Result<(i64, Forwarded), String> {
    let (mut reached, mut sent) = (mark, Forwarded::default());
    let mut authorization = Some(authorization);
    let mut header: Option<String> = None;
    let answered = |sent: &Forwarded| usize::try_from(sent.forwarded + sent.refused).unwrap_or(usize::MAX);
    while answered(&sent) < batch {
        let records: Vec<EventRecord> = window.take(PAGE.min(batch - answered(&sent))).await.map_err(|error| error.to_string())?;
        if records.is_empty() {
            break;
        }
        if let Some(read) = authorization.take() {
            header = read().await?;
        }
        for record in records {
            match post(&record.event, header.as_deref()).await {
                Err(error) => return Ok((reached, Forwarded { stalled: Some(error), ..sent })),
                Ok(200..=299) => sent.forwarded += 1,
                Ok(status) if REFUSED.contains(&status) => {
                    sent.refused += 1;
                    tracing::warn!(seq = record.seq, status, "lineage_forward_event_refused");
                }
                Ok(status) => {
                    return Ok((
                        reached,
                        Forwarded {
                            stalled: Some(format!("HTTP {status}")),
                            ..sent
                        },
                    ));
                }
            }
            reached = record.seq;
        }
    }
    Ok((reached, sent))
}

/// POST the feed's events after the forwarder's cursor to the backend in seq order, and move the cursor past them.
pub async fn forward<F: Followed>(
    feed: &F,
    settings: &Settings,
    authorization: impl AsyncFnOnce() -> Result<Option<String>, String>,
    post: impl AsyncFnMut(&Map<String, Value>, Option<&str>) -> Result<u16, String>,
) -> Result<Forwarded, String> {
    let failed = |error: CatalogError| error.to_string();
    let name = settings.follower(&feed.incarnation().await.map_err(failed)?);
    let mut mark = feed.cursor(&name).await.map_err(failed)?;
    // One read of the feed serves the whole tick: its bounds judge the cursor and its rows are the pages sent.
    let mut window = feed.window(mark).await.map_err(failed)?;
    match window.bounds().await.map_err(failed)? {
        Some((oldest, newest)) if newest < mark => {
            // The table's bytes were put back from a copy older than the cursor: rows committed since reuse its seqs.
            tracing::warn!(cursor = mark, head = newest, "lineage_forward_cursor_ahead_of_feed");
            mark = oldest - 1;
            feed.reset_cursor(&name, mark).await.map_err(failed)?;
            window = feed.window(mark).await.map_err(failed)?;
        }
        Some((oldest, _)) if mark != 0 && oldest > mark + 1 => {
            tracing::error!(cursor = mark, oldest_seq = oldest, "lineage_forward_pruned_before_forwarded");
        }
        _ => {}
    }
    let (reached, sent) = send(&mut window, settings.forward_batch, mark, authorization, post).await?;
    if reached > mark {
        feed.advance_cursor(&name, reached).await.map_err(failed)?;
    }
    if let Some(reason) = &sent.stalled {
        tracing::warn!(cursor = reached, reason, "lineage_forward_stalled");
    }
    Ok(sent)
}
