//! The governance trail: one record per security-relevant action, under the instrumentation scope `lance.audit`, which
//! is what the platform's gateway routes the trail to its own table by and admits only from the lakehouse's accounts.
//!
//! The log path a record rides can lose one without a word, so every record emitted is also counted on the metric path
//! (`audit.records`, by action and outcome): a store holding fewer records over a window than were counted lost some.

use super::logging::{LogSink, Value};
use crate::service::trace;
use opentelemetry::KeyValue;
use opentelemetry::metrics::{Counter, Meter};
use std::sync::{Arc, OnceLock};
use tracing::Level;

/// The process's trail, once installed: every door of every service records through it.
static TRAIL: OnceLock<Audit> = OnceLock::new();

/// Record on the process's trail (`Audit::install`); nothing before one is installed.
pub fn emit(action: AuditAction, outcome: AuditOutcome, subject: Option<&str>, resource: Option<&str>, fields: &[(&str, Value)]) {
    if let Some(trail) = TRAIL.get() {
        trail.record(action, outcome, subject, resource, fields);
    }
}

/// The scope every record is emitted under, and the meter `audit.records` is counted on.
pub const AUDIT_SCOPE: &str = "lance.audit";
/// The record's format, stamped as `audit.format`: a renamed or retyped field is a new format.
pub const AUDIT_FORMAT: &str = "2.0";
/// What a read record names as its columns when the read named none.
pub const ALL_COLUMNS: &str = "all";

/// What an action came to: an authorization verdict, or what the action did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditOutcome {
    Allow,
    Deny,
    Success,
    Failure,
}

impl AuditOutcome {
    pub const ALL: [AuditOutcome; 4] = [AuditOutcome::Allow, AuditOutcome::Deny, AuditOutcome::Success, AuditOutcome::Failure];

    pub fn label(self) -> &'static str {
        match self {
            AuditOutcome::Allow => "allow",
            AuditOutcome::Deny => "deny",
            AuditOutcome::Success => "success",
            AuditOutcome::Failure => "failure",
        }
    }
}

/// An action of a closed vocabulary: the lakehouse's are the constants here, and a client names its own the same way,
/// as constants, so no text off a request reaches the trail as an action.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AuditAction(pub &'static str);

macro_rules! actions {
    ($($name:ident = $label:literal),* $(,)?) => {
        impl AuditAction {
            $(pub const $name: AuditAction = AuditAction($label);)*
            /// The lakehouse's whole vocabulary.
            pub const ALL: &'static [AuditAction] = &[$(AuditAction::$name),*];
        }
    };
}

actions! {
    AUTHN = "authn", AUTHZ = "authz", AUTHZ_DECISION = "authz_decision", AUTHZ_LISTING = "authz_listing",
    READ_DATA = "read_data", VEND_CREDENTIALS = "vend_credentials", COMMIT_VERSION = "commit_version",
    COMMIT_VERSION_ANNOUNCED = "commit_version_announced", WRITE_JUDGE = "write_judge",
    EVENT_STREAM_OPENED = "event_stream_opened", LIST_PROJECTS = "list_projects", ACCESS_GRANT = "access_grant",
    ACCESS_REVOKE = "access_revoke", ACCESS_REVIEW = "access_review", ACCESS_SIMULATE = "access_simulate",
    ACCESS_SIMULATE_HYPOTHETICAL = "access_simulate_hypothetical", ACCESS_GRAPH = "access_graph",
    ACCESS_TUPLES_READ = "access_tuples_read", ACCESS_TUPLE_WRITE = "access_tuple_write",
    ACCESS_TUPLE_DELETE = "access_tuple_delete", ACCESS_LIST_OBJECTS = "access_list_objects",
    ACCESS_LIST_USERS = "access_list_users", ACCESS_EXPAND = "access_expand", MANAGED_ACCESS_SET = "managed_access_set",
    PROJECT_MEMBER_GRANT = "project_member_grant", PROJECT_MEMBER_REVOKE = "project_member_revoke",
    PRINCIPAL_DELETE = "principal_delete", ROLE_CREATE = "role_create", ROLE_DELETE = "role_delete",
    ROLE_ASSIGN = "role_assign", ROLE_UNASSIGN = "role_unassign",
    ERASURE_OBLIGATION_RECORDED = "erasure_obligation_recorded", ERASURE_REF_OBLIGED = "erasure_ref_obliged",
    ERASURE_OBLIGATION_VERIFIED = "erasure_obligation_verified", EXPIRE_VERSIONS = "expire_versions",
    DLQ_REPLAY = "dlq_replay", DLQ_PARKED_REPLAY = "dlq_parked_replay", CLONE_ENDING_FINISH = "clone_ending_finish",
    TRASH_RELEASE = "trash_release", TRASH_END = "trash_end", IDEMPOTENCY_KEYS_RECLAIM = "idempotency_keys_reclaim",
    ANNOUNCEMENT_INTENTS_FINISH = "announcement_intents_finish",
}

/// The trail of one service; a trail the chart turned off emits and counts nothing.
pub struct Audit {
    enabled: bool,
    sink: Arc<LogSink>,
    records: Counter<u64>,
}

impl Audit {
    pub(crate) fn new(enabled: bool, sink: Arc<LogSink>, meter: &Meter) -> Audit {
        let records = meter
            .u64_counter("audit.records")
            .with_unit("{record}")
            .with_description("Audit records this process emitted, by action and outcome: compare with the records the store holds.")
            .build();
        Audit { enabled, sink, records }
    }

    /// Make this the process's trail, which [`emit`] records on; a second install keeps the first.
    pub fn install(self) {
        let _ = TRAIL.set(self);
    }

    /// Record `action` on `resource` by `subject` and what it came to; `fields` (a relation, a reason) ride the same
    /// flat record under the `audit.` prefix. Every record names the trace it was made under, `-` outside one.
    pub fn record(&self, action: AuditAction, outcome: AuditOutcome, subject: Option<&str>, resource: Option<&str>, fields: &[(&str, Value)]) {
        if !self.enabled {
            return;
        }
        self.records
            .add(1, &[KeyValue::new("action", action.0), KeyValue::new("outcome", outcome.label())]);
        let text = |value: &str| Value::Str(value.to_owned());
        let mut attributes = vec![
            ("audit.format".to_owned(), text(AUDIT_FORMAT)),
            ("audit.action".to_owned(), text(action.0)),
            ("audit.outcome".to_owned(), text(outcome.label())),
            ("audit.trace_id".to_owned(), text(&trace::current_trace_id().unwrap_or_else(|| "-".to_owned()))),
            ("audit.subject".to_owned(), text(subject.unwrap_or_default())),
            ("audit.resource".to_owned(), text(resource.unwrap_or_default())),
        ];
        attributes.extend(fields.iter().map(|(key, value)| (format!("audit.{key}"), value.clone())));
        self.sink.emit(Level::INFO, AUDIT_SCOPE, "audit", &attributes, true);
    }

    /// Record that `subject` read data: the version served, and the columns returned (`all` where the read named none).
    pub fn read(&self, subject: &str, resource: &str, version: Option<i64>, columns: &[&str], fields: &[(&str, Value)]) {
        let mut all: Vec<(&str, Value)> = version.map(|version| ("version", Value::Int(version))).into_iter().collect();
        let columns = if columns.is_empty() { ALL_COLUMNS.to_owned() } else { columns.join(",") };
        all.push(("columns", Value::Str(columns)));
        all.extend(fields.iter().cloned());
        self.record(AuditAction::READ_DATA, AuditOutcome::Success, Some(subject), Some(resource), &all);
    }
}
