//! The public keys each signing identity publishes, read from the secret store and cached per identity, so a verifier
//! reads them through `PublicKeySource` and a stream of forged key ids costs one read per pacing interval.
//!
//! `fetch(identity)` answers the fields of the identity's published list, or an empty map when it could not be read.
//! Unreadable and absent are one answer, `KeysUnavailable`: either way nothing is known about a signature, and a
//! verifier that refused on a store blip would destroy honest events.

use super::store::{Budget, SecretStore};
use crate::auth::issuer::Clock;
use crate::auth::principal::{Principal, SEPARATOR, SERVICE_ACCOUNT_IDP};
use crate::signing::nkey::parse_published_keys;
use crate::signing::{KeysUnavailable, PublicKeySource};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

/// The field of `signing-public-<name>` that holds the comma-separated public NKEYs, current first.
pub const KEYS_FIELD: &str = "keys";
/// How long one successful read is served without asking again: short, because the list is the trust anchor and a key
/// removed from it stops verifying within this.
pub const KEY_TTL: Duration = Duration::from_secs(60);
/// The least time between two reads forced by a key id the cached list lacks, far below the 120 s before lineage asks for
/// a delivery again, so a key id met again is judged on a read begun since it was first met.
pub const KEY_REFRESH_INTERVAL: Duration = Duration::from_secs(30);
/// How many key ids met unknown one identity remembers: the ids are the events' own text, so the memory is bounded, and
/// per identity so a flood against one cannot push another's out. An id pushed out is met afresh, which costs its
/// event one more retry and never a refusal.
pub const MAX_UNKNOWN_KEY_IDS: usize = 8192;
/// The bound on one verifier's read of a published list: one short attempt, since it sits on the delivery path and an
/// outage asks for the delivery again instead.
pub const KEY_READ_TIMEOUT: Duration = Duration::from_secs(2);
/// The longest a blocking verifier waits for that read: its three bounded waits (a login in flight, a login, the read).
const KEY_READ_WAIT: Duration = Duration::from_secs(7);

/// The name an identity's key pair is stored under: a service account's name for its subject, else the identity.
pub fn signing_key_name(identity: &str) -> &str {
    let prefix = format!("{SERVICE_ACCOUNT_IDP}{SEPARATOR}");
    match (Principal::parse(identity), identity.strip_prefix(&prefix)) {
        (Ok(_), Some(subject)) => subject.rsplit_once(':').map_or(subject, |(_, account)| account),
        _ => identity,
    }
}

/// The secret holding an identity's published public keys, which every verifier's role reads.
pub fn signing_public_secret(identity: &str) -> String {
    format!("signing-public-{}", signing_key_name(identity))
}

/// How a published list is read: the fields of the identity's list, or an empty map when it could not be read.
pub type Fetch = Arc<dyn Fn(&str) -> BTreeMap<String, String> + Send + Sync>;

/// Each identity's list read from `store` under this process's own login, one attempt of `KEY_READ_TIMEOUT`.
///
/// The read runs on `runtime`, and the caller blocks on it: a verifier runs on a blocking thread, as verification
/// reads the store. A name that is no secret name, and a read that does not end in time, are an empty map.
pub fn store_fetch(store: Arc<SecretStore>, runtime: tokio::runtime::Handle) -> Fetch {
    Arc::new(move |identity: &str| {
        let (answer, answered) = std::sync::mpsc::channel();
        let (store, name) = (Arc::clone(&store), signing_public_secret(identity));
        runtime.spawn(async move {
            let fields = store.read(&name, Budget::one_attempt(KEY_READ_TIMEOUT)).await.unwrap_or_default();
            // The verifier may have stopped waiting; then nobody needs the answer.
            let _ = answer.send(fields);
        });
        answered.recv_timeout(KEY_READ_WAIT).unwrap_or_default()
    })
}

#[derive(Debug, Clone)]
struct Read {
    keys: Vec<String>,
    /// When the read began: an unknown key id is judged only on a read begun after it was first met.
    started: Instant,
    /// When it ended: the age the TTL and the pacing are counted from.
    finished: Instant,
}

/// What a key id the cached list lacks is judged on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnknownKeyId {
    /// The held list, read after the key id was first met.
    Held,
    /// Nothing yet: the last read ended inside the pacing interval, so the door judges the event again later.
    Declined,
    /// A read now.
    Read,
}

/// How a key id first met at `first_met` is judged, given the held read's start and end, at `now`.
pub fn judge_unknown_key_id(held: Option<(Instant, Instant)>, first_met: Instant, now: Instant) -> UnknownKeyId {
    match held {
        Some((started, _)) if started >= first_met => UnknownKeyId::Held,
        Some((_, finished)) if now.saturating_duration_since(finished) < KEY_REFRESH_INTERVAL => UnknownKeyId::Declined,
        _ => UnknownKeyId::Read,
    }
}

/// The key ids met unknown for one identity, each with when it was first met, least recently met evicted first.
#[derive(Debug, Default)]
struct Sightings {
    first_met: HashMap<String, (Instant, u64)>,
    by_recency: BTreeMap<u64, String>,
    next: u64,
}

impl Sightings {
    /// Record `kid` met at `arrived`, and return when it was first met.
    fn record(&mut self, kid: &str, arrived: Instant) -> Instant {
        let first = self.first_met.get(kid).map_or(arrived, |(first, _)| (*first).min(arrived));
        if let Some((_, recency)) = self.first_met.insert(kid.to_owned(), (first, self.next)) {
            self.by_recency.remove(&recency);
        }
        self.by_recency.insert(self.next, kid.to_owned());
        self.next += 1;
        if self.first_met.len() > MAX_UNKNOWN_KEY_IDS
            && let Some((_, oldest)) = self.by_recency.pop_first()
        {
            self.first_met.remove(&oldest);
        }
        first
    }
}

#[derive(Debug, Default)]
struct Identity {
    read: Option<Read>,
    /// When the latest read ended in failure, cleared by one that succeeds.
    failed: Option<Instant>,
    sightings: Sightings,
}

/// What each signer publishes, read through `fetch` and cached by what the store said.
///
/// Reads of one identity are single-flight behind its own lock, and a verification shares the read in progress
/// whatever it answers. A failure answers every verification that began before it ended, and none that began after, so
/// verifications queued behind a hung store cost one read and a half-seeded store cannot pin a missing list.
pub struct PublishedKeys {
    fetch: Fetch,
    clock: Clock,
    identities: Mutex<HashMap<String, Arc<Mutex<Identity>>>>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl PublishedKeys {
    pub fn new(fetch: Fetch) -> Self {
        PublishedKeys {
            fetch,
            clock: Arc::new(Instant::now),
            identities: Mutex::default(),
        }
    }

    pub fn with_clock(self, clock: Clock) -> Self {
        PublishedKeys { clock, ..self }
    }

    /// The key source for one verification, stamped with the moment it began and the key id its event names.
    pub fn for_event(&self, kid: Option<&str>) -> KeysForOneEvent<'_> {
        KeysForOneEvent {
            keys: self,
            arrived: (self.clock)(),
            kid: kid.map(str::to_owned),
        }
    }

    fn identity(&self, identity: &str) -> Arc<Mutex<Identity>> {
        Arc::clone(locked(&self.identities).entry(identity.to_owned()).or_default())
    }

    /// The identity's list from the cache while it is younger than the TTL, else from a fresh read.
    fn cached(&self, identity: &str, arrived: Instant) -> Result<Vec<String>, KeysUnavailable> {
        let lock = self.identity(identity);
        let mut held = locked(&lock);
        let fresh = held
            .read
            .as_ref()
            .filter(|read| (self.clock)().saturating_duration_since(read.finished) < KEY_TTL);
        match fresh.map(|read| read.keys.clone()) {
            Some(keys) => Ok(keys),
            None => self.read(identity, &mut held, arrived),
        }
    }

    /// The identity's list as read after `kid` was first met unknown, which is what an unknown key id is judged on.
    fn read_for_unknown_key_id(&self, identity: &str, kid: Option<&str>, arrived: Instant) -> Result<Vec<String>, KeysUnavailable> {
        let lock = self.identity(identity);
        let mut held = locked(&lock);
        let first_met = kid.map_or(arrived, |kid| held.sightings.record(kid, arrived));
        let times = held.read.as_ref().map(|read| (read.started, read.finished));
        match (
            judge_unknown_key_id(times, first_met, (self.clock)()),
            held.read.as_ref().map(|read| read.keys.clone()),
        ) {
            (UnknownKeyId::Held, Some(keys)) => Ok(keys),
            (UnknownKeyId::Declined, _) => Err(unavailable(identity)),
            _ => self.read(identity, &mut held, arrived),
        }
    }

    /// One read through the fetch, with the identity's lock held. A read that failed since the verification asking
    /// began is its answer: it is the read it waited behind, or one begun since.
    fn read(&self, identity: &str, held: &mut Identity, arrived: Instant) -> Result<Vec<String>, KeysUnavailable> {
        if held.failed.is_some_and(|failed| failed >= arrived) {
            return Err(unavailable(identity));
        }
        let started = (self.clock)();
        let fields = (self.fetch)(identity);
        let keys = parse_published_keys(fields.get(KEYS_FIELD).map_or("", String::as_str));
        if keys.is_empty() {
            held.failed = Some((self.clock)());
            return Err(unavailable(identity));
        }
        held.read = Some(Read {
            keys: keys.clone(),
            started,
            finished: (self.clock)(),
        });
        held.failed = None;
        Ok(keys)
    }
}

fn unavailable(identity: &str) -> KeysUnavailable {
    KeysUnavailable { identity: identity.to_owned() }
}

/// The key source of one verification: `published` may answer from the cache, `refresh` only from a read begun since the
/// key id was first met.
pub struct KeysForOneEvent<'a> {
    keys: &'a PublishedKeys,
    arrived: Instant,
    kid: Option<String>,
}

impl PublicKeySource for KeysForOneEvent<'_> {
    fn published(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable> {
        self.keys.cached(identity, self.arrived)
    }

    fn refresh(&self, identity: &str) -> Result<Vec<String>, KeysUnavailable> {
        self.keys.read_for_unknown_key_id(identity, self.kid.as_deref(), self.arrived)
    }
}
