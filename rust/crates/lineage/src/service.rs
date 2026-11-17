//! The lineage service, started from the environment the chart gives its Deployment and the flags it gives the server
//! (`lineage.main` with the library's `lance_app`, `supervision`, `draining`, `lifecycle`, `probes` and `health`): what
//! starts in what order, what readiness reads, and how the process stops (ADR 0238).
//!
//! The port is bound first, so the probes answer while the rest starts: `/livez` 200, `/readyz` 503 until everything
//! below has started, and again from SIGTERM on. Then, in the Python's order: the secret-store session, the object-store
//! session, the catalog's tables, the authenticator, the access decider, the bus, the published keys, the ingest lane,
//! the health watch and the relay. A start that cannot finish ends the process with its reason.

use crate::app::router;
use crate::authz::names::CatalogNames;
use crate::gate::Gate;
use crate::lane::IngestLane;
use crate::settings::Settings;
use crate::signals::Signals;
use crate::state::{Publisher, State};
use crate::store::Store;
use crate::tick::Relay;
use axum::Router;
use axum::extract::Request;
use lakehouse_bus::{Bus, BusSettings};
use lakehouse_core::auth::issuer;
use lakehouse_core::auth::settings::AuthSettings;
use lakehouse_core::authz::AccessDecider;
use lakehouse_core::authz::catalog::CatalogDecider;
use lakehouse_core::authz::openfga::{OpenFga, StoreSettings as FgaSettings};
use lakehouse_core::outbox::Outbox;
use lakehouse_core::secrets::{PublishedKeys, SecretStoreSettings, Session, store_fetch};
use lakehouse_core::service::health::{Dependency, HealthWatch};
use lakehouse_core::service::lifecycle::Lifecycle;
use lakehouse_core::service::shell::Shell;
use lakehouse_core::service::supervision::{Supervisor, end_process, stopped};
use lakehouse_core::storage::{S3, StoreSession};
use lakehouse_core::telemetry::Telemetry;
use lakehouse_core::telemetry::http::HttpTelemetry;
use lakehouse_core::telemetry::instruments::BusMetrics;
use lakehouse_tables::{Catalog, bearer};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::net::TcpListener;
use tower::ServiceExt as _;

/// The exit code of a start refused, as uvicorn ends one whose application did not start.
pub const START_REFUSED: i32 = 3;
/// How long the start waits for the catalog to create this service's tables, and how often it asks: the catalog keeps
/// the provenance namespace itself and may still be starting when this pod is.
const ENSURE_DEADLINE: Duration = Duration::from_secs(120);
const ENSURE_RETRY: Duration = Duration::from_secs(2);

/// The server's flags as the chart passes them (`--host=`, `--port=`, `--timeout-graceful-shutdown=`). The proxy-header
/// flags are taken and change nothing: no door reads the peer's address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub host: String,
    pub port: u16,
    /// How long the requests open at SIGTERM have to be answered; unset, the request deadline.
    pub grace: Option<Duration>,
}

impl Args {
    pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Args, String> {
        let mut parsed = Args {
            host: "127.0.0.1".to_owned(),
            port: 8000,
            grace: None,
        };
        for arg in args {
            let (flag, value) = arg.split_once('=').unwrap_or((&arg, ""));
            let refused = || format!("{flag} is {value:?}, which is not a number");
            match flag {
                "--host" => value.clone_into(&mut parsed.host),
                "--port" => parsed.port = value.parse().map_err(|_| refused())?,
                "--timeout-graceful-shutdown" => parsed.grace = Some(Duration::from_secs(value.parse().map_err(|_| refused())?)),
                "--proxy-headers" | "--forwarded-allow-ips" => {}
                _ => return Err(format!("{arg} is no flag of `lineage serve`")),
            }
        }
        Ok(parsed)
    }
}

/// Serve until SIGTERM, then stop; the exit code. Telemetry starts first and is flushed last.
pub async fn run(args: Args) -> i32 {
    let telemetry = match Telemetry::from_env() {
        Ok(telemetry) => telemetry,
        Err(why) => {
            eprintln!("lineage did not start: {why}");
            return START_REFUSED;
        }
    };
    if let Err(why) = telemetry.install() {
        eprintln!("lineage did not start: {why}");
        return START_REFUSED;
    }
    let code = match serve(args, &telemetry).await {
        Ok(()) => 0,
        Err(why) => {
            tracing::error!(error = why, "lineage_start_refused");
            START_REFUSED
        }
    };
    telemetry.shutdown().await;
    code
}

/// Whether OpenFGA is consulted, and who decides: the catalog's decision door where the chart names one, else OpenFGA
/// directly at the pinned store and model (`lakehouse.governed.auth_lifespan`).
fn decider(env: &HashMap<String, String>, timeout: Duration) -> Result<Decided, String> {
    let get = |name: &str| env.get(name).filter(|value| !value.is_empty()).cloned();
    let enabled = get("RASK_FGA_ENABLED").is_some_and(|flag| matches!(flag.trim().to_lowercase().as_str(), "1" | "true" | "t" | "yes" | "y" | "on"));
    if !enabled {
        return Ok((false, None, None));
    }
    if let Some(url) = get("RASK_FGA_DECIDER_URL") {
        let token = get("RASK_FGA_DECIDER_TOKEN_FILE")
            .ok_or("RASK_FGA_DECIDER_URL is set but RASK_FGA_DECIDER_TOKEN_FILE is not; the catalog would refuse every decision")?;
        return Ok((true, Some(Arc::new(CatalogDecider::new(&url, Some(token.into()), timeout)?)), None));
    }
    let (Some(store), Some(model)) = (get("RASK_FGA_STORE_ID"), get("RASK_FGA_MODEL_ID")) else {
        return Err("RASK_FGA_ENABLED names OpenFGA with no RASK_FGA_DECIDER_URL: set RASK_FGA_STORE_ID and RASK_FGA_MODEL_ID".to_owned());
    };
    let api = get("RASK_FGA_API_URL").unwrap_or_else(|| "http://openfga:8080".to_owned());
    let mut settings = FgaSettings::new(&api, &store, &model, get("RASK_FGA_TOKEN_FILE").map(Into::into));
    settings.timeout = timeout;
    let fga = Arc::new(OpenFga::new(settings)?);
    Ok((true, Some(Arc::clone(&fga) as Arc<dyn AccessDecider>), Some(fga)))
}

/// Create this service's tables through the catalog, asking again until it answers or the deadline passes.
async fn ensure_tables(store: &Store) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + ENSURE_DEADLINE;
    loop {
        match store.ensure().await {
            Ok(()) => return Ok(()),
            Err(error) if tokio::time::Instant::now() > deadline => return Err(format!("the provenance tables were not created: {error}")),
            Err(error) => tracing::warn!(error = %error, "lineage_provenance_tables_not_ready"),
        }
        tokio::time::sleep(ENSURE_RETRY).await;
    }
}

fn dependency<F: Future<Output = Result<(), String>> + Send + 'static>(
    name: &'static str,
    required: bool,
    read: impl Fn() -> F + Send + Sync + 'static,
) -> Dependency {
    Dependency {
        name,
        required,
        read: Box::new(move || Box::pin(read())),
    }
}

/// What lineage reads to know it can serve: the feed through the catalog gates readiness; OpenFGA where it is asked
/// directly, the bus, the object store holding the outbox and the secret store are reported and never gate.
fn dependencies(
    store: &Store,
    fga: Option<Arc<OpenFga>>,
    root: &str,
    bus: Option<&Arc<Bus>>,
    outbox: Option<&Arc<Outbox>>,
    secrets: Option<&Session>,
) -> Vec<Dependency> {
    let shown = |error: &dyn std::fmt::Display| error.to_string();
    let store = store.clone();
    let mut watched = vec![dependency("catalog", true, move || {
        let store = store.clone();
        async move { store.ready().await.map_err(|error| shown(&error)) }
    })];
    if let Some(fga) = fga {
        let root = root.to_owned();
        watched.push(dependency("openfga", false, move || {
            let (fga, root) = (Arc::clone(&fga), root.clone());
            async move {
                fga.check("kubernetes~readiness", "can_read_event_feed", &root)
                    .await
                    .map(drop)
                    .map_err(|error| error.0)
            }
        }));
    }
    if let Some(bus) = bus.cloned() {
        watched.push(dependency("bus", false, move || {
            let answering = bus.answering().map_err(|error| error.0);
            async move { answering }
        }));
    }
    if let Some(outbox) = outbox.cloned() {
        watched.push(dependency("store", false, move || {
            let outbox = Arc::clone(&outbox);
            async move { outbox.listing().await.map(drop).map_err(|error| shown(&error)) }
        }));
    }
    if let Some(secrets) = secrets {
        let store = Arc::clone(&secrets.store);
        watched.push(dependency("secret-store", false, move || {
            let store = Arc::clone(&store);
            async move { store.answers().await.map_err(|error| format!("{error:?}")) }
        }));
    }
    watched
}

/// Start, serve until SIGTERM, drain, then stop what started; a lane or loop that ends while serving ends the process.
async fn serve(args: Args, telemetry: &Telemetry) -> Result<(), String> {
    let pairs: Vec<(String, String)> = std::env::vars().collect();
    let env: HashMap<String, String> = pairs.iter().map(|(name, value)| (name.to_uppercase(), value.clone())).collect();
    let settings = Arc::new(Settings::from_environment(pairs.clone())?);
    let auth = AuthSettings::from_environment(pairs.clone())?;
    auth.require_authentication_or_its_waiver()?;
    let fga_timeout = env.get("RASK_FGA_TIMEOUT_SECONDS").map_or(Ok(5.0), |seconds| seconds.trim().parse::<f64>());
    let fga_timeout = fga_timeout
        .ok()
        .filter(|seconds| *seconds >= 0.1)
        .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok())
        .ok_or("RASK_FGA_TIMEOUT_SECONDS is not a number of seconds of at least 0.1")?;
    let read = Read {
        decided: decider(&env, fga_timeout)?,
        secrets: SecretStoreSettings::from_environment(pairs.clone())?,
        bus: BusSettings::from_env()?,
        root: env.get("RASK_FGA_ROOT_OBJECT").cloned().unwrap_or_else(|| "estate:rask".to_owned()),
        settings: Arc::clone(&settings),
        auth,
    };
    telemetry.audit(settings.audit_enabled).install();

    let lifecycle = Arc::new(Lifecycle::default());
    let probes = Shell {
        lifecycle: Arc::clone(&lifecycle),
        health: Arc::new(HealthWatch::new(Vec::new())),
        max_body_bytes: usize::try_from(settings.max_body_bytes).unwrap_or(usize::MAX),
        request_deadline: settings.request_deadline,
    };
    let serving: Arc<OnceLock<Router>> = Arc::new(OnceLock::new());
    let starting = probes.router(Router::new());
    let ready = Arc::clone(&serving);
    let front = Router::new().fallback(move |request: Request| {
        let router = ready.get().cloned().unwrap_or_else(|| starting.clone());
        async move { router.oneshot(request).await }
    });
    let listener = TcpListener::bind((args.host.as_str(), args.port))
        .await
        .map_err(|error| format!("{}:{} cannot be bound: {error}", args.host, args.port))?;
    let grace = args.grace.unwrap_or(settings.request_deadline);
    let mut served = tokio::spawn({
        let shell = probes.clone();
        async move { shell.serve(listener, front, grace).await }
    });

    let mut loops = Supervisor::default();
    let started = tokio::select! {
        started = start(read, telemetry, &mut loops) => started?,
        _ = &mut served => return Ok(()),
    };
    let (state, watch, bus, _secrets) = started;
    let shell = Shell { health: watch, ..probes };
    let _ = serving.set(shell.instrumented_router(router(state), &HttpTelemetry::new(telemetry)));
    lifecycle.mark_started();
    tracing::info!("lineage_started");

    tokio::select! {
        answered = &mut served => {
            if let Ok(Err(error)) = answered {
                tracing::error!(error = %error, "lineage_serve_failed");
            }
        }
        (name, panicked) = loops.ended() => end_process(telemetry, name, panicked).await,
    }
    // The doors have answered what they took; the lanes finish the delivery each holds, then everything is released.
    loops.stop().await;
    if let Some(bus) = bus {
        bus.close().await;
    }
    tracing::info!("lineage_stopped");
    Ok(())
}

type Started = (State, Arc<HealthWatch>, Option<Arc<Bus>>, Option<Session>);
/// Whether OpenFGA is consulted, the decider asked, and the OpenFGA client where it is asked directly.
type Decided = (bool, Option<Arc<dyn AccessDecider>>, Option<Arc<OpenFga>>);

/// What the start reads from the environment, every value taken before anything is started.
struct Read {
    settings: Arc<Settings>,
    auth: AuthSettings,
    decided: Decided,
    secrets: SecretStoreSettings,
    bus: Option<BusSettings>,
    /// The object every estate privilege is checked on (`RASK_FGA_ROOT_OBJECT`).
    root: String,
}

/// Everything the doors and the lanes run with, each piece started in the Python's order and its loops kept on `loops`.
async fn start(read: Read, telemetry: &Telemetry, loops: &mut Supervisor) -> Result<Started, String> {
    let (settings, (fga_enabled, decider, direct)) = (&read.settings, read.decided);
    let secrets = Session::from_settings(read.secrets).await?;
    let s3 = match &settings.store {
        Some(identity) => {
            let session = Arc::new(StoreSession::new(identity, "lineage")?);
            session.exchange_at_boot().await?;
            loops.keep_spawned("store-session", session.keep_renewed());
            Some(Arc::new(S3::new(identity, session)?))
        }
        None => None,
    };
    let bearer = match settings.catalog_identity_token_file.as_str() {
        "" => bearer::anonymous(),
        file => bearer::token_file(file),
    };
    let tables = Catalog::new(&settings.catalog_url, bearer, settings.catalog_timeout).map_err(|error| error.message)?;
    let namespace = settings.provenance_namespace.split('$').map(str::to_owned).collect();
    let store = Store::new(tables, namespace, &settings.catalog_dataset_namespace, fga_enabled).map_err(|error| error.to_string())?;
    ensure_tables(&store).await?;
    let names = Arc::new(CatalogNames::new(&settings.catalog_url, settings.catalog_timeout)?);
    let (authenticator, issuer_keys) = issuer::authenticator(&read.auth)?;
    loops.keep_spawned("oidc-keys", issuer::start(&issuer_keys).await);
    let bus = read
        .bus
        .map(|bus| Arc::new(Bus::start(bus).instrumented(BusMetrics::new(&telemetry.meter("lance.bus")))));
    let fetch = match &secrets {
        Some(session) => store_fetch(Arc::clone(&session.store), tokio::runtime::Handle::current()),
        None => Arc::new(|_: &str| BTreeMap::new()),
    };
    let gate = Arc::new(Gate {
        signing: settings.signing.clone(),
        keys: Arc::new(PublishedKeys::new(fetch)),
        fga_enabled,
        decider,
        catalog: settings.catalog_dataset_namespace.clone(),
        root: read.root,
        store: store.clone(),
        signals: Arc::new(Signals::of(telemetry)),
    });
    if let Some(bus) = &bus {
        let (lane, settled_by, bus) = (Arc::new(IngestLane::new(Arc::clone(&gate))), settings.lane()?, Arc::clone(bus));
        loops.keep_until_stopped("lineage-ingest", |stopping| async move {
            if let Err(panicked) = lane.run(&bus, &settled_by, stopped(stopping)).await {
                tracing::error!(error = %panicked, "lineage_ingest_ended");
            }
        });
    }
    let outbox = match settings.outbox_uri.as_str() {
        "" => None,
        uri => Some(Arc::new(Outbox::new(
            uri,
            s3.clone().ok_or("LINEAGE_OUTBOX_URI is on the object store and no store session was started")?,
        )?)),
    };
    let watch = Arc::new(HealthWatch::new(dependencies(
        &store,
        direct,
        &gate.root,
        bus.as_ref(),
        outbox.as_ref(),
        secrets.as_ref(),
    )));
    for watching in watch.watch() {
        loops.keep_spawned("health", watching);
    }
    if settings.relay_period_seconds > 0.0 {
        let secret_store = secrets.as_ref().map(|session| Arc::clone(&session.store));
        let relay = Relay::new(Arc::clone(settings), Arc::clone(&gate), bus.clone(), s3, secret_store)?;
        loops.keep("timer-lineage-relay", async move { relay.run().await });
    }
    let publisher = bus.as_ref().map(|bus| Publisher {
        bus: Arc::clone(bus),
        topic: settings.events_topic.clone(),
        within: settings.publish_timeout,
    });
    let state = State {
        authenticator: Arc::new(authenticator),
        gate,
        names,
        outbox,
        publisher,
    };
    Ok((state, watch, bus, secrets))
}
