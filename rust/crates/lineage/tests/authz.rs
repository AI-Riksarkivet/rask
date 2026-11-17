//! Lineage's gates ask the questions the Python gates ask and come to the same answers (`rust/vectors/authz.json`, ADR
//! 0217 §5). Each case runs against a decider, a feed and a catalog that answer from the case and record every question.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use lakehouse_core::authz::{AccessDecider, Answer, TableDecision, Unavailable};
use lakehouse_core::events::DatasetUuids;
use lakehouse_core::signing::{KeysUnavailable, Refusal as SignatureRefusal, RefusalReason, VerifiedSignature};
use lineage::authz::gates::{
    Authority, DatasetFilter, Denial, Feed, Names, authorizing, bus_authz, output_authz, require_event_feed_reader, require_metadata_access,
    require_parks_reader, require_parks_replayer,
};
use lineage::authz::names::{Resolution, Unresolved, enforce_author, stamp_resolved_uuids};
use lineage::authz::scope::{Governed, governed_refs, output_refs, relations_for_operation};
use lineage::intake::{Authorizing, Refusal};
use lineage::model::{Dataset, Event, parse_event};
use lineage::signature::{Admission, Signing};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Mutex;

const UNS: &str = "0190a1b2-0000-7000-8000-0000000000ff";

fn vectors(group: &str) -> Vec<Value> {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../vectors/authz.json");
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    all[group].as_array().unwrap().clone()
}

fn setting(name: &str) -> String {
    vectors("settings")[0][name].as_str().unwrap().to_owned()
}

fn texts(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|list| list.iter().map(|text| text.as_str().unwrap().to_owned()).collect())
        .unwrap_or_default()
}

/// A decider answering from a case's `answers`, recording each question as the Python stub does.
struct Stub {
    answers: Value,
    asked: Mutex<Vec<Value>>,
}

impl Stub {
    fn new(answers: &Value) -> Self {
        Stub {
            answers: answers.clone(),
            asked: Mutex::new(Vec::new()),
        }
    }

    fn ask(&self, question: Value) -> Result<(), Unavailable> {
        let method = question["ask"].as_str().unwrap().to_owned();
        self.asked.lock().unwrap().push(question);
        if texts(&self.answers["unavailable"]).contains(&method) {
            return Err(Unavailable::new());
        }
        Ok(())
    }

    fn allowed(&self, relation: &str, object: &str) -> bool {
        texts(&self.answers["allowed"][relation]).iter().any(|allowed| allowed == object)
    }
}

impl AccessDecider for Stub {
    fn resolves_names(&self) -> bool {
        self.answers["resolves_names"].as_bool().unwrap_or(true)
    }

    fn check<'a>(&'a self, user: &'a str, relation: &'a str, object: &'a str) -> Answer<'a, bool> {
        Box::pin(async move {
            self.ask(json!({"ask": "check", "user": user, "relation": relation, "objects": [object]}))?;
            Ok(self.allowed(relation, object))
        })
    }

    fn batch_check<'a>(&'a self, user: &'a str, relation: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move {
            let sorted: BTreeSet<&String> = objects.iter().collect();
            self.ask(json!({"ask": "batch_check", "user": user, "relation": relation, "objects": sorted}))?;
            Ok(objects.iter().map(|object| (object.clone(), self.allowed(relation, object))).collect())
        })
    }

    fn check_tables<'a>(&'a self, user: &'a str, relation: &'a str, tables: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>> {
        Box::pin(async move {
            self.ask(json!({"ask": "check_tables", "user": user, "relation": relation, "tables": tables}))?;
            let decide = |name: &String| match self.answers["tables"].get(name) {
                Some(known) => TableDecision {
                    allowed: true,
                    table_uuid: known["table_uuid"].as_str().map(str::to_owned),
                    namespace_uuid: known["namespace_uuid"].as_str().map(str::to_owned),
                },
                None => TableDecision::default(),
            };
            Ok(tables.iter().map(|name| (name.clone(), decide(name))).collect())
        })
    }

    fn list_users<'a>(&'a self, relation: &'a str, object: &'a str) -> Answer<'a, Vec<String>> {
        Box::pin(async move {
            self.ask(json!({"ask": "list_users", "relation": relation, "objects": [object]}))?;
            Ok(Vec::new())
        })
    }

    fn governed<'a>(&'a self, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move {
            self.ask(json!({"ask": "governed", "objects": objects}))?;
            let governed = texts(&self.answers["governed"]);
            Ok(objects.iter().map(|object| (object.clone(), governed.contains(object))).collect())
        })
    }

    fn locate<'a>(&'a self, locations: &'a [String]) -> Answer<'a, BTreeMap<String, Option<String>>> {
        Box::pin(async move {
            self.ask(json!({"ask": "locate", "locations": locations}))?;
            let held = |location: &String| self.answers["located"].get(location).and_then(Value::as_str).map(str::to_owned);
            Ok(locations.iter().map(|location| (location.clone(), held(location))).collect())
        })
    }
}

/// The feed's lookups, and the catalog's describe door, answering from the case into the decider's record.
struct Lookups<'s> {
    case: Value,
    stub: &'s Stub,
}

impl Feed for Lookups<'_> {
    fn run_author<'a>(&'a self, run_id: &'a str) -> Answer<'a, Option<String>> {
        Box::pin(async move {
            self.stub.asked.lock().unwrap().push(json!({"ask": "run_author", "run_id": run_id}));
            Ok(self.case["run_author"].as_str().map(str::to_owned))
        })
    }

    fn is_recorded<'a>(&'a self, _: &'a Event) -> Answer<'a, bool> {
        Box::pin(async move {
            self.stub.asked.lock().unwrap().push(json!({"ask": "recorded"}));
            Ok(self.case["replay"].as_bool().unwrap_or(false))
        })
    }
}

impl Names for Lookups<'_> {
    fn table<'a>(&'a self, name: &'a str, bearer: Option<&'a str>) -> Resolution<'a, DatasetUuids> {
        Box::pin(async move {
            self.stub.asked.lock().unwrap().push(json!({"ask": "describe", "name": name, "bearer": bearer}));
            let mut refused = self.case["answers"]["refused"].as_array().into_iter().flatten();
            if refused.any(|refused| refused.as_str() == Some(name)) {
                return Err(Unresolved::refused(name));
            }
            let uuid = self.case["answers"]["described"][name].as_str().map(str::to_owned);
            Ok(DatasetUuids {
                namespace_uuid: uuid.as_ref().map(|_| UNS.to_owned()),
                table_uuid: uuid,
            })
        })
    }
}

/// A case's authority over a decider its own, the estate's catalog and root.
struct Estate {
    catalog: String,
    root: String,
}

impl Estate {
    fn new() -> Self {
        Estate {
            catalog: setting("catalog"),
            root: setting("root"),
        }
    }

    fn authority<'a>(&'a self, case: &Value, stub: &'a Stub) -> Authority<'a> {
        Authority {
            enabled: case["fga"].as_bool().unwrap_or(true),
            decider: case["decider"].as_bool().unwrap_or(true).then_some(stub as &dyn AccessDecider),
            catalog: &self.catalog,
            root: &self.root,
        }
    }
}

fn outcome(gated: Result<Option<BTreeSet<String>>, Denial>) -> Value {
    match gated {
        Ok(admitted) => json!({"admitted": admitted}),
        Err(denial) => json!({"refused": denial.kind.name(), "message": denial.message}),
    }
}

/// What the Python said, with a message a client never reads left out: a 5xx's detail is withheld, and a signature
/// refusal's text is the port's own (ADR 0223).
fn comparable(outcome: &Value) -> Value {
    let mut outcome = outcome.clone();
    if matches!(outcome["refused"].as_str(), Some("ServiceUnavailableError" | "UnverifiedEventError")) {
        outcome.as_object_mut().unwrap().remove("message");
    }
    outcome
}

fn report(failures: &[String], cases: usize) {
    assert!(failures.is_empty(), "{} of {cases} cases differ:\n{}", failures.len(), failures.join("\n"));
}

fn compare(failures: &mut Vec<String>, name: &str, ours: Value, theirs: Value) {
    if ours != theirs {
        failures.push(format!("{name}\n    rust:   {ours}\n    python: {theirs}"));
    }
}

#[tokio::test]
async fn the_ingest_door_asks_and_answers_as_python() {
    let estate = Estate::new();
    let cases = vectors("output_authz");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let lookups = Lookups {
            case: case.clone(),
            stub: &stub,
        };
        let event = parse_event(&case["event"]).unwrap();
        let relations = case.get("relations").map(texts).unwrap_or_else(|| vec!["can_write_data".to_owned()]);
        let relations: Vec<&str> = relations.iter().map(String::as_str).collect();
        let attested = case["attested"].as_bool().unwrap_or(false);
        let gated = output_authz(
            &event,
            case["subject"].as_str(),
            &relations,
            attested,
            &estate.authority(case, &stub),
            Some(&lookups),
        )
        .await;
        let ours = json!({"asked": *stub.asked.lock().unwrap(), "outcome": comparable(&outcome(gated.map(Some)))});
        compare(
            &mut failures,
            case["name"].as_str().unwrap(),
            ours,
            json!({"asked": case["asked"], "outcome": comparable(&case["outcome"])}),
        );
    }
    report(&failures, cases.len());
}

fn admission(case: &Value) -> Admission {
    match &case["signature"] {
        Value::String(kind) if kind == "refused" => Admission::Refused(SignatureRefusal {
            reason: RefusalReason::Unsigned,
            message: "the event carries no signature",
        }),
        Value::String(kind) if kind == "unavailable" => Admission::Unavailable(KeysUnavailable { identity: "flows".to_owned() }),
        Value::Object(verified) => Admission::Verified(VerifiedSignature {
            identity: verified["verified"].as_str().unwrap().to_owned(),
            kid: "k1".to_owned(),
            on_behalf_of: verified.get("on_behalf_of").and_then(Value::as_str).map(str::to_owned),
        }),
        _ => Admission::Unenforced,
    }
}

#[tokio::test]
async fn the_bus_route_judges_whose_authority_as_python() {
    let estate = Estate::new();
    let cases = vectors("bus_authz");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let lookups = Lookups {
            case: case.clone(),
            stub: &stub,
        };
        let event = parse_event(&case["event"]).unwrap();
        let signing = Signing {
            signers: texts(&case["signers"]).into_iter().collect(),
            delegators: texts(&case["delegators"]).into_iter().collect(),
        };
        let gated = bus_authz(&event, &admission(case), &signing, &estate.authority(case, &stub), Some(&lookups)).await;
        // The route's answer follows the class, as the delivery vectors pin it.
        let expected = match case["outcome"]["refused"].as_str() {
            None => Authorizing::Allowed(texts(&case["outcome"]["admitted"])),
            Some("PermissionDeniedError") => Authorizing::Refused(Refusal::Denied),
            Some("UnverifiedEventError") => Authorizing::Refused(Refusal::Unverified),
            Some("UnauthoredRunError") => Authorizing::Refused(Refusal::Unauthored),
            Some("ForeignRunError") => Authorizing::Refused(Refusal::ForeignRun),
            Some("UngovernedOutputError") => Authorizing::Refused(Refusal::Ungoverned),
            Some(_) => Authorizing::Unavailable(String::new()),
        };
        let routed = match authorizing(gated.clone()) {
            Authorizing::Unavailable(_) => Authorizing::Unavailable(String::new()),
            routed => routed,
        };
        if routed != expected {
            failures.push(format!("{}: the route answers {routed:?}, not {expected:?}", case["name"]));
        }
        let ours = json!({"asked": *stub.asked.lock().unwrap(), "outcome": comparable(&outcome(gated.map(Some)))});
        compare(
            &mut failures,
            case["name"].as_str().unwrap(),
            ours,
            json!({"asked": case["asked"], "outcome": comparable(&case["outcome"])}),
        );
    }
    report(&failures, cases.len());
}

#[tokio::test]
async fn the_read_gates_ask_and_answer_as_python() {
    let estate = Estate::new();
    let cases = vectors("gates");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let lookups = Lookups {
            case: case.clone(),
            stub: &stub,
        };
        let authority = estate.authority(case, &stub);
        let subject = case["subject"].as_str();
        let gated = match case["gate"].as_str().unwrap() {
            "event_feed_reader" => require_event_feed_reader(&authority, subject).await,
            "parks_reader" => require_parks_reader(&authority, subject).await,
            "parks_replayer" => require_parks_replayer(&authority, subject).await,
            _ => require_metadata_access(case["table"].as_str().unwrap(), &authority, subject, &lookups, case["bearer"].as_str()).await,
        };
        let ours = json!({"asked": *stub.asked.lock().unwrap(), "outcome": comparable(&outcome(gated.map(|()| None)))});
        let name = format!("{} {}", case["gate"], case["name"]);
        compare(
            &mut failures,
            &name,
            ours,
            json!({"asked": case["asked"], "outcome": comparable(&case["outcome"])}),
        );
    }
    report(&failures, cases.len());
}

fn governed_of(item: &Value) -> Governed {
    Governed {
        objects: texts(&item["objects"]).into_iter().collect(),
        unresolved: texts(&item["unresolved"]).into_iter().collect(),
    }
}

#[tokio::test]
async fn a_page_is_filtered_as_python_filters_it() {
    let estate = Estate::new();
    let cases = vectors("filter");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let mut filter = DatasetFilter::new(estate.authority(case, &stub), case["subject"].as_str());
        let mut pages = Vec::new();
        for items in case["pages"].as_array().unwrap() {
            let refs: Vec<Governed> = items.as_array().unwrap().iter().map(governed_of).collect();
            let undecided = filter.undecided(refs.iter().flat_map(|governs| governs.objects.iter()));
            match filter.governed((0..refs.len()).collect(), |index| refs[*index].clone()).await {
                Ok(kept) => pages.push(json!({"undecided": undecided, "kept": kept})),
                Err(denial) => {
                    pages.push(json!({"undecided": undecided, "refused": denial.kind.name(), "message": denial.message}));
                    break;
                }
            }
        }
        let theirs: Vec<Value> = case["outcome"].as_array().unwrap().iter().map(comparable).collect();
        let ours: Vec<Value> = pages.iter().map(comparable).collect();
        let ours = json!({"asked": *stub.asked.lock().unwrap(), "outcome": ours});
        compare(
            &mut failures,
            case["name"].as_str().unwrap(),
            ours,
            json!({"asked": case["asked"], "outcome": theirs}),
        );
    }
    report(&failures, cases.len());
}

#[tokio::test]
async fn a_staged_event_is_scoped_as_python_scopes_it() {
    let estate = Estate::new();
    let cases = vectors("scope_unrecorded");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let filter = DatasetFilter::new(estate.authority(case, &stub), case["subject"].as_str());
        let events: Vec<Event> = case["events"].as_array().unwrap().iter().map(|event| parse_event(event).unwrap()).collect();
        let ours = match filter.scope_unrecorded(&events).await {
            Ok(scoped) => {
                json!({"scoped": scoped.iter().map(|governs| json!({"objects": governs.objects, "unresolved": governs.unresolved})).collect::<Vec<_>>()})
            }
            Err(denial) => json!({"refused": denial.kind.name(), "message": denial.message}),
        };
        let ours = json!({"asked": *stub.asked.lock().unwrap(), "outcome": comparable(&ours)});
        compare(
            &mut failures,
            case["name"].as_str().unwrap(),
            ours,
            json!({"asked": case["asked"], "outcome": comparable(&case["outcome"])}),
        );
    }
    report(&failures, cases.len());
}

#[tokio::test]
async fn names_are_resolved_and_stamped_as_python_stamps_them() {
    let estate = Estate::new();
    let cases = vectors("stamp");
    let mut failures = Vec::new();
    for case in &cases {
        let stub = Stub::new(&case["answers"]);
        let lookups = Lookups {
            case: case.clone(),
            stub: &stub,
        };
        let mut event = parse_event(&case["event"]).unwrap();
        let decider = case["decider"].as_bool().unwrap_or(true).then_some(&stub as &dyn AccessDecider);
        let stamped = stamp_resolved_uuids(&mut event, &lookups, Some("bearer-of-ada"), decider, case["subject"].as_str(), &estate.catalog).await;
        let asked = stub.asked.lock().unwrap().clone();
        let (ours, python) = match stamped {
            Ok(()) => (
                json!({"asked": asked, "stamped": event.to_json()}),
                json!({"asked": case["asked"], "stamped": case["stamped"]}),
            ),
            Err(denial) => (
                json!({"asked": asked, "refused": denial.kind.name(), "message": denial.message}),
                json!({"asked": case["asked"], "refused": case["refused"], "message": case["message"]}),
            ),
        };
        compare(&mut failures, case["name"].as_str().unwrap(), ours, python);
    }
    report(&failures, cases.len());
}

#[test]
fn the_author_is_the_verified_caller_as_in_python() {
    for case in vectors("author") {
        let mut event = parse_event(&case["event"]).unwrap();
        enforce_author(&mut event, case["subject"].as_str());
        assert_eq!(event.to_json(), case["stamped"].as_str().unwrap(), "{}", case["event"]);
    }
}

#[test]
fn what_datasets_govern_is_what_python_says() {
    let catalog = setting("catalog");
    for case in vectors("refs") {
        let datasets: Vec<Dataset> = serde_json::from_value(case["datasets"].clone()).unwrap();
        let upstreams: BTreeSet<String> = texts(&case["column_upstreams"]).into_iter().collect();
        let named = governed_refs(&datasets, &upstreams, &catalog);
        let written = output_refs(&datasets, &catalog);
        assert_eq!(
            json!({"objects": named.objects, "unresolved": named.unresolved}),
            case["governed"],
            "{}",
            case["datasets"]
        );
        assert_eq!(
            json!({"objects": written.objects, "unresolved": written.unresolved}),
            case["outputs"],
            "{}",
            case["datasets"]
        );
    }
}

#[test]
fn the_rung_follows_the_operation_as_in_python() {
    for case in vectors("relations") {
        let relations = relations_for_operation(case["operation"].as_str());
        assert_eq!(json!(relations), case["relations"], "{}", case["operation"]);
    }
}
