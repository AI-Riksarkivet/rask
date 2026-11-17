//! One read of the commit feed, pinned to one table version, handing out its rows in cursor order.
//!
//! The feed's sequence is Lance's own: its tables keep stable row ids, assigned from the manifest's `next_row_id` when a
//! commit lands (`lance_docs/lance/format/table/row_id_lineage.md`, "Row ID Assignment"), so row-id order is commit order
//! and a seq is a row id plus one. The query door takes no order (`QueryTableRequest`, `lance_docs/ns_catalog/spec.yaml`),
//! so no read trusts one: every read names a range of row ids, and a range of `w` ids holds at most `w` rows whatever
//! order they are answered in. A page therefore costs the rows it returns, not the feed's length.
//!
//! The bounds are read the same way: a count and the first row a query answers NAME them, and a second count PROVES
//! them, since no row lies outside `[lo, lo + total - 1]` exactly when the feed's ids are those. No row is handed out
//! before the proof holds; one that fails (a feed with holes) names the newest row by its offset and proves that, and a
//! store answering out of row-id order costs a read of every row's id.

use crate::store::rows::EventRecord;
use lakehouse_tables::CatalogError;
use std::collections::VecDeque;
use std::future::Future;

/// The reads a window makes, each at the one version the window is pinned to.
pub trait FeedReader {
    /// The rows the feed holds, or those outside `[lo, hi]` when given: a count on row ids alone reads no data file.
    fn count(&self, outside: Option<(i64, i64)>) -> impl Future<Output = Result<u64, CatalogError>>;
    /// Row ids in the order the query answers them, from `offset`, at most `limit`.
    fn ids(&self, limit: Option<usize>, offset: Option<usize>) -> impl Future<Output = Result<Vec<i64>, CatalogError>>;
    /// The rows whose ids lie in `[lo, hi]`, at most `limit`.
    fn range(&self, lo: i64, hi: i64, limit: usize) -> impl Future<Output = Result<Vec<EventRecord>, CatalogError>>;
}

/// Where a window reads next, in row ids: the arithmetic of the window, with no read in it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cursor {
    since: Option<i64>,
    after: Option<i64>,
    /// The oldest and newest seq the feed holds, once named.
    pub bounds: Option<(i64, i64)>,
    /// The lowest row id the feed holds, where a proof by offset starts.
    pub low_bound: i64,
    pub low: i64,
    pub high: i64,
}

impl Cursor {
    pub fn new(since: Option<i64>, after: Option<i64>) -> Self {
        Cursor {
            since,
            after,
            bounds: None,
            low_bound: 1,
            low: 1,
            high: 0,
        }
    }

    pub fn forward(&self) -> bool {
        self.since.is_some()
    }

    /// Read from the start of the cursor within `row_ids`, the lowest and highest row id the feed holds.
    ///
    /// `since` is a seq, so the first row id after it is `since` itself, and the row below `after` is row id `after - 2`.
    pub fn start(&mut self, row_ids: Option<(i64, i64)>) {
        self.bounds = row_ids.map(|(lo, hi)| (lo + 1, hi + 1));
        self.low_bound = row_ids.map_or(1, |(lo, _)| lo);
        (self.low, self.high) = row_ids.unwrap_or((1, 0));
        if let Some(since) = self.since {
            self.low = self.low.max(since);
        } else if let Some(after) = self.after {
            self.high = self.high.min(after - 2);
        }
    }

    /// The next range of `width` ids in cursor order, or `None` once none is left.
    pub fn next_range(&mut self, width: i64) -> Option<(i64, i64)> {
        if self.low > self.high {
            return None;
        }
        if self.forward() {
            let range = (self.low, self.high.min(self.low + width - 1));
            self.low = range.1 + 1;
            Some(range)
        } else {
            let range = (self.low.max(self.high - width + 1), self.high);
            self.high = range.0 - 1;
            Some(range)
        }
    }
}

/// The bounds a count of `total` rows and a first answered id of `first` name.
pub fn named(first: i64, total: u64) -> (i64, i64) {
    (first, first + i64::try_from(total).unwrap_or(i64::MAX) - 1)
}

/// A window serves one consumer at a time: its calls are awaited one after another.
pub struct FeedWindow<R> {
    reader: R,
    cursor: Cursor,
    buffer: VecDeque<EventRecord>,
    total: u64,
    /// The bounds named at open while their proof is still owed.
    unproven: Option<(i64, i64)>,
}

impl<R: FeedReader> FeedWindow<R> {
    /// Name the feed's bounds at the reader's version; the proof runs beside the first read.
    pub async fn open(reader: R, since: Option<i64>, after: Option<i64>) -> Result<Self, CatalogError> {
        let (total, first) = tokio::join!(reader.count(None), reader.ids(Some(1), None));
        let (total, first) = (total?, first?);
        let mut window = FeedWindow {
            reader,
            cursor: Cursor::new(since, after),
            buffer: VecDeque::new(),
            total,
            unproven: None,
        };
        match first.first() {
            _ if total == 0 => window.cursor.start(None),
            None => {
                let whole = window.read_whole().await?;
                window.cursor.start(whole);
            }
            Some(first) => {
                let bounds = named(*first, total);
                window.cursor.start(Some(bounds));
                window.unproven = Some(bounds);
            }
        }
        Ok(window)
    }

    /// The oldest and the newest seq the feed holds at this version, or `None` when it holds none.
    pub async fn bounds(&mut self) -> Result<Option<(i64, i64)>, CatalogError> {
        if let Some(bounds) = self.unproven.take()
            && let Err(error) = self.settle(held_outside(&self.reader, bounds).await).await
        {
            self.unproven = Some(bounds);
            return Err(error);
        }
        Ok(self.cursor.bounds)
    }

    /// The next `n` rows in cursor order (oldest first after `since`, else newest first); fewer once none are left.
    pub async fn take(&mut self, n: usize) -> Result<Vec<EventRecord>, CatalogError> {
        let Some(bounds) = self.unproven.take() else {
            return take(&self.reader, &mut self.cursor, &mut self.buffer, n).await;
        };
        let (read, outside) = tokio::join!(take(&self.reader, &mut self.cursor, &mut self.buffer, n), held_outside(&self.reader, bounds));
        match outside {
            Ok(false) => read,
            Ok(true) => {
                self.settle(Ok(true)).await?;
                take(&self.reader, &mut self.cursor, &mut self.buffer, n).await
            }
            Err(error) => {
                self.unproven = Some(bounds);
                Err(error)
            }
        }
    }

    /// Act on a proof: bounds that held stand, and ones that did not are proven by offset and the cursor restarts.
    async fn settle(&mut self, outside: Result<bool, CatalogError>) -> Result<(), CatalogError> {
        if outside? {
            let proven = self.prove_by_offset().await?;
            self.cursor.start(proven);
            self.buffer.clear();
        }
        Ok(())
    }

    async fn prove_by_offset(&self) -> Result<Option<(i64, i64)>, CatalogError> {
        let lo = self.cursor.low_bound;
        let offset = usize::try_from(self.total.saturating_sub(1)).unwrap_or(usize::MAX);
        let last = self.reader.ids(Some(1), Some(offset)).await?;
        if let Some(last) = last.first().copied()
            && last >= lo
            && !held_outside(&self.reader, (lo, last)).await?
        {
            return Ok(Some((lo, last)));
        }
        self.read_whole().await
    }

    async fn read_whole(&self) -> Result<Option<(i64, i64)>, CatalogError> {
        tracing::warn!(rows = self.total, "provenance_feed_bounds_read_whole");
        let ids = self.reader.ids(None, None).await?;
        Ok(ids.iter().min().copied().zip(ids.iter().max().copied()))
    }
}

async fn held_outside<R: FeedReader>(reader: &R, (lo, hi): (i64, i64)) -> Result<bool, CatalogError> {
    Ok(reader.count(Some((lo, hi))).await? > 0)
}

/// Read ranges until `n` rows wait or none are left, then hand out `n`.
///
/// A range that comes back short met a hole a prune or a delete left: doubling the width crosses a hole of `h` ids in
/// about `log2(h / n)` more reads, and reads at most twice the ids a take needs, the rest waiting in the buffer.
async fn take<R: FeedReader>(reader: &R, cursor: &mut Cursor, buffer: &mut VecDeque<EventRecord>, n: usize) -> Result<Vec<EventRecord>, CatalogError> {
    let wanted = i64::try_from(n).unwrap_or(i64::MAX);
    let mut width = wanted - i64::try_from(buffer.len()).unwrap_or(i64::MAX);
    while buffer.len() < n
        && let Some((lo, hi)) = cursor.next_range(width)
    {
        let mut records = reader.range(lo, hi, usize::try_from(hi - lo + 1).unwrap_or(0)).await?;
        records.sort_by_key(|record| record.seq);
        if !cursor.forward() {
            records.reverse();
        }
        buffer.extend(records);
        width = width.saturating_mul(2);
    }
    Ok(buffer.drain(..n.min(buffer.len())).collect())
}
