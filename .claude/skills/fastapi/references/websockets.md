# WebSockets

Long-lived bidirectional connections. Same lifespan + DI + observability primitives as HTTP routes, but with specific rules for **authentication before `accept()`**, **heartbeat for dead-connection detection**, and **NATS JetStream for horizontal scaling** — never in-process state.

## Contents

- When (and when not) to use WebSockets
- Endpoint pattern
- Authentication MUST happen before `accept()`
- `ConnectionManager` for local connections
- Heartbeat — detecting dead connections
- Horizontal scaling — NATS JetStream, not in-process state
- OTel instrumentation (manual — not auto-instrumented)
- Anti-patterns

## When (and when not) to use WebSockets

| Use WebSockets | Prefer HTTP / SSE |
| -------------- | ----------------- |
| Truly bidirectional (chat, collaborative editing, live cursors) | Server-push only — use **SSE** ([`streaming.md`](streaming.md)) — simpler, works through corporate proxies, auto-reconnect built into `EventSource` |
| <1 s message latency required | <30 s freshness is OK — just poll |
| Long-lived stateful session per client | Stateless request-response |

Default to SSE for "push notifications" and "live dashboard" workloads. WebSockets buy bidirectionality at the cost of complexity (auth, heartbeats, scaling).

## Endpoint pattern

```python
# api/routes/ws.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/{room}")
async def chat_ws(websocket: WebSocket, room: str) -> None:
    # 1. authenticate (see next section)
    user = await authenticate_ws(websocket)
    if user is None:
        return                                  # auth helper already closed the socket

    # 2. accept
    await websocket.accept()

    # 3. register + handle
    try:
        await manager.connect(websocket, room=room, user_id=user.id)
        while True:
            payload = await websocket.receive_json()
            await handle_message(room, user, payload)
    except WebSocketDisconnect:
        pass                                    # normal close
    finally:
        await manager.disconnect(websocket, room=room)
```

`receive_json()` and `send_json()` are preferred over `receive_text` — Pydantic parsing lives one line away.

## Authentication MUST happen before `accept()`

The single most important rule. After `accept()` the socket is open and counted against your connection limit — rejecting later wastes the handshake, leaks file descriptors under abuse, and is harder to log.

```python
# core/ws_auth.py
from fastapi import WebSocket, status

from app.core.security import verify_jwt


async def authenticate_ws(websocket: WebSocket) -> User | None:
    token = websocket.query_params.get("token")
    if token is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    try:
        claims = verify_jwt(token)
    except InvalidTokenError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None

    return await load_user(claims["sub"])
```

**Token in query param, not header.** Browsers can't set custom headers on `new WebSocket(url)` — `?token=...` is the only portable channel. Keep the token short-lived (5-15 min) since it ends up in URL access logs at every proxy hop. JWT details: [`authn.md`](authn.md).

**Close codes:** `4001`/`4003` for auth failures (slowapi-style custom codes), or the standardized `1008` policy violation. Never just `return` without closing — leaves the client hanging until the TCP timeout.

## `ConnectionManager` for local connections

One instance per app, built in lifespan and stashed on `app.state` — same pattern as `app.state.db_engine` and `app.state.redis`. Holds *only the connections on this pod*; cross-pod broadcast goes through NATS.

```python
# core/ws_manager.py
import asyncio
from collections.abc import Iterable
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # room -> set of sockets in this process
        self._rooms: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, *, room: str, user_id: str) -> None:
        async with self._lock:
            self._rooms.setdefault(room, set()).add(ws)

    async def disconnect(self, ws: WebSocket, *, room: str) -> None:
        async with self._lock:
            self._rooms.get(room, set()).discard(ws)
            if not self._rooms.get(room):
                self._rooms.pop(room, None)

    def local_sockets(self, room: str) -> Iterable[WebSocket]:
        return tuple(self._rooms.get(room, ()))  # snapshot — iteration outside the lock

    async def send_local(self, room: str, payload: dict) -> None:
        for ws in self.local_sockets(room):
            try:
                await ws.send_json(payload)
            except Exception:
                # drop dead sockets; they'll surface as WebSocketDisconnect on the
                # other side and get cleaned up by their handler's finally block.
                pass
```

The lock is for the set mutations; sends happen on a snapshot so a slow client can't block the broadcast.

## Heartbeat — detecting dead connections

TCP doesn't notice a peer going away (laptop closed, NAT timeout) until the next attempted send. For sessions that need fast detection (chat presence, live cursors), heartbeat from the **server** side with a receive timeout:

```python
import asyncio

PING_INTERVAL = 30      # seconds
PING_TIMEOUT = 10       # how long we wait for the pong


async def chat_ws(websocket: WebSocket, room: str) -> None:
    user = await authenticate_ws(websocket)
    if user is None:
        return
    await websocket.accept()

    try:
        await manager.connect(websocket, room=room, user_id=user.id)
        while True:
            try:
                payload = await asyncio.wait_for(
                    websocket.receive_json(), timeout=PING_INTERVAL,
                )
                await handle_message(room, user, payload)
            except TimeoutError:
                # No traffic for PING_INTERVAL — check the pipe is alive.
                await websocket.send_json({"type": "ping"})
                # Next iteration's wait_for will catch the pong, or time out again.
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket, room=room)
```

The client echoes `{"type": "pong"}` on receiving ping. If you don't see traffic within `2 × PING_INTERVAL`, the socket is effectively dead — let the next `send` fail and trigger `WebSocketDisconnect`.

## Horizontal scaling — NATS JetStream, not in-process state

With N pods, an in-process `ConnectionManager` only knows its own clients. To broadcast to everyone, publish to NATS; each pod subscribes and fans out to local sockets.

```python
# services/chat.py
async def send_to_room(room: str, payload: dict) -> None:
    # Publish to NATS — every pod (including this one) receives it.
    await nats.publish(f"chat.room.{room}", json.dumps(payload).encode())
```

```python
# main.py — lifespan
async def lifespan(app: FastAPI):
    # ... db, redis, http, nats ...
    app.state.ws_manager = ConnectionManager()

    # One subscription per pod for ALL chat rooms — `*` wildcard.
    async def deliver(msg) -> None:
        room = msg.subject.removeprefix("chat.room.")
        payload = json.loads(msg.data)
        await app.state.ws_manager.send_local(room, payload)

    sub = await app.state.nats.subscribe("chat.room.*", cb=deliver)
    yield
    await sub.unsubscribe()
```

**Don't subscribe per room** — one wildcard subscription scales; thousands of micro-subscriptions don't. NATS pattern docs in `python-infrastructure`.

**Trace propagation across the queue** matters: inject context into the message headers when publishing, extract on the consumer side. See [`microservices.md`](microservices.md) § Trace context propagation.

## OTel instrumentation (manual)

`FastAPIInstrumentor` only auto-traces HTTP — WebSocket spans are on you. Two-level hierarchy: one **connection span** for the whole session, one **child span per message**.

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


async def chat_ws(websocket: WebSocket, room: str) -> None:
    user = await authenticate_ws(websocket)
    if user is None:
        return
    await websocket.accept()

    with tracer.start_as_current_span("ws.session") as session_span:
        session_span.set_attribute("ws.room", room)
        session_span.set_attribute("user.id", user.id)
        try:
            await manager.connect(websocket, room=room, user_id=user.id)
            while True:
                payload = await websocket.receive_json()
                with tracer.start_as_current_span("ws.message") as msg_span:
                    msg_span.set_attribute("ws.message.type", payload.get("type", "?"))
                    await handle_message(room, user, payload)
        except WebSocketDisconnect:
            pass
        finally:
            await manager.disconnect(websocket, room=room)
```

Logs inside either span automatically pick up `trace_id`/`span_id` via OTel's logging integration — no manual binding. Don't call `span.record_exception(...)` — deprecated; log the exception instead and OTel attaches it. See [`observability.md`](observability.md) + the `otel` skill.

Useful metrics worth emitting via the OTel meter API: `ws.connections.active` (up-down counter on connect/disconnect), `ws.messages.received` (counter), `ws.message.duration` (histogram). Keep cardinality bounded — `ws.room` is OK if room count is low, otherwise drop it from metric attributes (still fine on spans).

## Anti-patterns

| Pattern | Why it's wrong | Fix |
| ------- | -------------- | --- |
| `await websocket.accept()` before validating the token | Open socket for unauthenticated clients; leaks file descriptors under abuse; harder to log rejection | Authenticate first; close with `1008` before accept |
| Token in custom header (`Authorization: Bearer ...`) | Browsers can't set custom headers on `new WebSocket(url)` — works in curl, breaks in the actual product | Token in query param (`?token=...`), short-lived |
| In-process `ConnectionManager` as the broadcast surface | Pod A's clients never see Pod B's messages | Local manager for own sockets + NATS pub/sub for cross-pod fan-out |
| Redis pub/sub for cross-pod WebSocket broadcast | We standardize on NATS JetStream; one less broker to operate | NATS — see `python-infrastructure` |
| Polling `await websocket.receive_text()` with no timeout | Dead clients pin a coroutine forever; can't detect NAT timeout / closed laptop | `asyncio.wait_for(... , PING_INTERVAL)` + server-side ping |
| Calling `span.record_exception(e)` | Deprecated in OTel for new code | `log.exception(...)` inside the active span — OTel attaches `trace_id`/`span_id` automatically |
| Per-message subscription creation in NATS | O(messages) work for a sub that should be O(rooms) or O(1 wildcard) | One wildcard subscription per pod (`chat.room.*`) |
| WebSocket for "server pushes notifications" (one-way) | All the WS complexity for none of the bidirectional benefit | Server-Sent Events ([`streaming.md`](streaming.md)) — simpler, proxy-friendly, auto-reconnect |
| Storing per-user state on the WebSocket object | Lost on disconnect; can't be migrated when the user reconnects to a different pod | Persist in Postgres / Redis keyed by `user_id` |
| `@app.on_event("startup")` for connection-manager init | Deprecated; doesn't run cleanly in tests | Build in `lifespan`, stash on `app.state` — same pattern as the rest of the app |
| Sending unbounded message sizes | One large message blocks the event loop; trivial DoS vector | Configure Starlette's `WebSocket` size limit at the ASGI server (uvicorn `--ws-max-size`) |
| Auth check in route body after `accept()` then `close()` | Already counted against pod connection limit; the handshake bandwidth is spent | Always pre-accept (above) |
