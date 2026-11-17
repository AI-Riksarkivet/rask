//! The shell: a table the catalog holds, reached over HTTP through its Lance Namespace doors as the caller's own identity.

use crate::arrow::{Rows, decode_answer, encode_stream};
use crate::bearer::Bearer;
use crate::errors::CatalogError;
use crate::requests::{self, Matched, Page, Request};
use arrow_schema::SchemaRef;
use lakehouse_core::service::problem::{ErrorCode, NsError};
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderValue};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// The property the catalog keeps a table's uuid under, minted at create and never shared by a table created again.
pub const UUID_PROPERTY: &str = "rask.uuid";
/// The bytes one page of a read asks for: a quarter of the catalog's default 16 MiB answer ceiling
/// (`LANCE_QUERY_MAX_RESPONSE_MB`), so a page sized from a measured row width stays under it with room for the catalog
/// estimating that width differently.
pub const PAGE_BYTES: usize = 4 << 20;
/// The first page's row count, before any row width is known.
pub const FIRST_PAGE_ROWS: usize = 64;
/// How many times a request that failed unanswered is sent again, where its [`requests::Resend`] allows.
const RESENDS: u32 = 3;

/// The catalog at one base URL: one connection pool, shared by every table reached through it.
///
/// The pool carries no identity: each request carries the bearer asked of [`Bearer`] as it is sent, so one caller's
/// token never answers another's call.
#[derive(Clone)]
pub struct Catalog {
    http: reqwest::Client,
    base: Arc<str>,
    bearer: Bearer,
}

impl Catalog {
    /// The catalog at `base_url`, each request bounded by `timeout`. Redirects are not followed, so a request and its
    /// bearer go only where they were addressed.
    pub fn new(base_url: &str, bearer: Bearer, timeout: Duration) -> Result<Self, NsError> {
        let http = reqwest::Client::builder().timeout(timeout).redirect(reqwest::redirect::Policy::none()).build();
        let http = http.map_err(|error| NsError::new(ErrorCode::Internal, format!("the catalog's HTTP client could not be built: {error}")))?;
        Ok(Catalog {
            http,
            base: base_url.trim_end_matches('/').into(),
            bearer,
        })
    }

    /// The table named by `id`'s segments.
    pub fn table(&self, id: Vec<String>) -> Table {
        Table {
            catalog: self.clone(),
            id,
            page_rows: Arc::default(),
        }
    }

    /// Send `request` and answer its body, or the refusal it met. The bearer is asked for once, before the first send.
    async fn send(&self, request: Request) -> Result<Vec<u8>, CatalogError> {
        let token = (self.bearer)().map_err(|error| CatalogError { status: None, error })?;
        let mut headers = HeaderMap::new();
        for (name, value) in requests::headers(token.as_deref()) {
            let mut value = HeaderValue::from_str(&value).map_err(|_| CatalogError::local(ErrorCode::ServiceUnavailable, "the bearer is no header value"))?;
            // Kept out of every debug rendering of the request.
            value.set_sensitive(name == AUTHORIZATION.as_str());
            headers.insert(name, value);
        }
        headers.insert(CONTENT_TYPE, HeaderValue::from_static(request.content_type));
        let url = format!("{}{}", self.base, request.target);
        let mut attempt = 0;
        loop {
            let (unsent, error) = match self.http.post(&url).headers(headers.clone()).body(request.body.clone()).send().await {
                Ok(answer) => {
                    let status = answer.status().as_u16();
                    match answer.bytes().await {
                        Ok(bytes) if (200..300).contains(&status) => return Ok(bytes.to_vec()),
                        Ok(bytes) => return Err(CatalogError::answered(status, &String::from_utf8_lossy(&bytes))),
                        Err(error) => (false, error),
                    }
                }
                Err(error) => (error.is_connect(), error),
            };
            if attempt >= RESENDS || !request.resend.allows(unsent) {
                return Err(CatalogError::unanswered(&error));
            }
            attempt += 1;
        }
    }
}

/// A scan of a table: which columns, which rows, and at which version.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Scan {
    pub columns: Option<Vec<String>>,
    pub filter: Option<String>,
    pub limit: Option<usize>,
    pub offset: Option<usize>,
    /// Whether each row carries its row id, as the column `_rowid`.
    pub with_row_id: bool,
    /// The version read; the current one, pinned when the read starts, when none is named.
    pub version: Option<u64>,
}

/// What the catalog describes a table as: its version, when it opened the table, and its properties.
#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
pub struct Described {
    pub version: Option<u64>,
    pub properties: Option<BTreeMap<String, String>>,
}

impl Described {
    /// The uuid the catalog minted for the table, or `None` when it keeps none.
    pub fn uuid(&self) -> Option<&str> {
        self.properties.as_ref()?.get(UUID_PROPERTY).map(String::as_str).filter(|uuid| !uuid.is_empty())
    }
}

/// A table the catalog holds, by its id.
#[derive(Clone)]
pub struct Table {
    catalog: Catalog,
    id: Vec<String>,
    /// The rows a page of each projection takes, from the width its last page measured.
    page_rows: Arc<Mutex<HashMap<Option<Vec<String>>, usize>>>,
}

impl Table {
    /// Create the table with `schema` and no rows, keeping it as it is when it already exists.
    pub async fn create(&self, schema: SchemaRef) -> Result<(), CatalogError> {
        let stream = encode_stream(&Rows::empty(schema)).map_err(|error| unencodable(&error))?;
        self.catalog.send(requests::create(&self.id, stream)).await.map(drop)
    }

    /// The table as the catalog's record describes it, its properties included.
    pub async fn describe(&self) -> Result<Described, CatalogError> {
        described(&self.catalog.send(requests::describe(&self.id, false)).await?)
    }

    /// The table's current version: what a pinned read and the commit handshake compare against.
    pub async fn version(&self) -> Result<u64, CatalogError> {
        let described = described(&self.catalog.send(requests::describe(&self.id, true)).await?)?;
        let missing = || CatalogError::local(ErrorCode::ServiceUnavailable, format!("catalog describe returned no version for {:?}", self.id));
        described.version.ok_or_else(missing)
    }

    /// The rows `filter` holds for (every row when none is given), at `version` (the current one when none is given).
    pub async fn count_rows(&self, filter: Option<&str>, version: Option<u64>) -> Result<u64, CatalogError> {
        let answer = self.catalog.send(requests::count_rows(&self.id, filter, version)).await?;
        let count = String::from_utf8_lossy(&answer).trim().parse();
        count.map_err(|_| CatalogError::local(ErrorCode::Internal, "the catalog answered a count that is no whole number"))
    }

    /// Scan the table, page by page, every page pinned to one version so a read cannot straddle a commit.
    ///
    /// The catalog refuses a `k` whose answer could exceed its ceiling, because it builds each answer whole, so a read
    /// is many bounded answers: each page is [`PAGE_BYTES`] worth of rows at the width the last page of the same
    /// projection measured on this table handle, and a page shorter than the `k` it asked for is the last.
    pub async fn read(&self, scan: &Scan) -> Result<Rows, CatalogError> {
        let version = match scan.version {
            Some(version) => version,
            None => self.version().await?,
        };
        let columns = scan.columns.clone().filter(|columns| !columns.is_empty());
        let start = scan.offset.unwrap_or(0);
        let mut page_rows = self
            .page_rows
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(&columns)
            .copied()
            .unwrap_or(FIRST_PAGE_ROWS);
        let mut read: Option<Rows> = None;
        loop {
            let done = read.as_ref().map_or(0, Rows::num_rows);
            let k = page_size(page_rows, scan.limit, done);
            let page = Page {
                columns: columns.as_deref(),
                filter: scan.filter.as_deref(),
                k,
                offset: start + done,
                with_row_id: scan.with_row_id,
                version,
            };
            let answer = self.catalog.send(requests::query(&self.id, &page)).await?;
            let rows = decode_answer(&answer)
                .map_err(|error| CatalogError::local(ErrorCode::Internal, format!("the catalog answered bytes that are not valid Arrow IPC: {error}")))?;
            let got = rows.num_rows();
            if got > 0 {
                page_rows = rows_per_page(rows.nbytes(), got);
                self.page_rows
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .insert(columns.clone(), page_rows);
            }
            let rows = match read.take() {
                Some(earlier) => earlier
                    .joined(rows)
                    .ok_or_else(|| CatalogError::local(ErrorCode::Internal, "the pages of one read answered different schemas"))?,
                None => rows,
            };
            let finished = got < k || scan.limit.is_some_and(|limit| rows.num_rows() >= limit);
            read = Some(rows);
            if finished {
                break;
            }
        }
        let rows = read.ok_or_else(|| CatalogError::local(ErrorCode::Internal, "a read took no page"))?;
        Ok(match scan.limit {
            Some(limit) => rows.head(limit),
            None => rows,
        })
    }

    /// Insert the rows whose `on` key the table does not hold; a held key lands nothing.
    pub async fn merge_insert_only(&self, rows: &Rows, on: &str) -> Result<(), CatalogError> {
        self.merge(rows, on, &Matched::Keep).await
    }

    /// Insert the unmatched rows and update the matched ones, only where `matched_when` holds when it is given.
    ///
    /// `matched_when` is an SQL condition over `source.<col>` and `target.<col>`: `source.seq > target.seq` keeps a
    /// row's value from moving backward.
    pub async fn merge_upsert(&self, rows: &Rows, on: &str, matched_when: Option<&str>) -> Result<(), CatalogError> {
        self.merge(rows, on, &Matched::Update(matched_when.map(str::to_owned))).await
    }

    async fn merge(&self, rows: &Rows, on: &str, matched: &Matched) -> Result<(), CatalogError> {
        let stream = encode_stream(rows).map_err(|error| unencodable(&error))?;
        self.catalog.send(requests::merge_insert(&self.id, on, matched, stream)).await.map(drop)
    }

    /// Delete the rows `predicate` holds for.
    pub async fn delete(&self, predicate: &str) -> Result<(), CatalogError> {
        self.catalog.send(requests::delete(&self.id, predicate)).await.map(drop)
    }
}

/// The `k` of the next page: a page's rows, or what is left of the limit, and at least one.
pub fn page_size(page_rows: usize, limit: Option<usize>, read: usize) -> usize {
    limit.map_or(page_rows, |limit| page_rows.min(limit.saturating_sub(read))).max(1)
}

/// The rows a page takes after one of `rows` rows measured `nbytes`: at least a byte a row, since a projection of a
/// field that stores nothing measures none.
pub fn rows_per_page(nbytes: usize, rows: usize) -> usize {
    (PAGE_BYTES / nbytes.div_ceil(rows.max(1)).max(1)).max(1)
}

/// What a describe answer says.
pub fn described(answer: &[u8]) -> Result<Described, CatalogError> {
    serde_json::from_slice(answer).map_err(|error| CatalogError::local(ErrorCode::Internal, format!("the catalog's describe answer is not one: {error}")))
}

fn unencodable(error: &dyn std::fmt::Display) -> CatalogError {
    CatalogError::local(ErrorCode::InvalidInput, format!("the rows could not be written as Arrow IPC: {error}"))
}
