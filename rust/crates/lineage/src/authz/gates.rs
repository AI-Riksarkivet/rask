//! The gates: thin shells that ask the decider the questions `super::scope` decides on, one per door and route.
//!
//! Every gate fails closed in one order: authorization off admits; no decider is an outage; no caller is a 401; a
//! refusal names what was refused by the caller's own names; an authority that does not answer is an outage, never an
//! allow and never a deny.

use super::names::Resolution;
use super::scope::{
    Aliases, Governed, READ, WRITE_RELATIONS, alias_questions, governed_refs, named_by, names_of, output_refs, relations_for_operation, scoped,
    upstreams_named_as_tables, upstreams_named_by_a_location,
};
use crate::intake::{Authorizing, Refusal};
use crate::model::{Dataset, Event, author_sub_from_payload};
use crate::signature::{Admission, Signing, attested};
use lakehouse_core::auth::principal::Principal;
use lakehouse_core::authz::{AccessDecider, Answer, Unavailable, is_object_id, table_object};
use lakehouse_core::events::{DatasetUuids, LANCE_FACET};
use lakehouse_core::service::problem::{ErrorCode, NsError};
use lakehouse_core::telemetry::audit::{self, AuditAction, AuditOutcome};
use lakehouse_core::telemetry::logging::Value as Field;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

/// The class of a refusal, named as the Python door names it to a client.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Unavailable,
    Unauthenticated,
    PermissionDenied,
    /// The event extends a run another subject founded.
    ForeignRun,
    /// An output names no object a grant could be written on.
    Ungoverned,
    /// A bus-delivered event names no principal as its author.
    Unauthored,
    /// No listed signer's key verifies the event.
    Unverified,
    /// The catalog refused a name the caller gave as none it can resolve (`super::names::Unresolved::NoName`).
    InvalidInput,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Unavailable => "ServiceUnavailableError",
            Kind::Unauthenticated => "UnauthenticatedError",
            Kind::PermissionDenied => "PermissionDeniedError",
            Kind::ForeignRun => "ForeignRunError",
            Kind::Ungoverned => "UngovernedOutputError",
            Kind::Unauthored => "UnauthoredRunError",
            Kind::Unverified => "UnverifiedEventError",
            Kind::InvalidInput => "InvalidInputError",
        }
    }

    /// Whether a redelivery of an event the feed already holds is admitted instead: every class a grant or a stamp decides.
    fn is_a_permission(self) -> bool {
        !matches!(self, Kind::Unavailable | Kind::Unauthenticated | Kind::InvalidInput)
    }
}

/// Why a gate refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Denial {
    pub kind: Kind,
    pub message: String,
}

impl Denial {
    fn new(kind: Kind, message: impl Into<String>) -> Self {
        Denial { kind, message: message.into() }
    }

    fn not_wired() -> Self {
        Denial::new(Kind::Unavailable, "authorization service is not available")
    }

    fn unauthenticated() -> Self {
        Denial::new(Kind::Unauthenticated, "authentication required")
    }

    /// The refusal an HTTP door answers with.
    pub fn ns_error(&self) -> NsError {
        let code = match self.kind {
            Kind::Unavailable => ErrorCode::ServiceUnavailable,
            Kind::Unauthenticated => ErrorCode::Unauthenticated,
            Kind::InvalidInput => ErrorCode::InvalidInput,
            _ => ErrorCode::PermissionDenied,
        };
        NsError::new(code, self.message.clone()).named(self.kind.name())
    }

    /// What the bus and outbox routes do with the refusal: an outage, or a missing caller nothing on the bus has, is
    /// asked about again. Those routes resolve no name by describing it, so meet no refused name; one would be a verdict
    /// on the event that no grant repairs, as an output naming no table is.
    pub fn refusal(&self) -> Result<Refusal, Unavailable> {
        match self.kind {
            Kind::PermissionDenied => Ok(Refusal::Denied),
            Kind::ForeignRun => Ok(Refusal::ForeignRun),
            Kind::Ungoverned | Kind::InvalidInput => Ok(Refusal::Ungoverned),
            Kind::Unauthored => Ok(Refusal::Unauthored),
            Kind::Unverified => Ok(Refusal::Unverified),
            Kind::Unavailable | Kind::Unauthenticated => Err(Unavailable(self.message.clone())),
        }
    }
}

impl From<Unavailable> for Denial {
    fn from(unavailable: Unavailable) -> Self {
        Denial::new(Kind::Unavailable, unavailable.0)
    }
}

/// What the bus and outbox routes answer, from what the gate came to.
pub fn authorizing(gated: Result<BTreeSet<String>, Denial>) -> Authorizing {
    match gated {
        Ok(aliased) => Authorizing::Allowed(aliased.into_iter().collect()),
        Err(denial) => match denial.refusal() {
            Ok(refusal) => Authorizing::Refused(refusal),
            Err(Unavailable(error)) => Authorizing::Unavailable(error),
        },
    }
}

/// Who decides, and over which estate.
#[derive(Clone, Copy)]
pub struct Authority<'a> {
    /// Whether OpenFGA is consulted at all (`RASK_FGA_ENABLED`).
    pub enabled: bool,
    pub decider: Option<&'a dyn AccessDecider>,
    /// The dataset namespace this estate's catalog names its tables under.
    pub catalog: &'a str,
    /// The object every estate privilege is checked on.
    pub root: &'a str,
}

impl<'a> Authority<'a> {
    /// The decider and the subject it is asked as, where both are present.
    fn asking<'s>(&self, subject: Option<&'s str>) -> Result<(&'a dyn AccessDecider, &'s str), Denial> {
        let decider = self.decider.ok_or_else(Denial::not_wired)?;
        Ok((decider, subject.ok_or_else(Denial::unauthenticated)?))
    }
}

/// The feed's two lookups a gate makes.
pub trait Feed: Send + Sync {
    /// The subject whose event founded the run, where the feed holds it.
    fn run_author<'a>(&'a self, run_id: &'a str) -> Answer<'a, Option<String>>;
    /// Whether the feed already holds this very event, byte for byte as it stores it.
    fn is_recorded<'a>(&'a self, event: &'a Event) -> Answer<'a, bool>;
}

/// Where a table's name is resolved to its uuids as the caller.
pub trait Names: Send + Sync {
    fn table<'a>(&'a self, name: &'a str, bearer: Option<&'a str>) -> Resolution<'a, DatasetUuids>;
}

/// A refusal: who was refused on the audit trail, one record per object as the catalog's doors write them, then a line.
fn denied(event: &str, subject: &str, relation: &str, resources: &[String]) {
    for resource in resources {
        let relation = [("relation", Field::Str(relation.to_owned()))];
        audit::emit(AuditAction::AUTHZ, AuditOutcome::Deny, Some(subject), Some(resource), &relation);
    }
    tracing::info!(relation, objects = ?resources, "{event}");
}

async fn require_relation(relation: &str, object: Option<String>, label: &str, authority: &Authority<'_>, subject: Option<&str>) -> Result<(), Denial> {
    if !authority.enabled {
        return Ok(());
    }
    let (decider, subject) = authority.asking(subject)?;
    let allowed = match &object {
        Some(object) => decider.check(subject, relation, object).await?,
        None => false,
    };
    if !allowed {
        denied("access_denied", subject, relation, &[object.unwrap_or_else(|| label.to_owned())]);
        return Err(Denial::new(Kind::PermissionDenied, format!("{relation} required on {label}")));
    }
    Ok(())
}

/// An estate privilege, checked on the estate's root object: it is no relation of any one table.
pub async fn require_estate_rung(relation: &str, authority: &Authority<'_>, subject: Option<&str>) -> Result<(), Denial> {
    require_relation(relation, Some(authority.root.to_owned()), authority.root, authority, subject).await
}

/// The unfiltered run feed, on its own rung so the reader of it holds nothing that mints a tenant.
pub async fn require_event_feed_reader(authority: &Authority<'_>, subject: Option<&str>) -> Result<(), Denial> {
    require_estate_rung("can_read_event_feed", authority, subject).await
}

/// The kept parks are every tenant's refused deliveries.
pub async fn require_parks_reader(authority: &Authority<'_>, subject: Option<&str>) -> Result<(), Denial> {
    require_estate_rung("can_read_parked_events", authority, subject).await
}

/// A replay writes to the feed for whoever the event's author is, so it is a verb of its own.
pub async fn require_parks_replayer(authority: &Authority<'_>, subject: Option<&str>) -> Result<(), Denial> {
    require_estate_rung("can_replay_parked_events", authority, subject).await
}

/// A table's reads, on the rung the catalog's DescribeTable asks, of the table `name` names now for the caller. A name
/// the caller may not see resolves to nothing and is refused alike.
pub async fn require_metadata_access(
    name: &str,
    authority: &Authority<'_>,
    subject: Option<&str>,
    names: &dyn Names,
    bearer: Option<&str>,
) -> Result<(), Denial> {
    if !authority.enabled {
        return Ok(());
    }
    let uuid = names.table(name, bearer).await?.table_uuid;
    require_relation(READ, uuid.as_deref().map(table_object), &format!("table:{name}"), authority, subject).await
}

async fn resolve_aliases(decider: &dyn AccessDecider, datasets: &[Dataset], user: &str, catalog: &str) -> Result<Aliases, Unavailable> {
    let questions = alias_questions(datasets, catalog);
    let located = match questions.location_list() {
        locations if locations.is_empty() => BTreeMap::new(),
        locations => decider.locate(&locations).await?,
    };
    let decided = match questions.name_list() {
        names if names.is_empty() => BTreeMap::new(),
        names => decider.check_tables(user, READ, &names).await?,
    };
    Ok(questions.answered(&located, &decided))
}

/// The objects the subject may record against under none of `relations`, asked a relation at a time.
async fn denied_objects(decider: &dyn AccessDecider, user: &str, relations: &[&str], objects: &BTreeSet<String>) -> Result<BTreeSet<String>, Unavailable> {
    let mut remaining: Vec<String> = objects.iter().cloned().collect();
    for relation in relations {
        if remaining.is_empty() {
            break;
        }
        let allowed = decider.batch_check(user, relation, &remaining).await?;
        remaining.retain(|object| !allowed.get(object).copied().unwrap_or(false));
    }
    Ok(remaining.into_iter().collect())
}

/// Whether every denied object carries no tuple, so no grant could repair the refusal. An unreadable store answers no:
/// parking a repairable event costs a kept copy, while taking one deletes provenance.
async fn none_are_governed(decider: &dyn AccessDecider, objects: &BTreeSet<String>) -> bool {
    let asked: Vec<String> = objects.iter().cloned().collect();
    match decider.governed(&asked).await {
        Ok(governed) => !governed.values().any(|governed| *governed),
        Err(error) => {
            tracing::warn!(error = error.0, "ingest_governance_probe_unreadable");
            false
        }
    }
}

fn operation(event: &Event) -> Option<&str> {
    let facets = match event {
        Event::Run(run) => &run.run.facets,
        Event::Dataset(dataset) => &dataset.dataset.facets,
    };
    facets.get(LANCE_FACET)?.get("operation")?.as_str()
}

fn run_id(event: &Event) -> Option<&str> {
    match event {
        Event::Run(run) => Some(run.run.run_id.as_str()).filter(|id| !id.is_empty()),
        Event::Dataset(_) => None,
    }
}

/// May the writer record what `event` says it wrote and read: every output on one of `relations`, every governed input on
/// the reading rung. An event the catalog `attested` has neither re-derived, since the catalog checked both before it
/// committed; an output or input naming no uuid is refused all the same. Returns the uuids of the tables the event's
/// datasets alias, which its feed row is scoped by.
pub async fn output_authz(
    event: &Event,
    subject: Option<&str>,
    relations: &[&str],
    attested: bool,
    authority: &Authority<'_>,
    feed: Option<&dyn Feed>,
) -> Result<BTreeSet<String>, Denial> {
    if !authority.enabled {
        return Ok(BTreeSet::new());
    }
    let (decider, subject) = authority.asking(subject)?;
    if let (Some(feed), Some(run_id)) = (feed, run_id(event))
        && feed.run_author(run_id).await?.is_some_and(|author| author != subject)
    {
        denied("ingest_run_mutation_denied", subject, "author", &[format!("run:{run_id}")]);
        return Err(Denial::new(
            Kind::ForeignRun,
            format!("run {run_id} was founded by another subject; only its author may extend it"),
        ));
    }
    let catalog = authority.catalog;
    let (inputs, outputs) = (event.inputs(), event.outputs());
    let sources: Vec<Dataset> = inputs.iter().cloned().chain(upstreams_named_by_a_location(outputs, catalog)).collect();
    let (written, read_as) = if attested {
        (Aliases::default(), Aliases::default())
    } else {
        let written = resolve_aliases(decider, outputs, subject, catalog).await?;
        (written, resolve_aliases(decider, &sources, subject, catalog).await?)
    };
    let unnamed = outputs.iter().enumerate().filter(|(index, _)| !written.located.contains(index));
    let unnamed = unnamed.flat_map(|(_, output)| output_refs([output], catalog).unresolved);
    let written_refs = Governed {
        objects: output_refs(outputs, catalog).objects,
        unresolved: unnamed.collect(),
    }
    .joined(written.all_objects(), written.hidden_names());
    let joined = relations.join("|");
    if !written_refs.unresolved.is_empty() {
        let names: Vec<String> = written_refs.unresolved.into_iter().collect();
        denied("ingest_denied", subject, &joined, &names);
        return Err(Denial::new(
            Kind::Ungoverned,
            format!("{} required on outputs: {}", relations.join(" or "), names.join(", ")),
        ));
    }
    if !written_refs.objects.is_empty() && !attested {
        let refused = denied_objects(decider, subject, relations, &written_refs.objects).await?;
        if !refused.is_empty() {
            denied("ingest_denied", subject, &joined, &refused.iter().cloned().collect::<Vec<_>>());
            let names: Vec<String> = names_of(outputs, &refused, &written).into_iter().collect();
            let reason = format!("{} required on outputs: {}", relations.join(" or "), names.join(", "));
            let kind = if none_are_governed(decider, &refused).await {
                Kind::Ungoverned
            } else {
                Kind::PermissionDenied
            };
            return Err(Denial::new(kind, reason));
        }
    }
    let read = governed_refs(inputs, &upstreams_named_as_tables(outputs, catalog), catalog).joined(read_as.all_objects(), read_as.hidden_names());
    let mut hidden = read.unresolved.clone();
    if !read.objects.is_empty() && !attested {
        let asked: Vec<String> = read.objects.iter().cloned().collect();
        let seen = decider.batch_check(subject, READ, &asked).await?;
        let unseen: BTreeSet<String> = asked.into_iter().filter(|object| !seen.get(object).copied().unwrap_or(false)).collect();
        hidden.extend(names_of(&sources, &unseen, &read_as));
    }
    if !hidden.is_empty() {
        let names: Vec<String> = hidden.into_iter().collect();
        denied("ingest_input_denied", subject, READ, &names);
        return Err(Denial::new(Kind::PermissionDenied, format!("{READ} required on inputs: {}", names.join(", "))));
    }
    Ok(written.tables.union(&read_as.tables).cloned().collect())
}

/// The HTTP door's ingest gate: the caller writes as itself, on the data writer's rung.
pub async fn ingest_authz(event: &Event, subject: Option<&str>, authority: &Authority<'_>, feed: Option<&dyn Feed>) -> Result<BTreeSet<String>, Denial> {
    output_authz(event, subject, WRITE_RELATIONS, false, authority, feed).await
}

/// A text as Python's `repr` writes it, for a refusal that quotes what an event claimed.
pub(crate) fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut written = String::from(quote);
    for character in text.chars() {
        match character {
            '\\' => written.push_str("\\\\"),
            '\t' => written.push_str("\\t"),
            '\n' => written.push_str("\\n"),
            '\r' => written.push_str("\\r"),
            _ if character == quote => {
                written.push('\\');
                written.push(quote);
            }
            '\0'..='\x1f' | '\x7f'..='\u{a0}' | '\u{ad}' => written.push_str(&format!("\\x{:02x}", u32::from(character))),
            _ => written.push(character),
        }
    }
    written.push(quote);
    written
}

/// The subject the producer stamped on the event, as its payload carries it.
fn stamped_author(event: &Event) -> Option<String> {
    let payload = match event {
        Event::Run(run) => serde_json::to_value(run),
        Event::Dataset(dataset) => serde_json::to_value(dataset),
    };
    author_sub_from_payload(&payload.unwrap_or(Value::Null)).map(str::to_owned)
}

fn is_a_drop(event: &Event) -> bool {
    matches!(event, Event::Dataset(_))
        && operation(event).is_some_and(|operation| matches!(operation.to_lowercase().as_str(), "drop_table" | "deregister_table"))
}

/// The bus and outbox routes' gate, as the subject the producer stamped. The signature is judged first and on its own:
/// it authenticates the producer whether or not OpenFGA is consulted. A drop the catalog attests is admitted on that
/// signature, since the catalog revokes the table's grants in the request that emits it; any other event's stamped
/// author must hold the rung on every output. A refusal of an event the feed already holds is a redelivery, admitted.
pub async fn bus_authz(
    event: &Event,
    admission: &Admission,
    signing: &Signing,
    authority: &Authority<'_>,
    feed: Option<&dyn Feed>,
) -> Result<BTreeSet<String>, Denial> {
    match admission {
        Admission::Refused(refusal) => return Err(Denial::new(Kind::Unverified, refusal.message)),
        Admission::Unavailable(_) => return Err(Denial::new(Kind::Unavailable, "the signer's published keys could not be read")),
        Admission::Unenforced | Admission::Verified(_) => {}
    }
    let attested = attested(admission, signing);
    if attested && is_a_drop(event) {
        tracing::info!("lineage_catalog_drop_admitted");
        return Ok(BTreeSet::new());
    }
    if !authority.enabled {
        return Ok(BTreeSet::new());
    }
    let gated = match stamped_author(event) {
        None => Err(Denial::new(
            Kind::Unauthored,
            "a bus-delivered run must carry a verified author sub to be authorized",
        )),
        Some(subject) if Principal::parse(&subject).is_err() => {
            let claimed: String = subject.chars().take(128).collect();
            let message = format!("a bus-delivered run's author {} is not a principal (<idp-id>~<subject>)", py_repr(&claimed));
            Err(Denial::new(Kind::Unauthored, message))
        }
        Some(subject) => output_authz(event, Some(&subject), relations_for_operation(operation(event)), attested, authority, feed).await,
    };
    match gated {
        Err(denial) if denial.kind.is_a_permission() && is_replay(event, feed).await? => {
            tracing::info!("lineage_replay_not_reauthorized");
            Ok(BTreeSet::new())
        }
        gated => gated,
    }
}

async fn is_replay(event: &Event, feed: Option<&dyn Feed>) -> Result<bool, Unavailable> {
    let keyed = match event {
        Event::Run(run) => !run.run.run_id.is_empty(),
        Event::Dataset(_) => true,
    };
    match feed {
        Some(feed) if keyed => feed.is_recorded(event).await,
        _ => Ok(false),
    }
}

/// Drops from a reader's pages what the reader may not see, asking about each object once per request.
pub struct DatasetFilter<'a> {
    authority: Authority<'a>,
    subject: Option<&'a str>,
    decided: BTreeMap<String, bool>,
}

impl<'a> DatasetFilter<'a> {
    pub fn new(authority: Authority<'a>, subject: Option<&'a str>) -> Self {
        DatasetFilter {
            authority,
            subject,
            decided: BTreeMap::new(),
        }
    }

    /// How many of `objects` [`DatasetFilter::visible`] would ask about: none with authorization off, and none that is
    /// no object id, which the check answers without asking.
    pub fn undecided<'o>(&self, objects: impl IntoIterator<Item = &'o String>) -> usize {
        if !self.authority.enabled {
            return 0;
        }
        let undecided: BTreeSet<&String> = objects
            .into_iter()
            .filter(|object| !self.decided.contains_key(*object) && is_object_id(object))
            .collect();
        undecided.len()
    }

    /// The objects among `objects` the reader may see.
    pub async fn visible(&mut self, objects: &BTreeSet<String>) -> Result<BTreeSet<String>, Denial> {
        if !self.authority.enabled || objects.is_empty() {
            return Ok(objects.clone());
        }
        let undecided: Vec<String> = objects.iter().filter(|object| !self.decided.contains_key(*object)).cloned().collect();
        if !undecided.is_empty() {
            let (decider, subject) = self.authority.asking(self.subject)?;
            let allowed = decider.batch_check(subject, READ, &undecided).await?;
            for object in undecided {
                let seen = allowed.get(&object).copied().unwrap_or(false);
                self.decided.insert(object, seen);
            }
        }
        Ok(objects
            .iter()
            .filter(|object| self.decided.get(*object).copied().unwrap_or(false))
            .cloned()
            .collect())
    }

    /// The items the reader may see: none naming a table it may not read, none naming a table without a uuid, and none
    /// naming no table at all, which would otherwise pass for want of anything to check.
    pub async fn governed<T>(&mut self, items: Vec<T>, refs: impl Fn(&T) -> Governed) -> Result<Vec<T>, Denial> {
        if !self.authority.enabled {
            return Ok(items);
        }
        let named: Vec<(T, Governed)> = items
            .into_iter()
            .map(|item| (refs(&item), item))
            .map(|(governs, item)| (item, governs))
            .collect();
        let visible = self
            .visible(&named.iter().flat_map(|(_, governs)| governs.objects.iter().cloned()).collect())
            .await?;
        let shown = |governs: &Governed| !governs.names_nothing() && governs.unresolved.is_empty() && governs.objects.is_subset(&visible);
        Ok(named.into_iter().filter(|(_, governs)| shown(governs)).map(|(item, _)| item).collect())
    }

    /// What each staged event names for the reader, under every identifier it uses, asked in one question about every
    /// location and one about every symlinked name.
    pub async fn scope_unrecorded(&self, events: &[Event]) -> Result<Vec<Governed>, Denial> {
        let catalog = self.authority.catalog;
        if !self.authority.enabled {
            let refs = events
                .iter()
                .map(|event| governed_refs(event.inputs().iter().chain(event.outputs()), &BTreeSet::new(), catalog));
            return Ok(refs.collect());
        }
        let (decider, subject) = self.authority.asking(self.subject)?;
        let datasets: Vec<(&[Dataset], &[Dataset])> = events.iter().map(|event| (event.inputs(), event.outputs())).collect();
        let aliases = resolve_aliases(decider, &named_by(&datasets, catalog), subject, catalog).await?;
        Ok(scoped(&datasets, &aliases, catalog))
    }
}
