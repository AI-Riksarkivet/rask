//! The group commit: events recorded while a commit is in flight wait and land together in the next one.
//!
//! Every commit is a Lance version and a fragment that each later open and scan of the table pays for, and the ingest
//! lane handles many deliveries at once. So one writer per process lands what is pending, a batch per commit, started by
//! the first event of a burst and gone with its last, with no timer, so an idle estate waits on nothing. Each caller is
//! answered with its own event's outcome.

use crate::intake::Recording;
use lakehouse_tables::CatalogError;
use std::collections::VecDeque;
use std::future::Future;
use std::sync::{Arc, Mutex, MutexGuard};
use tokio::sync::oneshot;

/// The statuses the catalog answers a write whose rows it refuses: invalid input, a body over its cap, a body it cannot
/// process. Any other failure is about the catalog or the caller, not the rows, and splitting would only multiply it.
pub const REFUSED_ROWS: [u16; 3] = [400, 413, 422];

/// What lands a batch: one commit per table, answering the positions of the events it refused as another author's run.
pub trait Committer: Send + Sync + 'static {
    type Item: Send + 'static;
    /// The bytes an item weighs, what a batch is bounded by.
    fn weight(item: &Self::Item) -> usize;
    fn commit(&self, batch: &[Self::Item]) -> impl Future<Output = Result<Vec<usize>, CatalogError>> + Send;
}

struct Waiting<T> {
    items: VecDeque<(T, oneshot::Sender<Recording>)>,
    /// Whether a writer is landing what is waiting; set and cleared under the lock, so a burst has one.
    landing: bool,
}

pub struct Group<C: Committer> {
    committer: C,
    /// The most bytes one commit takes, and always at least one event.
    batch_bytes: usize,
    waiting: Mutex<Waiting<C::Item>>,
}

impl<C: Committer> Group<C> {
    pub fn new(committer: C, batch_bytes: usize) -> Arc<Self> {
        Arc::new(Group {
            committer,
            batch_bytes,
            waiting: Mutex::new(Waiting {
                items: VecDeque::new(),
                landing: false,
            }),
        })
    }

    pub fn committer(&self) -> &C {
        &self.committer
    }

    fn lock(&self) -> MutexGuard<'_, Waiting<C::Item>> {
        self.waiting.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Land `item` with whatever else is waiting, and answer its outcome once the commit that carried it landed.
    ///
    /// A caller that stops waiting leaves its item to be landed all the same: its redelivery finds it recorded.
    pub async fn record(self: &Arc<Self>, item: C::Item) -> Recording {
        let (answer, answered) = oneshot::channel();
        let start = {
            let mut waiting = self.lock();
            waiting.items.push_back((item, answer));
            !std::mem::replace(&mut waiting.landing, true)
        };
        if start {
            tokio::spawn(Arc::clone(self).land_waiting());
        }
        answered
            .await
            .unwrap_or_else(|_| Recording::Failed("the writer stopped before it answered".to_owned()))
    }

    /// The writer of one burst: land what is waiting, a batch per commit, until nothing is.
    async fn land_waiting(self: Arc<Self>) {
        let mut writer = Writer {
            group: Arc::clone(&self),
            unwinding: true,
        };
        loop {
            let batch = self.next_batch();
            if batch.is_empty() {
                break;
            }
            self.land(batch).await;
        }
        // `next_batch` handed the burst on under the lock; clearing the flag again could start a second writer.
        writer.unwinding = false;
    }

    /// The items the next commit takes, oldest first, within the batch's bytes and at least one; none ends the writer.
    fn next_batch(&self) -> Vec<(C::Item, oneshot::Sender<Recording>)> {
        let mut waiting = self.lock();
        let mut batch = Vec::new();
        let mut weight = 0;
        while let Some((next, _)) = waiting.items.front() {
            let next = C::weight(next);
            if !batch.is_empty() && weight + next > self.batch_bytes {
                break;
            }
            weight += next;
            batch.extend(waiting.items.pop_front());
        }
        if batch.is_empty() {
            waiting.landing = false;
        }
        batch
    }

    /// Commit `batch` and answer each caller in it.
    ///
    /// A failure one event causes stays its own: a batch whose rows the catalog refuses lands in halves, each again in
    /// halves while it is refused, so only the event the catalog refuses fails. Any other failure fails the batch whole.
    async fn land(&self, batch: Vec<(C::Item, oneshot::Sender<Recording>)>) {
        let mut work = vec![batch];
        while let Some(batch) = work.pop() {
            let (items, answers): (Vec<C::Item>, Vec<oneshot::Sender<Recording>>) = batch.into_iter().unzip();
            match self.committer.commit(&items).await {
                Ok(foreign) => {
                    for (at, answer) in answers.into_iter().enumerate() {
                        // A caller that stopped waiting is answered by nothing.
                        let _ = answer.send(if foreign.contains(&at) { Recording::ForeignRun } else { Recording::Landed });
                    }
                }
                Err(error) if items.len() > 1 && error.status.is_some_and(|status| REFUSED_ROWS.contains(&status)) => {
                    tracing::warn!(events = items.len(), %error, "provenance_batch_refused");
                    let mut first: Vec<_> = items.into_iter().zip(answers).collect();
                    let second = first.split_off(first.len() / 2);
                    work.push(second);
                    work.push(first);
                }
                Err(error) => {
                    tracing::warn!(events = items.len(), %error, "provenance_record_failed");
                    for answer in answers {
                        let _ = answer.send(Recording::Failed(error.to_string()));
                    }
                }
            }
        }
    }
}

/// Held by a writer while it lands: a writer that ends unwinding hands the burst on, so the next record starts one.
struct Writer<C: Committer> {
    group: Arc<Group<C>>,
    unwinding: bool,
}

impl<C: Committer> Drop for Writer<C> {
    fn drop(&mut self) {
        if self.unwinding {
            self.group.lock().landing = false;
        }
    }
}
