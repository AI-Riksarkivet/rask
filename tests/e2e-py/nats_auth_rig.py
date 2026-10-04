"""An operator-mode NATS broker with test-only keys, and the clients the bus-auth proof drives through it.

The rig behind `test_nats_admits_each_client_only_to_its_own_subjects.py` ([[XC-078]] slice 2). It mints an
operator, a SYS account, one APP account with JetStream and one user per permission-table entry with `nsc`, runs
`nats-server` in operator mode with a MEMORY resolver, and runs `daprd` against it with the chart's own Components.
Every binary is the one the chart deploys: `.dagger/nats_auth.go` copies them out of those images into
`RASK_NATS_AUTH_TOOLS` and names each image in `images.json`, which the test compares with the render.

The keys are generated in the run's temp directory and nothing here prints a seed: a `Credential` holds its seed
privately, and its `repr` carries the user name, the public key, the JWT and the creds path, all but the last public.
The broker's trace log carries each client's CONNECT, which holds the user JWT and a signature over that run's nonce,
never the seed. Seeds reach disk only under the run's temp directory: nsc's keystore and creds files, and the
sidecars' resources directories.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import httpx
import nats
import yaml
from nats.errors import NoRespondersError
from pydantic import BaseModel, ConfigDict, PrivateAttr

from lineage_kit.signing import SigningKey


if TYPE_CHECKING:
    from collections.abc import Mapping

    from nats.aio.client import Client as NatsClient


#: Where the lane put the binaries (`nsc`, `nats-server`, `daprd`, `job/nats`, `box/nats`) and `images.json`.
TOOLS_ENV = "RASK_NATS_AUTH_TOOLS"
#: The values file holding the permission table under `nats.auth.users`; the chart's own when unset.
VALUES_ENV = "RASK_NATS_AUTH_VALUES"

_OPERATOR = "rask-test"
_ACCOUNT = "APP"
_CREDS_JWT = re.compile(r"-----BEGIN NATS USER JWT-----\s+(\S+)\s+------END NATS USER JWT------")
_CREDS_SEED = re.compile(r"-----BEGIN USER NKEY SEED-----\s+(\S+)\s+------END USER NKEY SEED------")
#: `[TRC] <addr> - cid:7 - "<name>" - "<account>/APP/jwt:<user>" - <<- [PUB <subject> ...]`: a client's own op.
_CLIENT_OP = re.compile(r'cid:(\d+) - (?:"([^"]*)" - )?"[^"]*jwt:(U[A-Z2-7]+)" - <<- \[(PUB|HPUB|SUB) (\S+)')
#: `[ERR] <addr> - cid:7 - "<name>" - "<account>/APP/jwt:<user>" - Publish Violation - Subject "<subject>"`.
_VIOLATION = re.compile(r'\[ERR\] .*?cid:(\d+) - (?:"([^"]*)" - )?"[^"]*jwt:(U[A-Z2-7]+)" - (Publish|Subscription) Violation - Subject "([^"]+)"')


class Tools(BaseModel):
    """The lane's binaries, and the image each was copied from."""

    model_config = ConfigDict(frozen=True)

    nsc: Path
    nats_server: Path
    daprd: Path
    #: The nats CLI of the stream Job's image; the Job's script calls it as `nats`, so it sits alone in its directory.
    job_cli: Path
    #: The nats CLI of the nats-box image, the one an operator reads the broker with.
    box_cli: Path
    images: dict[str, str]

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self:
        """Read the tool directory `.dagger/nats_auth.go` assembled.

        Raises:
            LookupError: `RASK_NATS_AUTH_TOOLS` is unset or a binary is missing; the lane provides them.
        """
        root = environ.get(TOOLS_ENV)
        if not root:
            raise LookupError(f"{TOOLS_ENV} is unset: this suite runs from `dagger call nats-auth`, which provides the binaries")
        base = Path(root)
        tools = cls(
            nsc=base / "nsc",
            nats_server=base / "nats-server",
            daprd=base / "daprd",
            job_cli=base / "job" / "nats",
            box_cli=base / "box" / "nats",
            images=json.loads((base / "images.json").read_text()),
        )
        missing = [str(p) for p in (tools.nsc, tools.nats_server, tools.daprd, tools.job_cli, tools.box_cli) if not p.is_file()]
        if missing:
            raise LookupError(f"{TOOLS_ENV}={root} lacks {missing}")
        return tools


class Permissions(BaseModel):
    """One user's row of the chart's permission table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publish: tuple[str, ...] = ()
    subscribe: tuple[str, ...] = ()


def permission_table(values: Path) -> dict[str, Permissions]:
    """`nats.auth.users` from a values file.

    Raises:
        LookupError: the file declares no table.
    """
    users = (((yaml.safe_load(values.read_text()) or {}).get("nats") or {}).get("auth") or {}).get("users")
    if not users:
        raise LookupError(f"{values} declares no nats.auth.users table")
    return {name: Permissions.model_validate(row) for name, row in users.items()}


class Credential(BaseModel):
    """One user's JWT and NKEY seed, as nsc wrote them into the user's creds file."""

    model_config = ConfigDict(frozen=True)

    user: str
    public_key: str
    jwt: str
    creds_file: Path
    _seed: str = PrivateAttr()

    @classmethod
    def from_creds(cls, user: str, creds_file: Path) -> Self:
        text = creds_file.read_text()
        jwt_match, seed_match = _CREDS_JWT.search(text), _CREDS_SEED.search(text)
        if jwt_match is None or seed_match is None:
            raise ValueError(f"{creds_file} is not an nsc creds file")
        key = SigningKey.from_seed(seed_match.group(1))
        credential = cls(user=user, public_key=key.public_nkey, jwt=jwt_match.group(1), creds_file=creds_file)
        credential._seed = seed_match.group(1)
        return credential

    def seed(self) -> str:
        """The NKEY seed, for a Component's inline `seedKey`."""
        return self._seed

    def jwt_bytes(self) -> bytes:
        """nats-py's `user_jwt_cb`."""
        return self.jwt.encode()

    def sign_nonce(self, nonce: str) -> bytes:
        """nats-py's `signature_cb`: the server's nonce signed with the user's seed, as nats-py's own creds path does."""
        return base64.b64encode(SigningKey.from_seed(self._seed).sign(nonce.encode()))


class Mint(BaseModel):
    """The operator config a server needs (all public) and one credential per table user."""

    model_config = ConfigDict(frozen=True)

    resolver_conf: str
    credentials: dict[str, Credential]

    def user_of(self, public_key: str) -> str:
        return next((c.user for c in self.credentials.values() if c.public_key == public_key), public_key)


def mint(nsc: Path, table: dict[str, Permissions], home: Path) -> Mint:
    """Operator + SYS (`--sys`), one APP account with JetStream inside the chart's 1Gi store, one user per row.

    Raises:
        RuntimeError: an nsc command failed; its stderr carries no seed.
    """

    def run(*args: str) -> str:
        done = subprocess.run([str(nsc), "-H", str(home), *args], capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode:
            raise RuntimeError(f"nsc {' '.join(args[:2])} failed: {done.stderr.strip()[-400:]}")
        return done.stdout

    run("add", "operator", "--name", _OPERATOR, "--sys")
    run("add", "account", "--name", _ACCOUNT)
    run("edit", "account", "--name", _ACCOUNT, "--js-disk-storage", "512M", "--js-mem-storage", "64M")
    for user, permissions in table.items():
        rows = [arg for subject in permissions.publish for arg in ("--allow-pub", subject)]
        rows += [arg for subject in permissions.subscribe for arg in ("--allow-sub", subject)]
        run("add", "user", "--account", _ACCOUNT, "--name", user, *rows)
    creds = home / "creds" / _OPERATOR / _ACCOUNT
    return Mint(
        resolver_conf=run("generate", "config", "--mem-resolver", "--sys-account", "SYS"),
        credentials={user: Credential.from_creds(user, creds / f"{user}.creds") for user in table},
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Broker:
    """One `nats-server` in operator mode (MEMORY resolver, JetStream on, every client op traced)."""

    def __init__(self, server: Path, mint: Mint, workdir: Path) -> None:
        self._server, self._mint, self._dir = server, mint, workdir
        self._process: subprocess.Popen[bytes] | None = None
        self.port = free_port()
        self.url = f"nats://127.0.0.1:{self.port}"
        self.log_path = workdir / "nats-server.log"

    def start(self, timeout: float = 20.0) -> None:
        self._dir.mkdir(parents=True)
        (self._dir / "resolver.conf").write_text(self._mint.resolver_conf)
        (self._dir / "server.conf").write_text(
            f"listen: 127.0.0.1:{self.port}\n"
            "server_name: rask-test\n"
            f'jetstream: {{ store_dir: "{self._dir / "js"}", max_file_store: 1G, max_memory_store: 64M }}\n'
            "trace: true\n"
            "logtime: true\n"
            "include resolver.conf\n"
        )
        with self.log_path.open("wb") as log:
            self._process = subprocess.Popen([str(self._server), "-c", str(self._dir / "server.conf")], stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
        deadline = time.monotonic() + timeout
        while "Server is ready" not in self.log_text():
            if self._process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"nats-server did not start: {self.log_text()[-1500:]}")
            time.sleep(0.1)

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            self._process.wait(timeout=15)

    def log_text(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""


class Violation(BaseModel):
    """One permission refusal the broker logged."""

    model_config = ConfigDict(frozen=True)

    user_key: str
    connection: str
    kind: str
    subject: str


def violations(log: str) -> list[Violation]:
    return [Violation(connection=m[2] or f"cid:{m[1]}", user_key=m[3], kind=m[4], subject=m[5]) for m in _VIOLATION.finditer(log)]


def client_subjects(log: str) -> dict[tuple[str, str], set[str]]:
    """Every subject each authenticated connection published to or subscribed to, keyed (user key, connection)."""
    seen: dict[tuple[str, str], set[str]] = {}
    for m in _CLIENT_OP.finditer(log):
        seen.setdefault((m[3], m[2] or f"cid:{m[1]}"), set()).add(m[5])
    return seen


def call_template(subject: str, consumers: set[str]) -> str:
    """A subject with its per-run parts folded: inboxes, ack-reply tails, and consumer names nobody chose."""
    if subject.startswith("_INBOX."):
        return "_INBOX.>"
    tokens = subject.split(".")
    if subject.startswith("$JS.ACK.") and len(tokens) > 4:
        consumer = tokens[3] if tokens[3] in consumers else "*"
        return f"$JS.ACK.{tokens[2]}.{consumer}.>"
    if subject.startswith("$JS.API.CONSUMER.INFO.") and len(tokens) == 6 and tokens[5] not in consumers:
        return ".".join([*tokens[:5], "*"])
    return subject


class Delivery(BaseModel):
    """One request daprd made to the app."""

    model_config = ConfigDict(frozen=True)

    route: str
    body: dict[str, Any]


class SubscriberApp:
    """The smallest Dapr app: `GET /dapr/subscribe` declares `subscriptions`, every POST is recorded.

    A route in `failing` answers 500, which is how a delivery is made to fail so daprd dead-letters it.
    """

    def __init__(self, subscriptions: list[dict[str, str]], failing: frozenset[str] = frozenset()) -> None:
        self.deliveries: list[Delivery] = []
        lock = threading.Lock()
        app = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's own parameter name
                return

            def _answer(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 — the stdlib's dispatch name
                if self.path == "/dapr/subscribe":
                    self._answer(200, json.dumps(subscriptions).encode())
                else:
                    self._answer(404)

            def do_POST(self) -> None:  # noqa: N802 — the stdlib's dispatch name
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with lock:
                    app.deliveries.append(Delivery(route=self.path, body=json.loads(raw or b"{}")))
                if self.path in failing:
                    self._answer(500)
                else:
                    self._answer(200, b'{"status":"SUCCESS"}')

        self.port = free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def markers(self, route: str) -> list[str]:
        return [str((d.body.get("data") or {}).get("marker")) for d in list(self.deliveries) if d.route == route and isinstance(d.body.get("data"), dict)]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def sidecar_component(rendered: dict[str, Any], url: str, credential: Credential) -> dict[str, Any]:
    """The chart's Component with its connection rows inline: the broker's URL and the user's JWT and seed.

    The chart names `jwt` and `seedKey` through `secretKeyRef` on a store this rig does not run, so the rows are
    replaced rather than resolved, and the Component-level `auth` that names the store goes with them.
    """
    component = copy.deepcopy(rendered)
    kept = [row for row in component["spec"]["metadata"] if row["name"] not in {"natsURL", "jwt", "seedKey", "token"}]
    component["spec"]["metadata"] = [
        {"name": "natsURL", "value": url},
        {"name": "jwt", "value": credential.jwt},
        {"name": "seedKey", "value": credential.seed()},
        *kept,
    ]
    component.pop("auth", None)
    return component


class Sidecar:
    """One standalone `daprd` serving `app` with `components`, every port its own."""

    def __init__(self, daprd: Path, app_id: str, components: list[dict[str, Any]], app: SubscriberApp, workdir: Path) -> None:
        self._daprd, self.app_id, self._components, self.app, self._dir = daprd, app_id, components, app, workdir
        self.http_port = free_port()
        self.log_path = workdir / "daprd.log"
        self._process: subprocess.Popen[bytes] | None = None

    async def start(self, timeout: float = 45.0) -> None:
        resources = self._dir / "resources"
        resources.mkdir(parents=True)
        for component in self._components:
            (resources / f"{component['metadata']['name']}.yaml").write_text(yaml.safe_dump(component))
        argv = [
            str(self._daprd),
            *("--app-id", self.app_id, "--app-port", str(self.app.port), "--app-protocol", "http"),
            *("--dapr-http-port", str(self.http_port), "--dapr-grpc-port", str(free_port())),
            *("--dapr-internal-grpc-port", str(free_port()), "--dapr-public-port", str(free_port())),
            *("--enable-metrics=false", "--resources-path", str(resources), "--log-level", "debug"),
        ]
        with self.log_path.open("wb") as log:
            self._process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                if self._process.poll() is not None:
                    raise RuntimeError(f"daprd {self.app_id} exited: {self.log_text()[-1500:]}")
                try:
                    if (await client.get(f"http://127.0.0.1:{self.http_port}/v1.0/healthz")).status_code == 204:
                        return
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"daprd {self.app_id} was not healthy within {timeout}s: {self.log_text()[-1500:]}")
                await asyncio.sleep(0.2)

    async def publish(self, pubsub: str, topic: str, data: dict[str, str]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(f"http://127.0.0.1:{self.http_port}/v1.0/publish/{pubsub}/{topic}", json=data)

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            self._process.wait(timeout=15)

    def log_text(self) -> str:
        return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""


async def ignore_refusal(_: Exception) -> None:
    """nats-py's `error_cb`: the broker's log, read once it stops, is what judges every refusal."""


class Client:
    """A nats-py connection authenticated as one user."""

    def __init__(self, nc: NatsClient) -> None:
        self.nc = nc
        self.js = nc.jetstream()

    @classmethod
    async def connect(cls, url: str, credential: Credential, name: str) -> Self:
        nc = await nats.connect(
            url,
            name=name,
            error_cb=ignore_refusal,
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=5,
            user_jwt_cb=credential.jwt_bytes,
            signature_cb=credential.sign_nonce,
        )
        return cls(nc)

    async def settle(self) -> None:
        """Return once the server has processed everything this client sent.

        TWO round trips: nats-py 2.15 writes a flush's PING ahead of publishes still in its buffer, so the first PONG
        can come back before the server reads them; the second PING leaves after them. Measured: with one flush,
        about half of a burst of refused publishes were answered after it returned.
        """
        await self.nc.flush()
        await self.nc.flush()

    async def request(self, subject: str, body: dict[str, Any] | None = None, timeout: float = 3.0) -> dict[str, Any] | None:
        """A JetStream API request; None when no answer came (a refused request is never answered)."""
        try:
            reply = await self.nc.request(subject, json.dumps(body or {}).encode(), timeout=timeout)
        except (TimeoutError, NoRespondersError):
            return None
        return json.loads(reply.data)

    async def close(self) -> None:
        await self.nc.close()


def go_duration_ns(value: str) -> int:
    """`720s`, `1h30m`, `500ms` as Dapr's metadata decoder reads them, in nanoseconds."""
    units = {"h": 3_600_000_000_000, "m": 60_000_000_000, "s": 1_000_000_000, "ms": 1_000_000}
    parts = re.findall(r"(\d+)(ms|h|m|s)", value)
    if not parts or "".join(n + u for n, u in parts) != value:
        raise ValueError(f"not a duration: {value!r}")
    return sum(int(n) * units[u] for n, u in parts)


def dapr_consumer_config(metadata: dict[str, str], topic: str, deliver_subject: str) -> dict[str, Any]:
    """The ConsumerConfig Dapr 1.18.1's pubsub.jetstream builds from a Component (components-contrib v1.18.0).

    Subscribe sets the deliver subject to a fresh inbox, the durable and deliver group from `durableName` and
    `queueGroupName`, the deliver policy (`all` unless named), explicit acks, the redelivery numbers when set,
    and the topic as the filter.
    """
    config: dict[str, Any] = {
        "deliver_subject": deliver_subject,
        "deliver_policy": metadata.get("deliverPolicy") or "all",
        "ack_policy": metadata.get("ackPolicy") or "explicit",
        "replay_policy": "instant",
        "filter_subject": topic,
    }
    if metadata.get("durableName"):
        config["durable_name"] = metadata["durableName"]
    if metadata.get("queueGroupName"):
        config["deliver_group"] = metadata["queueGroupName"]
    if metadata.get("ackWait"):
        config["ack_wait"] = go_duration_ns(metadata["ackWait"])
    if metadata.get("maxDeliver"):
        config["max_deliver"] = int(metadata["maxDeliver"])
    if metadata.get("backOff"):
        config["backoff"] = [go_duration_ns(step) for step in metadata["backOff"].split(",")]
    if metadata.get("maxAckPending"):
        config["max_ack_pending"] = int(metadata["maxAckPending"])
    return config


def component_metadata(component: dict[str, Any]) -> dict[str, str]:
    return {row["name"]: str(row["value"]) for row in component["spec"]["metadata"] if "value" in row}


def run_cli(cli: Path, args: list[str], credential: Credential, url: str, home: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """One nats CLI command as `credential`'s user."""
    return subprocess.run(  # noqa: S603
        [str(cli), "--server", url, "--creds", str(credential.creds_file), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def run_stream_job(cli: Path, script: str, credential: Credential, home: Path, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    """The stream Job's rendered script, with its nats CLI on PATH and the admin user's creds in `NATS_CREDS`."""
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"HOME": str(home), "PATH": f"{cli.parent}:/usr/bin:/bin", "NATS_CREDS": str(credential.creds_file)},
        check=False,
    )
