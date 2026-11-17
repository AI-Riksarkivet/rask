//! The outbox on the object store, as `lakehouse.outbox` keeps it: an event staged under `<outbox>/<its envelope's
//! id>.json` before it is published and dropped once the bus took it, and the relay's reads of what is still staged.
//!
//! The order of each side's effects is the model's in `specs/lineage/EventPath.tla`. The producer takes Stage, Publish,
//! then Drop ([`publish_with_outbox`]): a stage that fails degrades the call to a plain publish (StageFails), a publish
//! that fails leaves what was staged staged (PublishFails), and a drop that fails leaves a copy the relay delivers
//! again (DropFails). The relay picks the staged events oldest first ([`Outbox::listing`], [`in_order`],
//! [`Outbox::read_staged`], the model's Pick), and after `lineage::relay::drain_one` has judged, recorded and published
//! one again, [`Outbox::drop_staged`] is its RelayDrop.
//!
//! A stage needs only put permission on its own key and a drop only delete permission (the relay's identity is granted
//! nothing else on the outbox): each is one plain request that writes or removes nothing beside its object.

use crate::storage::{Listed, S3, StorageError};
use std::sync::Arc;

/// Where an outbox is: its bucket, and the key prefix its objects are under (empty at the bucket's root).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Location {
    pub bucket: String,
    pub prefix: String,
}

impl Location {
    /// The outbox `uri` names (`s3://<bucket>[/<prefix>]`), or why it names none on the object store.
    pub fn parse(uri: &str) -> Result<Self, String> {
        let rest = uri
            .strip_prefix("s3://")
            .ok_or_else(|| format!("the outbox {uri:?} is not on the object store (s3://)"))?
            .trim_end_matches('/');
        let (bucket, prefix) = rest.split_once('/').unwrap_or((rest, ""));
        Ok(Location {
            bucket: bucket.to_owned(),
            prefix: prefix.trim_end_matches('/').to_owned(),
        })
    }

    /// The object an event staged under `key` is.
    pub fn object_key(&self, key: &str) -> String {
        if self.prefix.is_empty() {
            format!("{key}.json")
        } else {
            format!("{}/{key}.json", self.prefix)
        }
    }

    /// What a listing of the outbox is asked for: everything directly under the prefix.
    fn listed_under(&self) -> String {
        if self.prefix.is_empty() { String::new() } else { format!("{}/", self.prefix) }
    }
}

/// The staged events of `listing`, oldest first and at most `limit`, each as its key (the envelope's id) and its
/// object. An object that is no `.json` is no staged event. Objects written in the same instant keep the listing's
/// order. Oldest first, so the events at risk longest drain first and the backlog's age falls as the relay catches up.
pub fn in_order(listing: &[Listed], limit: Option<usize>) -> Vec<(String, &Listed)> {
    let mut staged: Vec<&Listed> = listing.iter().filter(|listed| listed.key.ends_with(".json")).collect();
    staged.sort_by_key(|listed| listed.modified_ns);
    staged
        .into_iter()
        .take(limit.unwrap_or(usize::MAX))
        .map(|listed| {
            let name = listed.key.rsplit('/').next().unwrap_or_default();
            (name.strip_suffix(".json").unwrap_or(name).to_owned(), listed)
        })
        .collect()
}

/// How many events are staged, and how long the oldest has been, in seconds, at `now_ns`: the outbox's saturation.
pub fn backlog(listing: &[Listed], now_ns: i64) -> (usize, f64) {
    let staged = in_order(listing, None);
    let age = match staged.first() {
        Some((_, oldest)) if oldest.modified_ns != 0 => ((i128::from(now_ns) - i128::from(oldest.modified_ns)) as f64 / 1e9).max(0.0),
        _ => 0.0,
    };
    (staged.len(), age)
}

/// One staged event: the key a drop names, and the text as it was staged.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Staged {
    pub key: String,
    pub body: Vec<u8>,
}

/// One outbox, on the store `s3` reaches.
pub struct Outbox {
    location: Location,
    s3: Arc<S3>,
}

impl Outbox {
    pub fn new(uri: &str, s3: Arc<S3>) -> Result<Self, String> {
        Ok(Outbox {
            location: Location::parse(uri)?,
            s3,
        })
    }

    /// Stage `text` under `key`. Staging the same event again writes the same object.
    pub async fn stage(&self, key: &str, text: &str) -> Result<(), StorageError> {
        self.s3
            .put(&self.location.bucket, &self.location.object_key(key), text.as_bytes().to_vec(), false)
            .await
    }

    /// Drop what is staged under `key`; one already gone is what was asked for.
    pub async fn drop_staged(&self, key: &str) -> Result<(), StorageError> {
        self.s3.delete(&self.location.bucket, &self.location.object_key(key)).await
    }

    /// What is staged under `key`, or `None` where nothing is.
    pub async fn read(&self, key: &str) -> Result<Option<Vec<u8>>, StorageError> {
        self.s3.get(&self.location.bucket, &self.location.object_key(key)).await
    }

    /// Every object directly in the outbox. Metadata only: no object is read.
    pub async fn listing(&self) -> Result<Vec<Listed>, StorageError> {
        self.s3.list(&self.location.bucket, &self.location.listed_under()).await
    }

    /// The staged events of `listing`, oldest first and at most `limit`, read. An event dropped between the listing and
    /// its read was published by whoever dropped it, and is passed over.
    pub async fn read_staged(&self, listing: &[Listed], limit: Option<usize>) -> Result<Vec<Staged>, StorageError> {
        let mut staged = Vec::new();
        for (key, listed) in in_order(listing, limit) {
            if let Some(body) = self.s3.get(&self.location.bucket, &listed.key).await? {
                staged.push(Staged { key, body });
            }
        }
        Ok(staged)
    }
}

/// The producer's effects on one event. Each answers whether it was done, and why not.
pub trait Producing {
    fn stage(&mut self) -> impl Future<Output = Result<(), String>>;
    fn publish(&mut self) -> impl Future<Output = Result<(), String>>;
    fn drop_staged(&mut self) -> impl Future<Output = Result<(), String>>;
}

/// Whether the event was staged before its publish.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Staging {
    /// No outbox is configured: a plain publish.
    Off,
    Staged,
    /// The stage failed, and the call went on as a plain publish.
    Failed,
}

/// A publish the bus took: whether it was staged, and, where it was, whether the staged copy was dropped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Published {
    pub staging: Staging,
    pub dropped: Option<bool>,
}

/// A publish that did not reach the bus, and whether its event stays staged for the relay to deliver.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unpublished {
    pub staging: Staging,
    pub error: String,
}

impl Unpublished {
    /// Whether the outbox holds the event, so that its relay delivers it.
    pub fn left_staged(&self) -> bool {
        self.staging == Staging::Staged
    }
}

/// Publish one event durably: stage it when `outbox` is configured, publish it, then drop the staged copy.
pub async fn publish_with_outbox(outbox: bool, effects: &mut impl Producing) -> Result<Published, Unpublished> {
    let staging = match outbox {
        false => Staging::Off,
        true => match effects.stage().await {
            Ok(()) => Staging::Staged,
            Err(error) => {
                // Raising here would lose the event outright, where a plain publish that reaches the bus delivers it.
                tracing::warn!(%error, "outbox_stage_failed");
                Staging::Failed
            }
        },
    };
    if let Err(error) = effects.publish().await {
        return Err(Unpublished { staging, error });
    }
    if staging != Staging::Staged {
        return Ok(Published { staging, dropped: None });
    }
    let dropped = match effects.drop_staged().await {
        Ok(()) => true,
        Err(error) => {
            tracing::warn!(%error, "outbox_drop_failed");
            false
        }
    };
    Ok(Published {
        staging,
        dropped: Some(dropped),
    })
}
