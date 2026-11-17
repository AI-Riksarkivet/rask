//! Lineage's own settings, read from the environment under the names the chart gives the lineage Deployment, as
//! `lineage.core.config.LineageSettings` reads them. A value that cannot be taken refuses the start, naming its variable.
//!
//! The authentication and authorization settings are the estate's one set (`lakehouse_core::auth::settings`), the bus's
//! are `lakehouse_bus::BusSettings` and the store identity is `lakehouse_core::storage::StoreSettings`: each is read
//! where it is defined.

use crate::signature::Signing;
use lakehouse_core::delivery::Lane;
use lakehouse_core::naming::{CATALOG_DATASET_SCHEME, is_catalog_dataset_namespace};
use lakehouse_core::storage::StoreSettings;
use std::collections::{BTreeSet, HashMap};
use std::time::Duration;

/// The stream lineage's events are kept on, and lineage's durable consumer on it, as the chart's stream Job declares them.
pub const STREAM: &str = "LINEAGE";
pub const CONSUMER: &str = "lineage-ingest";

#[derive(Debug, Clone, PartialEq)]
pub struct Settings {
    pub catalog_url: String,
    /// The projected token for the catalog's doors, re-read at each call; empty sends no bearer.
    pub catalog_identity_token_file: String,
    pub provenance_namespace: String,
    pub catalog_dataset_namespace: String,
    pub catalog_timeout: Duration,
    /// The graph backend every recorded event is forwarded to; empty forwards nothing.
    pub forward_url: String,
    pub forward_endpoint: String,
    pub forward_api_key_secret: String,
    pub forward_api_key_field: String,
    pub forward_batch: usize,
    pub forward_timeout: Duration,
    pub signing: Signing,
    pub events_topic: String,
    pub deliveries_in_flight: usize,
    pub delivery_attempts: u32,
    pub delivery_retry: Duration,
    pub delivery_ack_wait: Duration,
    pub publish_timeout: Duration,
    /// Seconds between two relay ticks; 0 runs no relay.
    pub relay_period_seconds: f64,
    pub timer_root: String,
    pub read_only: bool,
    pub outbox_uri: String,
    /// The most staged events one tick drains, oldest first; 0 or less drains every one.
    pub outbox_drain_limit: i64,
    /// Feed retention in days of arrival; 0 keeps every row.
    pub events_retention_days: u32,
    pub audit_enabled: bool,
    pub docs_enabled: bool,
    pub max_body_bytes: u64,
    pub request_deadline: Duration,
    /// The store identity, where an outbox or a timer root is on the store.
    pub store: Option<StoreSettings>,
}

/// The environment as pydantic-settings reads it: names without regard to case, values as pydantic's lax mode takes them.
struct Environment(HashMap<String, String>);

impl Environment {
    fn text(&self, name: &str, default: &str) -> String {
        self.0.get(name).map_or_else(|| default.to_owned(), String::clone)
    }

    fn refused(name: &str, value: &str, why: &str) -> String {
        format!("{name} is {value:?}, which is not {why}")
    }

    fn whole(&self, name: &str, default: i64, least: Option<i64>) -> Result<i64, String> {
        let Some(value) = self.0.get(name) else { return Ok(default) };
        let digits = value.trim().replace('_', "");
        let digits = digits
            .split_once('.')
            .map_or(digits.as_str(), |(whole, zeros)| if zeros.bytes().all(|b| b == b'0') { whole } else { "" });
        let number: i64 = digits
            .parse()
            .ok()
            .filter(|_| !value.contains("__"))
            .ok_or_else(|| Self::refused(name, value, "a whole number"))?;
        match least {
            Some(least) if number < least => Err(Self::refused(name, value, &format!("at least {least}"))),
            _ => Ok(number),
        }
    }

    /// A number of seconds, `above` (exclusive) or `least` (inclusive) a bound.
    fn seconds(&self, name: &str, default: f64, bound: f64, inclusive: bool) -> Result<f64, String> {
        let Some(value) = self.0.get(name) else { return Ok(default) };
        let number: f64 = value.trim().parse().map_err(|_| Self::refused(name, value, "a number"))?;
        let within = if inclusive { number >= bound } else { number > bound };
        if !within {
            let side = if inclusive { "at least" } else { "above" };
            return Err(Self::refused(name, value, &format!("{side} {bound}")));
        }
        Ok(number)
    }

    fn duration(&self, name: &str, default: f64, bound: f64, inclusive: bool) -> Result<Duration, String> {
        let seconds = self.seconds(name, default, bound, inclusive)?;
        Duration::try_from_secs_f64(seconds).map_err(|_| Self::refused(name, &seconds.to_string(), "a finite number of seconds"))
    }

    fn flag(&self, name: &str, default: bool) -> Result<bool, String> {
        match self.0.get(name).map(|value| (value, value.trim().to_lowercase())) {
            None => Ok(default),
            Some((_, word)) if matches!(word.as_str(), "1" | "true" | "t" | "yes" | "y" | "on") => Ok(true),
            Some((_, word)) if matches!(word.as_str(), "0" | "false" | "f" | "no" | "n" | "off") => Ok(false),
            Some((value, _)) => Err(Self::refused(name, value, "true or false")),
        }
    }

    fn identities(&self, name: &str) -> Result<BTreeSet<String>, String> {
        self.0.get(name).map_or(Ok(BTreeSet::new()), |value| {
            serde_json::from_str(value).map_err(|_| Self::refused(name, value, "a JSON list of identities"))
        })
    }
}

fn on_the_store(name: &str, uri: &str) -> Result<bool, String> {
    match uri {
        "" => Ok(false),
        _ if uri.starts_with("s3://") => Ok(true),
        _ => Err(format!("{name} is {uri:?}, which is not on the object store (s3://)")),
    }
}

impl Settings {
    /// The settings `environment` holds, or the first reason one of them cannot be taken.
    pub fn from_environment(environment: impl IntoIterator<Item = (String, String)>) -> Result<Self, String> {
        let pairs: Vec<(String, String)> = environment.into_iter().collect();
        let env = Environment(pairs.iter().map(|(name, value)| (name.to_uppercase(), value.clone())).collect());
        let dataset_namespace = env.text(
            "LINEAGE_CATALOG_DATASET_NAMESPACE",
            &format!("{CATALOG_DATASET_SCHEME}://localhost/api/catalog"),
        );
        if !is_catalog_dataset_namespace(&dataset_namespace) || dataset_namespace == format!("{CATALOG_DATASET_SCHEME}://") {
            return Err(format!(
                "LINEAGE_CATALOG_DATASET_NAMESPACE is {dataset_namespace:?}: a dataset namespace is {CATALOG_DATASET_SCHEME}://<host><prefix>"
            ));
        }
        let small = |number: i64| usize::try_from(number).unwrap_or(usize::MAX);
        let settings = Settings {
            catalog_url: env.text("LINEAGE_CATALOG_URL", ""),
            catalog_identity_token_file: env.text("RASK_CATALOG_IDENTITY_TOKEN_FILE", ""),
            provenance_namespace: env.text("LINEAGE_PROVENANCE_NAMESPACE", "lance_catalog$provenance"),
            catalog_dataset_namespace: dataset_namespace,
            catalog_timeout: env.duration("LINEAGE_CATALOG_TIMEOUT_SECONDS", 30.0, 0.0, false)?,
            forward_url: env.text("LINEAGE_FORWARD_URL", ""),
            forward_endpoint: env.text("LINEAGE_FORWARD_ENDPOINT", "api/v1/lineage"),
            forward_api_key_secret: env.text("LINEAGE_FORWARD_API_KEY_SECRET", ""),
            forward_api_key_field: env.text("LINEAGE_FORWARD_API_KEY_FIELD", "api-key"),
            forward_batch: small(env.whole("LINEAGE_FORWARD_BATCH", 100, Some(1))?),
            forward_timeout: env.duration("LINEAGE_FORWARD_TIMEOUT_SECONDS", 10.0, 0.0, false)?,
            signing: Signing {
                signers: env.identities("LINEAGE_SIGNERS")?,
                delegators: env.identities("LINEAGE_DELEGATORS")?,
            },
            events_topic: env.text("LINEAGE_EVENTS_TOPIC", "lineage.events.v1"),
            deliveries_in_flight: small(env.whole("LINEAGE_DELIVERIES_IN_FLIGHT", 64, Some(1))?),
            delivery_attempts: u32::try_from(env.whole("LINEAGE_DELIVERY_ATTEMPTS", 5, Some(1))?).unwrap_or(u32::MAX),
            delivery_retry: env.duration("LINEAGE_DELIVERY_RETRY_SECONDS", 120.0, 0.0, true)?,
            delivery_ack_wait: env.duration("LINEAGE_DELIVERY_ACK_WAIT_SECONDS", 300.0, Lane::SETTLE.as_secs_f64(), false)?,
            publish_timeout: env.duration("LINEAGE_PUBLISH_TIMEOUT_SECONDS", 5.0, 0.1, true)?,
            relay_period_seconds: env.seconds("LINEAGE_RELAY_PERIOD_SECONDS", 0.0, 0.0, true)?,
            timer_root: env.text("LINEAGE_TIMER_ROOT", ""),
            read_only: env.flag("LINEAGE_READ_ONLY", false)?,
            outbox_uri: env.text("LINEAGE_OUTBOX_URI", ""),
            outbox_drain_limit: env.whole("LINEAGE_OUTBOX_DRAIN_LIMIT", 500, None)?,
            events_retention_days: u32::try_from(env.whole("LINEAGE_EVENTS_RETENTION_DAYS", 7, Some(0))?).unwrap_or(u32::MAX),
            audit_enabled: env.flag("LANCE_AUDIT_ENABLED", true)?,
            docs_enabled: env.flag("LINEAGE_DOCS", false)?,
            max_body_bytes: env.whole("LINEAGE_MAX_BODY_BYTES", 8 * 1024 * 1024, Some(1))?.unsigned_abs(),
            request_deadline: env.duration("LINEAGE_REQUEST_DEADLINE_SECONDS", 300.0, 0.0, false)?,
            store: None,
        };
        if settings.relay_period_seconds > 0.0 && settings.timer_root.is_empty() {
            return Err(
                "LINEAGE_RELAY_PERIOD_SECONDS is set and LINEAGE_TIMER_ROOT is empty: the relay's timer claims each window under a control root".to_owned(),
            );
        }
        let on_store = on_the_store("LINEAGE_OUTBOX_URI", &settings.outbox_uri)? | on_the_store("LINEAGE_TIMER_ROOT", &settings.timer_root)?;
        let store = if on_store {
            Some(StoreSettings::from_environment("LINEAGE", pairs)?)
        } else {
            None
        };
        Ok(Settings { store, ..settings })
    }

    /// The consumer lineage pulls, and the numbers the chart gives its deliveries.
    pub fn lane(&self) -> Result<Lane, String> {
        Lane::new(
            STREAM,
            CONSUMER,
            self.delivery_attempts,
            self.delivery_retry,
            self.delivery_ack_wait,
            self.deliveries_in_flight,
        )
    }

    /// The most staged events one tick drains; `None` drains every one.
    pub fn drain_limit(&self) -> Option<usize> {
        usize::try_from(self.outbox_drain_limit).ok().filter(|limit| *limit > 0)
    }

    /// Where events are POSTed: the HTTP transport's `url` joined to its `endpoint`.
    pub fn endpoint_url(&self) -> String {
        format!("{}/{}", self.forward_url.trim_end_matches('/'), self.forward_endpoint.trim_start_matches('/'))
    }

    /// The forwarder's name in the cursors table: its endpoint and the feed table `feed` names, so a new backend, and a
    /// feed created again, each start from the feed's oldest row.
    pub fn follower(&self, feed: &str) -> String {
        format!("forward:{}@{feed}", self.endpoint_url())
    }
}
