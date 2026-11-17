//! The group commit with a committer that keeps every batch it is given (ADR 0228): what waits lands together, a
//! refused batch lands in halves, any other failure fails the batch whole, and a batch is bounded by bytes.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::service::problem::ErrorCode;
use lakehouse_tables::CatalogError;
use lineage::intake::Recording;
use lineage::store::group::{Committer, Group};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use tokio::sync::Semaphore;

/// An item: its number, its weight, and how its commit answers when it is in the batch.
#[derive(Debug, Clone, Copy)]
struct Item {
    number: u32,
    weight: usize,
    answers: Answer,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Answer {
    Lands,
    Foreign,
    /// The catalog refuses the rows of any batch holding it.
    RefusedRows,
    /// The catalog cannot take a write now.
    Unavailable,
}

#[derive(Default)]
struct Keeper {
    batches: Mutex<Vec<Vec<u32>>>,
    /// How many commits have started.
    entered: AtomicUsize,
    /// Holds each commit until a permit is added, so the test decides what is in flight.
    gate: Option<Arc<Semaphore>>,
}

impl Committer for Keeper {
    type Item = Item;

    fn weight(item: &Item) -> usize {
        item.weight
    }

    async fn commit(&self, batch: &[Item]) -> Result<Vec<usize>, CatalogError> {
        self.entered.fetch_add(1, Ordering::SeqCst);
        if let Some(gate) = &self.gate {
            gate.acquire().await.unwrap().forget();
        }
        self.batches.lock().unwrap().push(batch.iter().map(|item| item.number).collect());
        if batch.iter().any(|item| item.answers == Answer::RefusedRows) {
            return Err(CatalogError::answered(400, "{}"));
        }
        if batch.iter().any(|item| item.answers == Answer::Unavailable) {
            return Err(CatalogError::local(ErrorCode::ServiceUnavailable, "the catalog is unavailable"));
        }
        Ok(batch
            .iter()
            .enumerate()
            .filter(|(_, item)| item.answers == Answer::Foreign)
            .map(|(at, _)| at)
            .collect())
    }
}

fn item(number: u32, answers: Answer) -> Item {
    Item { number, weight: 1, answers }
}

/// Record `items` at once while the first commit is held, and answer each caller's outcome with the batches committed.
async fn recorded(items: Vec<Item>, batch_bytes: usize) -> (Vec<Recording>, Vec<Vec<u32>>) {
    let gate = Arc::new(Semaphore::new(0));
    let group = Group::new(
        Keeper {
            gate: Some(Arc::clone(&gate)),
            ..Keeper::default()
        },
        batch_bytes,
    );
    let mut first = items.into_iter();
    let head = first.next().unwrap();
    let mut waiting = vec![tokio::spawn({
        let group = Arc::clone(&group);
        async move { group.record(head).await }
    })];
    // The first item's commit is in flight before the rest arrive, and they all wait before it lands.
    while group.committer().entered.load(Ordering::SeqCst) == 0 {
        tokio::task::yield_now().await;
    }
    for item in first {
        let group = Arc::clone(&group);
        waiting.push(tokio::spawn(async move { group.record(item).await }));
    }
    for _ in 0..10 {
        tokio::task::yield_now().await;
    }
    gate.add_permits(1000);
    let mut outcomes = Vec::new();
    for waiting in waiting {
        outcomes.push(waiting.await.unwrap());
    }
    let batches = group.committer().batches.lock().unwrap().clone();
    (outcomes, batches)
}

#[tokio::test(flavor = "current_thread")]
async fn events_recorded_while_a_commit_is_in_flight_land_together_in_the_next() {
    let items = (0..6).map(|number| item(number, Answer::Lands)).collect();

    let (outcomes, batches) = recorded(items, 1 << 20).await;

    assert_eq!(batches, vec![vec![0], vec![1, 2, 3, 4, 5]]);
    assert!(outcomes.iter().all(|outcome| *outcome == Recording::Landed));
}

#[tokio::test(flavor = "current_thread")]
async fn each_caller_is_told_its_own_outcome() {
    let items = vec![
        item(0, Answer::Lands),
        item(1, Answer::Foreign),
        item(2, Answer::Lands),
        item(3, Answer::Foreign),
    ];

    let (outcomes, _) = recorded(items, 1 << 20).await;

    assert_eq!(
        outcomes,
        vec![Recording::Landed, Recording::ForeignRun, Recording::Landed, Recording::ForeignRun]
    );
}

#[tokio::test(flavor = "current_thread")]
async fn a_batch_whose_rows_are_refused_lands_in_halves_until_only_the_refused_event_fails() {
    let mut items: Vec<Item> = (0..6).map(|number| item(number, Answer::Lands)).collect();
    items[3].answers = Answer::RefusedRows;

    let (outcomes, batches) = recorded(items, 1 << 20).await;

    assert_eq!(batches, vec![vec![0], vec![1, 2, 3, 4, 5], vec![1, 2], vec![3, 4, 5], vec![3], vec![4, 5]]);
    assert!(matches!(outcomes[3], Recording::Failed(_)), "{outcomes:?}");
    assert!([0, 1, 2, 4, 5].iter().all(|at| outcomes[*at] == Recording::Landed), "{outcomes:?}");
}

#[tokio::test(flavor = "current_thread")]
async fn any_other_failure_fails_the_batch_whole_without_splitting() {
    let mut items: Vec<Item> = (0..4).map(|number| item(number, Answer::Lands)).collect();
    items[2].answers = Answer::Unavailable;

    let (outcomes, batches) = recorded(items, 1 << 20).await;

    assert_eq!(batches, vec![vec![0], vec![1, 2, 3]]);
    assert_eq!(outcomes[0], Recording::Landed);
    assert!(outcomes[1..].iter().all(|outcome| matches!(outcome, Recording::Failed(_))), "{outcomes:?}");
}

#[tokio::test(flavor = "current_thread")]
async fn a_batch_takes_what_its_bytes_allow_and_always_one_event() {
    let mut items: Vec<Item> = (0..6).map(|number| item(number, Answer::Lands)).collect();
    for (at, weight) in [(1, 4), (2, 4), (3, 9), (4, 1), (5, 1)] {
        items[at].weight = weight;
    }

    let (_, batches) = recorded(items, 8).await;

    assert_eq!(
        batches,
        vec![vec![0], vec![1, 2], vec![3], vec![4, 5]],
        "an event heavier than a batch goes alone"
    );
}

#[tokio::test]
async fn a_burst_ends_its_writer_and_the_next_record_starts_another() {
    let group = Group::new(Keeper::default(), 1 << 20);

    assert_eq!(group.record(item(0, Answer::Lands)).await, Recording::Landed);
    assert_eq!(group.record(item(1, Answer::Lands)).await, Recording::Landed);

    assert_eq!(*group.committer().batches.lock().unwrap(), vec![vec![0], vec![1]]);
}
