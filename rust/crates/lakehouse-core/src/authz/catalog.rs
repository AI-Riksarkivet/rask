//! The catalog's decision door, asked as the service account this pod's projected token names.
//!
//! The catalog resolves a table's name to the table it names now and decides on its uuid, so only this decider answers
//! a table by name or a storage location by the table it holds.

use super::{AccessDecider, Answer, MAX_OBJECTS_PER_QUESTION, TableDecision, Unavailable, bearer, chunks, complete, read_token};
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE};
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

/// Where the catalog serves the decision door.
pub const DECISIONS_PREFIX: &str = "/management/v1/decisions";
/// How often a question is sent when the catalog could not be reached at all. A request the catalog received is never
/// sent again: it may already be retrying OpenFGA, and a second copy would multiply that load.
const CONNECT_ATTEMPTS: u32 = 3;
const CONNECT_BACKOFF: Duration = Duration::from_millis(200);

pub struct CatalogDecider {
    base: String,
    /// The pod's projected token for the catalog, read on every question; `None` sends no bearer.
    token_file: Option<PathBuf>,
    client: reqwest::Client,
}

#[derive(Deserialize)]
struct CheckAnswer {
    #[serde(default)]
    allowed: BTreeMap<String, bool>,
    #[serde(default)]
    tables: BTreeMap<String, TableDecision>,
}

#[derive(Deserialize)]
struct UsersAnswer {
    users: Vec<String>,
}

#[derive(Deserialize)]
struct GovernedAnswer {
    governed: BTreeMap<String, bool>,
}

#[derive(Deserialize)]
struct LocateAnswer {
    located: BTreeMap<String, Option<String>>,
}

impl CatalogDecider {
    pub fn new(catalog_url: &str, token_file: Option<PathBuf>, timeout: Duration) -> Result<Self, String> {
        if catalog_url.is_empty() {
            return Err("a catalog decider needs the catalog's URL".to_owned());
        }
        // No redirect is followed, so the service's bearer reaches no host but the catalog.
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|error| format!("the HTTP client for the catalog cannot be built: {error}"))?;
        Ok(CatalogDecider {
            base: format!("{}{DECISIONS_PREFIX}", catalog_url.trim_end_matches('/')),
            token_file,
            client,
        })
    }

    async fn post(&self, route: &str, question: &Value) -> Result<reqwest::Response, Unavailable> {
        let mut attempt = 1;
        loop {
            let mut request = self.client.post(format!("{}{route}", self.base)).header(CONTENT_TYPE, "application/json");
            if let Some(path) = &self.token_file {
                request = request.header(AUTHORIZATION, bearer(&read_token(path)?)?);
            }
            match request.body(question.to_string()).send().await {
                Err(error) if error.is_connect() && attempt < CONNECT_ATTEMPTS => {
                    tokio::time::sleep(CONNECT_BACKOFF * 2u32.pow(attempt - 1)).await;
                    attempt += 1;
                }
                Err(error) => {
                    tracing::error!(route, error = %error.without_url(), "decision_unreachable");
                    return Err(Unavailable::new());
                }
                Ok(response) => return Ok(response),
            }
        }
    }

    /// One question and its answer; anything but a well-formed 200 is an outage of every gate, never a refusal of the
    /// person behind the request: a 401 or 403 means this service may not ask, which is a deployment fault.
    async fn ask<R: DeserializeOwned>(&self, route: &str, question: Value) -> Result<R, Unavailable> {
        let response = self.post(route, &question).await?;
        let status = response.status().as_u16();
        let body = response.bytes().await.map_err(|_| Unavailable::new())?;
        if status != 200 {
            let excerpt: String = String::from_utf8_lossy(&body).chars().take(512).collect();
            tracing::error!(route, status, body = excerpt, "decision_failed");
            return Err(Unavailable::new());
        }
        serde_json::from_slice(&body).map_err(|_| {
            tracing::error!(route, "decision_answer_malformed");
            Unavailable::new()
        })
    }

    async fn checked(&self, user: &str, relation: &str, objects: &[String]) -> Result<BTreeMap<String, bool>, Unavailable> {
        let mut allowed = BTreeMap::new();
        for chunk in chunks(objects, MAX_OBJECTS_PER_QUESTION) {
            let question = json!({"user": user, "relation": relation, "objects": chunk, "tables": []});
            let answer: CheckAnswer = self.ask("/check", question).await?;
            allowed.extend(complete(answer.allowed, &chunk)?);
        }
        Ok(allowed)
    }
}

impl AccessDecider for CatalogDecider {
    fn resolves_names(&self) -> bool {
        true
    }

    fn check<'a>(&'a self, user: &'a str, relation: &'a str, object: &'a str) -> Answer<'a, bool> {
        Box::pin(async move { Ok(self.checked(user, relation, &[object.to_owned()]).await?.get(object).copied().unwrap_or(false)) })
    }

    fn batch_check<'a>(&'a self, user: &'a str, relation: &'a str, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(self.checked(user, relation, objects))
    }

    fn check_tables<'a>(&'a self, user: &'a str, relation: &'a str, tables: &'a [String]) -> Answer<'a, BTreeMap<String, TableDecision>> {
        Box::pin(async move {
            let mut decided = BTreeMap::new();
            for chunk in chunks(tables, MAX_OBJECTS_PER_QUESTION) {
                let question = json!({"user": user, "relation": relation, "objects": [], "tables": chunk});
                let answer: CheckAnswer = self.ask("/check", question).await?;
                if !answer.tables.values().all(TableDecision::is_whole) {
                    tracing::error!("decision_answer_malformed");
                    return Err(Unavailable::new());
                }
                decided.extend(complete(answer.tables, &chunk)?);
            }
            Ok(decided)
        })
    }

    fn list_users<'a>(&'a self, relation: &'a str, object: &'a str) -> Answer<'a, Vec<String>> {
        Box::pin(async move {
            let answer: UsersAnswer = self.ask("/list-users", json!({"relation": relation, "object": object})).await?;
            Ok(answer.users)
        })
    }

    fn governed<'a>(&'a self, objects: &'a [String]) -> Answer<'a, BTreeMap<String, bool>> {
        Box::pin(async move {
            let mut governed = BTreeMap::new();
            for chunk in chunks(objects, MAX_OBJECTS_PER_QUESTION) {
                let answer: GovernedAnswer = self.ask("/governed", json!({"objects": chunk})).await?;
                governed.extend(complete(answer.governed, &chunk)?);
            }
            Ok(governed)
        })
    }

    fn locate<'a>(&'a self, locations: &'a [String]) -> Answer<'a, BTreeMap<String, Option<String>>> {
        Box::pin(async move {
            let mut located = BTreeMap::new();
            for chunk in chunks(locations, MAX_OBJECTS_PER_QUESTION) {
                let answer: LocateAnswer = self.ask("/locate", json!({"locations": chunk})).await?;
                located.extend(complete(answer.located, &chunk)?);
            }
            Ok(located)
        })
    }
}
