//! This process's one connection to the bus, as its own bus user: each event published on it under a timeout, each
//! delivery of a durable consumer taken on it and settled, and a stream read by sequence.
//!
//! The process holds no bus credential. The chart names the user and the file the kubelet writes the pod's
//! ServiceAccount token to; each connect presents the token as the file holds it then, and the bus's signer answers with
//! a session it ends after `RASK_BUS_SESSION_SECONDS`, so the connection connects again shortly before that. A `tls://`
//! bus is dialled with the handshake before the server's greeting, so the token never travels in the clear.
//!
//! The service binds to the consumers the chart's stream Job declares and creates nothing on the bus.

use crate::envelope::{CloudEvent, Oversize, STRUCTURED_CONTENT_TYPE, TraceContext, WARN_PAYLOAD_BYTES};
use crate::jetstream::{self, ACK_PREFIX, ApiError, NO_MESSAGE_FOUND, PULL_EXPIRED};
use crate::page::StreamPage;
use async_nats::connection::State;
use async_nats::{Auth, AuthError, Client, ConnectOptions, Event, HeaderMap, Message, client::RequestErrorKind};
use futures_util::{FutureExt, StreamExt};
use lakehouse_core::delivery::{Delivery, Lane, Verdict, judged};
use std::collections::HashSet;
use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, watch};
use tokio::task::{JoinHandle, JoinSet};
use tokio::time::{Instant, sleep, sleep_until, timeout};

/// The prefix of a bus user's reply subjects, before the user's name: the permission table grants it to that user alone.
const INBOX_PREFIX: &str = "_INBOX_";
/// How long before a session's time is up it is asked for again: this share of its length, within these bounds.
const RENEW_SHARE: f64 = 0.1;
const RENEW_MARGIN: (Duration, Duration) = (Duration::from_secs(5), Duration::from_secs(30));
/// How long after a session is asked for again a publish waits for the connection before it is called undelivered.
const RENEWED_WITHIN: Duration = Duration::from_secs(2);
/// How long the client reconnects by itself: this many attempts at each server, this far apart, before it closes and
/// the keeper opens another.
const RECONNECT_ATTEMPTS: usize = 10;
const RECONNECT_WAIT: Duration = Duration::from_secs(2);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// How long one opening of a connection may take: a peer that answers a handshake with something else must not hold it.
const OPEN_WITHIN: Duration = Duration::from_secs(45);
/// How often the client asks its server whether it is still there; two unanswered asks drop the connection.
const PING_INTERVAL: Duration = Duration::from_secs(10);
/// How long a closing connection waits for the bus to take what it has sent, and then to be let go.
const CLOSE_WITHIN: Duration = Duration::from_secs(3);
/// The bounds of the random pause between one failed opening and the next, so a release's replicas do not ask in step.
const PAUSE_SECONDS: (u64, u64) = (2, 15);
/// How long one pull waits on a consumer that holds nothing, and how long a pull the bus refused waits before the next.
const PULL_WAIT: Duration = Duration::from_secs(5);
/// How long past a pull's own wait the bus is given to end it before it is judged gone.
const PULL_ANSWERED_WITHIN: Duration = Duration::from_secs(10);
/// How long a JetStream API request is given.
const API_WITHIN: Duration = Duration::from_secs(5);
/// How many different faults one outage is told of.
const FAULTS_SAID: usize = 16;
/// How the bus refuses a subscription its connection's user is not granted.
const SUBSCRIPTION_REFUSED: &str = "permissions violation for subscription";
const DISCONNECTED: &str = "disconnected";

/// The bus as the chart names it to a pod, and the bus user that pod's process connects as.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BusSettings {
    /// One NATS URL, or several separated by commas.
    pub url: String,
    /// One subject token: the name the connection asks the signer for a session under, and the tail of its inbox prefix.
    pub user: String,
    /// The file the kubelet keeps this pod's ServiceAccount token in, for the bus's audience.
    pub token_file: PathBuf,
    /// The CA bundle the bus's certificate chains to; none for the system's trust store.
    pub ca_file: Option<PathBuf>,
    /// How long the bus keeps a session before it ends it.
    pub session: Duration,
}

impl BusSettings {
    /// The bus the chart names in `RASK_BUS_*`; none for a process the chart names no bus. Only where the token is
    /// read from comes through the environment, never the token.
    pub fn from_env() -> Result<Option<Self>, String> {
        let read = |name: &str| std::env::var(name).unwrap_or_default();
        let url = read("RASK_BUS_URL");
        if url.is_empty() {
            return Ok(None);
        }
        let session = match read("RASK_BUS_SESSION_SECONDS").as_str() {
            "" => 300,
            seconds => seconds
                .parse::<u64>()
                .map_err(|_| "RASK_BUS_SESSION_SECONDS is not a whole number of seconds".to_owned())?,
        };
        let ca_file = read("RASK_BUS_CA_FILE");
        BusSettings {
            url,
            user: read("RASK_BUS_USER"),
            token_file: PathBuf::from(read("RASK_BUS_TOKEN_FILE")),
            ca_file: (!ca_file.is_empty()).then(|| PathBuf::from(ca_file)),
            session: Duration::from_secs(session),
        }
        .validated()
        .map(Some)
    }

    /// These settings, once they name a user that is one subject token, a token file and a session of 20 s or more.
    pub fn validated(self) -> Result<Self, String> {
        let user_valid = self.user.len() <= 243
            && self.user.bytes().next().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
            && self.user.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
        if !user_valid {
            return Err("RASK_BUS_URL names a bus and RASK_BUS_USER names no user to connect to it as".to_owned());
        }
        if self.token_file.as_os_str().is_empty() {
            return Err("RASK_BUS_URL names a bus and RASK_BUS_TOKEN_FILE names no token to present to it".to_owned());
        }
        if self.session < Duration::from_secs(20) {
            return Err("RASK_BUS_SESSION_SECONDS is below 20".to_owned());
        }
        Ok(self)
    }

    fn servers(&self) -> Vec<String> {
        self.url
            .split(',')
            .map(str::trim)
            .filter(|server| !server.is_empty())
            .map(str::to_owned)
            .collect()
    }

    /// The prefix of this user's reply subjects.
    pub fn inbox_prefix(&self) -> String {
        format!("{INBOX_PREFIX}{}", self.user)
    }

    /// How long into a session it is asked for again: its length less a margin the next connect fits in.
    pub fn renew_after(&self) -> Duration {
        let margin = self.session.mul_f64(RENEW_SHARE).clamp(RENEW_MARGIN.0, RENEW_MARGIN.1);
        self.session.saturating_sub(margin)
    }

    /// Whether every server of the bus is dialled over TLS, the handshake before anything else.
    pub fn encrypted(&self) -> bool {
        let servers = self.servers();
        !servers.is_empty() && servers.iter().all(|server| server.starts_with("tls://"))
    }
}

/// Why a publish is not delivered: the event stays where its caller staged it.
#[derive(Debug, thiserror::Error)]
pub enum PublishError {
    #[error(transparent)]
    Oversize(#[from] Oversize),
    #[error("the bus did not acknowledge {0} in time")]
    Timeout(String),
    #[error("{0}")]
    Unavailable(String),
}

/// The bus did not answer: nothing was read or taken.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct Unavailable(pub String);

/// A handling that ended in a panic: its delivery is unsettled and comes again after the consumer's ack wait.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("a handling of {consumer} panicked")]
pub struct HandlingPanicked {
    pub consumer: String,
}

/// A service's handling of the deliveries of one lane, and its keeping of one it parks.
pub trait Handler: Send + Sync + 'static {
    /// What became of `delivery`, or why its handling failed.
    fn handle(&self, delivery: &Delivery) -> impl Future<Output = Result<Verdict, String>> + Send;
    /// Keep `delivery`, which is ended unhandled for `reason`, where an admin finds it.
    fn park(&self, delivery: &Delivery, reason: &str) -> impl Future<Output = Result<(), String>> + Send;
}

/// This process's one connection to the bus, kept by a task of its own for as long as the `Bus` lives.
pub struct Bus {
    user: String,
    held: watch::Receiver<Option<Client>>,
    renewing: watch::Receiver<Option<Instant>>,
    keeper: JoinHandle<()>,
}

impl Bus {
    /// Start keeping a connection to the bus `settings` names. Nothing waits on it: a publish with no connection fails
    /// at once, and a consumer waits for one.
    pub fn start(settings: BusSettings) -> Self {
        let (held_tx, held) = watch::channel(None);
        let (renewing_tx, renewing) = watch::channel(None);
        let user = settings.user.clone();
        let keeper = tokio::spawn(keep(Arc::new(settings), held_tx, renewing_tx));
        Bus { user, held, renewing, keeper }
    }

    /// Ok while this process's connection to the bus is up: the verdict a readiness report shows for the bus.
    pub fn answering(&self) -> Result<(), Unavailable> {
        match self.held.borrow().as_ref() {
            Some(client) if client.connection_state() == State::Connected => Ok(()),
            _ => Err(Unavailable("this process is not connected to the bus".to_owned())),
        }
    }

    /// The connection when it is up; one asking for its session again is waited for up to [`RENEWED_WITHIN`].
    async fn up(&self) -> Option<Client> {
        loop {
            let client = self.held.borrow().clone()?;
            if client.connection_state() == State::Connected {
                return Some(client);
            }
            let renewing = *self.renewing.borrow();
            if !renewing.is_some_and(|until| Instant::now() < until) {
                return None;
            }
            sleep(Duration::from_millis(20)).await;
        }
    }

    /// The connection held now, waited for while there is none.
    async fn connection(&self) -> Client {
        let mut held = self.held.clone();
        loop {
            if let Some(client) = held.borrow_and_update().clone() {
                return client;
            }
            if held.changed().await.is_err() {
                // The keeper is gone, so no connection comes; the consumer waits to be stopped.
                std::future::pending::<()>().await;
            }
        }
    }

    /// Publish `body` on `topic` under `message_id` and wait up to `within` for the stream's acknowledgement. A duplicate
    /// acknowledgement is believed only once the stored message it names is read and carries the same event.
    pub async fn send(&self, topic: &str, body: Vec<u8>, message_id: &str, within: Duration) -> Result<(), PublishError> {
        let client = self
            .up()
            .await
            .ok_or_else(|| PublishError::Unavailable(format!("this process is not connected to the bus: {message_id} was not published on {topic}")))?;
        let undelivered = |what: String| PublishError::Unavailable(clipped(format!("the bus did not take {message_id} on {topic}: {what}"), 500));
        let sending = async {
            let mut headers = HeaderMap::new();
            headers.insert("Nats-Msg-Id", message_id);
            headers.insert("Content-Type", STRUCTURED_CONTENT_TYPE);
            let reply = client
                .request_with_headers(topic.to_owned(), headers, body.clone().into())
                .await
                .map_err(|error| match error.kind() {
                    RequestErrorKind::TimedOut => PublishError::Timeout(message_id.to_owned()),
                    _ => undelivered(error.to_string()),
                })?;
            let acknowledged = jetstream::answer(&reply.payload)
                .ok()
                .as_ref()
                .and_then(jetstream::publish_ack)
                .ok_or_else(|| undelivered(String::from_utf8_lossy(&reply.payload).into_owned()))?;
            if acknowledged.duplicate && !stored_carries(&client, &acknowledged, &body).await {
                return Err(PublishError::Unavailable(format!(
                    "the bus holds another message under the id of {message_id} on {topic}: the event is not delivered"
                )));
            }
            Ok(())
        };
        timeout(within, sending)
            .await
            .unwrap_or_else(|_| Err(PublishError::Timeout(message_id.to_owned())))
    }

    /// What `stream` holds on `subject` after the sequence `after`, read by sequence: at most `limit` messages, and from
    /// `after` 0 the last `limit` the stream holds. Every reader of one stream reads the same messages under the same
    /// sequences, so a reader's place is good with any process.
    pub async fn read(&self, stream: &str, subject: &str, after: u64, limit: u64) -> Result<StreamPage, Unavailable> {
        let client = self
            .up()
            .await
            .ok_or_else(|| Unavailable(format!("this process is not connected to the bus: {subject} was not read")))?;
        let unanswered = |error: ApiError| Unavailable(clipped(format!("the bus did not answer the read of {subject}: {error}"), 500));
        let end = match api(&client, stream, jetstream::last_request(subject)).await {
            Ok(answer) => jetstream::stored(&answer).map_or(0, |stored| stored.sequence),
            // A stream that is missing is a read the bus cannot answer, not an empty one.
            Err(error) if error.err_code == NO_MESSAGE_FOUND => 0,
            Err(error) => return Err(unanswered(error)),
        };
        let mut messages = Vec::new();
        let mut place = StreamPage::first_place(after, end, limit);
        while let Some(at) = place.filter(|at| (messages.len() as u64) < limit && *at < end) {
            let stored = match api(&client, stream, jetstream::next_request(at + 1, subject)).await {
                Ok(answer) => jetstream::stored(&answer),
                Err(error) if error.err_code == NO_MESSAGE_FOUND => break,
                Err(error) => return Err(unanswered(error)),
            };
            let Some(stored) = stored else { break };
            let sequence = if stored.sequence > 0 { stored.sequence } else { at + 1 };
            place = Some(sequence);
            let subject = if stored.subject.is_empty() { subject.to_owned() } else { stored.subject };
            messages.push(Delivery {
                subject,
                body: stored.data,
                sequence,
                attempt: 1,
            });
        }
        Ok(StreamPage::of(after, end, messages))
    }

    /// Hand every message of `lane`'s consumer to `handler` until `stop` resolves, at most `lane.in_flight` at once, and
    /// tell the bus what became of each (`lakehouse_core::delivery::judged`).
    ///
    /// The consumer is the chart's: this binds to it and creates nothing, and one the bus does not hold yet is asked for
    /// again until it does. Once `stop` resolves no pull is begun; the one under way is waited out, and every handling
    /// already begun is finished and settled before this returns.
    pub async fn consume<H: Handler>(&self, lane: &Lane, handler: Arc<H>, stop: impl Future<Output = ()>) -> Result<(), HandlingPanicked> {
        let mut handling: JoinSet<()> = JoinSet::new();
        let mut said = String::new();
        let mut panicked = false;
        tokio::pin!(stop);
        loop {
            while let Some(ended) = handling.try_join_next() {
                panicked |= ended.is_err();
            }
            if panicked {
                break;
            }
            if handling.len() >= lane.in_flight {
                tokio::select! {
                    ended = handling.join_next() => panicked |= matches!(ended, Some(Err(_))),
                    () = &mut stop => break,
                }
                continue;
            }
            let client = tokio::select! {
                client = self.connection() => client,
                () = &mut stop => break,
            };
            match pull(&client, &lane.stream, &lane.consumer).await {
                Ok(Some(message)) => {
                    said.clear();
                    handling.spawn(settled(client, message, lane.clone(), Arc::clone(&handler)));
                }
                Ok(None) => {}
                Err(fault) => {
                    if fault != said {
                        tracing::warn!(stream = lane.stream, consumer = lane.consumer, error = fault, "bus_pull_failed");
                    }
                    said = fault;
                    tokio::select! {
                        () = sleep(PULL_WAIT) => {}
                        () = &mut stop => break,
                    }
                }
            }
            if (&mut stop).now_or_never().is_some() {
                break;
            }
        }
        while let Some(ended) = handling.join_next().await {
            panicked |= ended.is_err();
        }
        if panicked {
            tracing::error!(consumer = lane.consumer, "bus_handling_panicked");
            return Err(HandlingPanicked {
                consumer: lane.consumer.clone(),
            });
        }
        Ok(())
    }

    /// Close the connection once the bus has taken what was sent on it, or after [`CLOSE_WITHIN`] without.
    pub async fn close(self) {
        self.keeper.abort();
        let client = self.held.borrow().clone();
        if let Some(client) = client {
            let flushed = client.connection_state() == State::Connected && matches!(timeout(CLOSE_WITHIN, client.flush()).await, Ok(Ok(())));
            if !flushed {
                tracing::warn!(user = self.user, "bus_closed_unflushed");
            }
            let _ = timeout(CLOSE_WITHIN, client.drain()).await;
        }
    }
}

impl Drop for Bus {
    fn drop(&mut self) {
        self.keeper.abort();
    }
}

/// Publish `envelope` on `topic` over this process's connection, within `within`, under the publish's trace context
/// `trace`, or the trace the envelope was staged under where the caller names none.
pub async fn publish_event(bus: &Bus, envelope: &CloudEvent, topic: &str, within: Duration, trace: Option<&TraceContext>) -> Result<(), PublishError> {
    let staged = TraceContext::continuing(envelope.traceparent.as_deref(), envelope.tracestate.as_deref());
    let body = envelope.body(trace.or(staged.as_ref())).inspect_err(|oversize| {
        tracing::warn!(topic, bytes = oversize.bytes, "bus_publish_refused_oversize");
    })?;
    if body.len() > WARN_PAYLOAD_BYTES {
        tracing::warn!(topic, bytes = body.len(), "bus_publish_payload_large");
    }
    bus.send(topic, body, &envelope.id, within).await
}

fn clipped(mut text: String, most: usize) -> String {
    if let Some((cut, _)) = text.char_indices().nth(most) {
        text.truncate(cut);
    }
    text
}

/// Ask the JetStream API of `stream` for a read by sequence.
async fn api(client: &Client, stream: &str, request: Vec<u8>) -> Result<serde_json::Value, ApiError> {
    let failed = |description: String| ApiError {
        code: 0,
        err_code: 0,
        description,
    };
    match timeout(API_WITHIN, client.request(jetstream::get_subject(stream), request.into())).await {
        Ok(Ok(reply)) => jetstream::answer(&reply.payload),
        Ok(Err(error)) => Err(failed(error.to_string())),
        Err(_) => Err(failed("the bus did not answer in time".to_owned())),
    }
}

/// Whether the message the stream kept at the acknowledged sequence carries the event `body` does.
async fn stored_carries(client: &Client, acknowledged: &jetstream::PublishAck, body: &[u8]) -> bool {
    let Ok(answer) = api(client, &acknowledged.stream, jetstream::at_request(acknowledged.sequence)).await else {
        return false;
    };
    let kept = jetstream::stored(&answer).and_then(|stored| jetstream::event_of(&stored.data));
    kept.is_some() && kept == jetstream::event_of(body)
}

/// The message one pull on `consumer` of `stream` is answered with, or none once the bus ends the pull with none.
///
/// The pull is read until the bus ends it, never sooner: the bus answers it until it learns of the unsubscribe, so a
/// message sent to an inbox nobody reads would be spent there and handed out again only an ack wait later. A delivery
/// is told from a status by its reply subject, which the bus writes; a stored message's headers are its publisher's.
async fn pull(client: &Client, stream: &str, consumer: &str) -> Result<Option<Message>, String> {
    let inbox = client.new_inbox();
    let mut subscription = client.subscribe(inbox.clone()).await.map_err(|error| error.to_string())?;
    let asked = client
        .publish_with_reply(jetstream::pull_subject(stream, consumer), inbox, jetstream::pull_request(PULL_WAIT).into())
        .await;
    let answer = match asked {
        Ok(()) => timeout(PULL_WAIT + PULL_ANSWERED_WITHIN, subscription.next()).await.ok().flatten(),
        Err(error) => {
            let _ = subscription.unsubscribe().await;
            return Err(error.to_string());
        }
    };
    let _ = subscription.unsubscribe().await;
    let Some(answer) = answer else { return Ok(None) };
    if answer.reply.as_ref().is_some_and(|reply| reply.starts_with(ACK_PREFIX)) {
        return Ok(Some(answer));
    }
    match answer.status.map(|status| status.as_u16()) {
        Some(PULL_EXPIRED) => Ok(None),
        status => {
            let code = status.map_or_else(|| "no status".to_owned(), |code| code.to_string());
            let said = format!("{code} {}", answer.description.unwrap_or_default());
            let said = said.split_whitespace().collect::<Vec<_>>().join(" ");
            Err(clipped(format!("the bus ended a pull on {stream}/{consumer} with {said}"), 300))
        }
    }
}

/// Handle one message of `lane` and tell the bus how it was settled. A settle the bus does not take is said, and the
/// message comes again after the consumer's ack wait.
async fn settled<H: Handler>(client: Client, message: Message, lane: Lane, handler: Arc<H>) {
    let Some(reply) = message.reply.clone() else { return };
    let Some(delivered) = jetstream::delivered(&reply) else {
        tracing::warn!(consumer = lane.consumer, reply = reply.as_str(), "bus_delivery_unreadable");
        return;
    };
    let delivery = Delivery {
        subject: message.subject.to_string(),
        body: message.payload.to_vec(),
        sequence: delivered.sequence,
        attempt: delivered.attempt,
    };
    let settlement = judged(
        &delivery,
        &lane,
        async |delivery: &Delivery| handler.handle(delivery).await,
        async |delivery: &Delivery, reason: &str| handler.park(delivery, reason).await,
    )
    .await;
    if let Err(error) = client.publish(reply, jetstream::settle_payload(&settlement).into()).await {
        tracing::warn!(consumer = lane.consumer, sequence = delivery.sequence, error = %error, "bus_delivery_unsettled");
    }
}

/// Hold a connection for as long as the task runs: open one, ask for its session again before the bus ends it, and
/// open another whenever the one held has closed for good.
async fn keep(settings: Arc<BusSettings>, held: watch::Sender<Option<Client>>, renewing: watch::Sender<Option<Instant>>) {
    let mut reported = false;
    loop {
        let (events_tx, mut events) = mpsc::unbounded_channel();
        let opened = timeout(OPEN_WITHIN, open(&settings, events_tx)).await;
        let client = match opened {
            Ok(Ok(client)) => client,
            failed => {
                if !reported {
                    let error = match failed {
                        Ok(Err(error)) => error,
                        _ => "the opening outran its time".to_owned(),
                    };
                    tracing::warn!(user = settings.user, error, "bus_not_connected");
                }
                reported = true;
                sleep(pause()).await;
                continue;
            }
        };
        reported = false;
        held.send_replace(Some(client.clone()));
        tracing::info!(user = settings.user, "bus_connected");
        kept(&settings, &client, &mut events, &renewing).await;
        held.send_replace(None);
        tracing::warn!(user = settings.user, "bus_connection_ended");
        let _ = timeout(CLOSE_WITHIN, client.drain()).await;
    }
}

/// Connect as `settings.user`, presenting the token the file holds at each connect, and ask for the user's own inbox.
async fn open(settings: &BusSettings, events: mpsc::UnboundedSender<Event>) -> Result<Client, String> {
    let token_file = settings.token_file.clone();
    let servers = settings.servers();
    let mut options = ConnectOptions::with_auth_callback(move |_nonce| {
        let token_file = token_file.clone();
        async move {
            let token = tokio::fs::read_to_string(&token_file)
                .await
                // The error's kind alone: nothing read from the file is said.
                .map_err(|error| AuthError::new(format!("the bus token file could not be read: {:?}", error.kind())))?;
            let mut auth = Auth::new();
            auth.token = Some(token.trim().to_owned());
            Ok(auth)
        }
    })
    .name(&settings.user)
    .custom_inbox_prefix(settings.inbox_prefix())
    .connection_timeout(CONNECT_TIMEOUT)
    .ping_interval(PING_INTERVAL)
    .max_reconnects(RECONNECT_ATTEMPTS * servers.len().max(1))
    .reconnect_delay_callback(|_| RECONNECT_WAIT)
    .request_timeout(None)
    .event_callback(move |event| {
        let events = events.clone();
        async move {
            let _ = events.send(event);
        }
    });
    if settings.encrypted() {
        options = options.require_tls(true).tls_first();
    }
    if let Some(ca_file) = &settings.ca_file {
        options = options.add_root_certificates(ca_file.clone());
    }
    let client = options.connect(servers).await.map_err(|error| clipped(error.to_string(), 300))?;
    // The user's own inbox is asked for as the connection opens: a session granted under a row without it would answer
    // as connected while every publish waited out its timeout. The bus's refusal is watched for as long as it is held.
    let mut inbox = client
        .subscribe(format!("{}.>", settings.inbox_prefix()))
        .await
        .map_err(|error| error.to_string())?;
    let _ = inbox.unsubscribe().await;
    Ok(client)
}

/// Wait until `client` has closed for good or the bus refused it the user's inbox, having it connect again each time
/// its session nears its end.
async fn kept(settings: &BusSettings, client: &Client, events: &mut mpsc::UnboundedReceiver<Event>, renewing: &watch::Sender<Option<Instant>>) {
    let mut began = Instant::now();
    let mut said: HashSet<String> = HashSet::new();
    let say = |said: &mut HashSet<String>, text: String, event: &str| {
        if said.len() < FAULTS_SAID && said.insert(text.clone()) {
            tracing::warn!(user = settings.user, error = text, "{event}");
        }
    };
    loop {
        tokio::select! {
            event = events.recv() => match event {
                None | Some(Event::Closed) => return,
                Some(Event::Connected) => {
                    began = Instant::now();
                    said.clear();
                    tracing::info!(user = settings.user, "bus_reconnected");
                }
                Some(Event::Disconnected) => say(&mut said, DISCONNECTED.to_owned(), "bus_disconnected"),
                Some(Event::ServerError(error)) => {
                    let text = clipped(error.to_string(), 300);
                    if text.to_lowercase().contains(SUBSCRIPTION_REFUSED) && text.to_lowercase().contains(&settings.inbox_prefix().to_lowercase()) {
                        tracing::warn!(user = settings.user, inbox = settings.inbox_prefix(), "bus_inbox_refused");
                        return;
                    }
                    say(&mut said, text, "bus_error");
                }
                Some(Event::ClientError(error)) => say(&mut said, clipped(error.to_string(), 300), "bus_error"),
                Some(_) => {}
            },
            () = sleep_until(began + settings.renew_after()) => {
                // Counted from now, so a connect the bus is slow to take is not asked for again on every turn.
                began = Instant::now();
                // The client's own dropping of its connection is no disconnection to report.
                said.insert(DISCONNECTED.to_owned());
                renewing.send_replace(Some(Instant::now() + RENEWED_WITHIN));
                tracing::info!(user = settings.user, "bus_session_renewing");
                let _ = client.force_reconnect().await;
            }
        }
    }
}

/// A random pause within [`PAUSE_SECONDS`].
fn pause() -> Duration {
    let (low, high) = PAUSE_SECONDS;
    let random = u64::from_le_bytes(uuid::Uuid::new_v4().as_bytes()[..8].try_into().unwrap_or([0; 8]));
    Duration::from_millis(low * 1000 + random % ((high - low) * 1000))
}
