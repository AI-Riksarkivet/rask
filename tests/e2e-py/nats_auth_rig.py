"""An operator-mode NATS cluster with test-only keys, and the clients the bus-auth proof drives through it.

The rig behind `test_nats_admits_each_client_only_to_its_own_subjects.py` ([[XC-078]] slice 2) and the maintenance
worker's queue module. It mints an operator, a SYS account, one APP account with JetStream and one user per
permission-table entry with `nsc`, and runs the NATS fixture's three `nats-server`s in operator mode with a MEMORY
resolver, clustered as the estate runs them. The chart's stream Job runs against them with its rendered script.
Every binary is the one the chart deploys: `.dagger/nats_auth.go` copies them out of those images into
`RASK_NATS_AUTH_TOOLS` and names each image in `images.json`, which the test compares with the render.

The keys are generated in the run's temp directory and nothing here prints a seed: a `Credential` holds its seed
privately, and its `repr` carries the user name, the public key, the JWT and the creds path, all but the last public.
The broker's trace log carries each client's CONNECT, which holds the user JWT and a signature over that run's nonce,
never the seed. Seeds reach disk only under the run's temp directory: nsc's keystore and creds files.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import nats
from nats.errors import NoRespondersError
from pydantic import BaseModel, ConfigDict, PrivateAttr

from lakehouse.lineage.signing import SigningKey
from tests.loopback import free_ports


if TYPE_CHECKING:
    from collections.abc import Mapping

    from nats.aio.client import Client as NatsClient


#: Where the lane put the binaries (`nsc`, `nats-server`, `job/nats`, `box/nats`) and `images.json`.
TOOLS_ENV = "RASK_NATS_AUTH_TOOLS"
#: The values file holding the permission table under `nats.auth.users`; the chart's own when unset.
VALUES_ENV = "RASK_NATS_AUTH_VALUES"

_OPERATOR = "rask-test"
_ACCOUNT = "APP"
_CREDS_JWT = re.compile(r"-----BEGIN NATS USER JWT-----\s+(\S+)\s+------END NATS USER JWT------")
_CREDS_SEED = re.compile(r"-----BEGIN USER NKEY SEED-----\s+(\S+)\s+------END USER NKEY SEED------")
#: `[TRC] <addr> - cid:7 - "<name>" - "<account>/APP/jwt:<user>" - <<- [PUB <subject> ...]`: a client's own op.
_CLIENT_OP = re.compile(r'cid:(\d+) - (?:"([^"]*)" - )?"[^"]*jwt:(U[A-Z2-7]+)" - <<- \[(PUB|HPUB|SUB) (\S+)')
#: The stream Job's rendered script names the bus it dials on its first line, `S="<url>"`.
_JOB_BUS = re.compile(r'S="(nats://[^"]+)"')
#: `[ERR] <addr> - cid:7 - "<name>" - "<account>/APP/jwt:<user>" - Publish Violation - Subject "<subject>"`.
_VIOLATION = re.compile(r'\[ERR\] .*?cid:(\d+) - (?:"([^"]*)" - )?"[^"]*jwt:(U[A-Z2-7]+)" - (Publish|Subscription) Violation - Subject "([^"]+)"')


class Tools(BaseModel):
    """The lane's binaries, and the image each was copied from."""

    model_config = ConfigDict(frozen=True)

    nsc: Path
    nats_server: Path
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
            job_cli=base / "job" / "nats",
            box_cli=base / "box" / "nats",
            images=json.loads((base / "images.json").read_text()),
        )
        missing = [str(p) for p in (tools.nsc, tools.nats_server, tools.job_cli, tools.box_cli) if not p.is_file()]
        if missing:
            raise LookupError(f"{TOOLS_ENV}={root} lacks {missing}")
        return tools


class Permissions(BaseModel):
    """One user's row of the chart's permission table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publish: tuple[str, ...] = ()
    subscribe: tuple[str, ...] = ()
    #: How long the user's credential is good for, as nsc's `--expiry` takes it (`1m` is its least); empty, for ever.
    expiry: str = ""

    @property
    def inbox(self) -> str:
        """The prefix the replies to this user's own connection arrive under: the inbox its subscribe grant gives it
        alone (``_INBOX_<user>``) where it has one, else the shared ``_INBOX``."""
        inboxes = {subject.removesuffix(".>") for subject in self.subscribe if subject.startswith("_INBOX") and subject.endswith(".>")}
        (inbox,) = inboxes - {"_INBOX"} or inboxes
        return inbox


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
        """The NKEY seed, for the store's stand-in, which holds it beside the JWT as the dev seed writes it."""
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

    _nsc(nsc, home, "add", "operator", "--name", _OPERATOR, "--sys")
    _nsc(nsc, home, "add", "account", "--name", _ACCOUNT)
    _nsc(nsc, home, "edit", "account", "--name", _ACCOUNT, "--js-disk-storage", "512M", "--js-mem-storage", "64M")
    credentials = {user: issue(nsc, home, user, permissions) for user, permissions in table.items()}
    return Mint(resolver_conf=_nsc(nsc, home, "generate", "config", "--mem-resolver", "--sys-account", "SYS"), credentials=credentials)


def issue(nsc: Path, home: Path, user: str, permissions: Permissions) -> Credential:
    """One more user of the account :func:`mint` made in ``home``. A running bus takes it at once: the server resolves the
    account that signed it, never its users.

    Raises:
        RuntimeError: nsc refused the user; its stderr carries no seed.
    """
    rows = [arg for subject in permissions.publish for arg in ("--allow-pub", subject)]
    rows += [arg for subject in permissions.subscribe for arg in ("--allow-sub", subject)]
    rows += ["--expiry", permissions.expiry] if permissions.expiry else []
    _nsc(nsc, home, "add", "user", "--account", _ACCOUNT, "--name", user, *rows)
    return Credential.from_creds(user, home / "creds" / _OPERATOR / _ACCOUNT / f"{user}.creds")


def _nsc(nsc: Path, home: Path, *args: str) -> str:
    done = subprocess.run([str(nsc), "-H", str(home), *args], capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode:
        raise RuntimeError(f"nsc {' '.join(args[:2])} failed: {done.stderr.strip()[-400:]}")
    return done.stdout


#: The NATS fixture's cluster size (`deploy/fixtures/nats/chart/values.yaml`, `config.cluster.replicas`), and so the
#: copies of each stream the chart declares at its default `nats.streamReplicas`.
_NODES = 3

#: How much of a server's log a failed start quotes, traces left out: enough for the startup lines and a fatal error, or
#: a crash's stack, which a server writes last.
_LOG_END_LINES = 30


class Broker:
    """The NATS fixture's bus: `_NODES` `nats-server`s in operator mode (MEMORY resolver, JetStream on, every client op
    traced), routed into one cluster, so each stream the chart declares keeps a copy on every server, as it does live.

    A copied stream refuses some puts unlike one server: a put expecting its subject to hold nothing is refused with 10164
    while another put for that subject is still being agreed, and with 10071 once that one is stored, where one server
    answers 10071 to both (measured on nats-server 2.15.2, 2026-11-09: 40 of 40 racing puts). Every client connects to the
    first server, and the cluster advertises no other (`no_advertise`), so a client stays on the connection the test gave
    it and a `Link` in front of that server cuts it. The routes run on loopback without the fixture's route credential,
    which this rig does not prove.
    """

    def __init__(self, server: Path, mint: Mint, workdir: Path) -> None:
        self._server, self._mint, self._dir = server, mint, workdir
        self._processes: list[subprocess.Popen[bytes]] = []
        ports = free_ports(2 * _NODES)
        self._ports, self._routes = ports[:_NODES], ports[_NODES:]
        self.port = self._ports[0]
        self.url = f"nats://127.0.0.1:{self.port}"

    def start(self, timeout: float = 30.0) -> None:
        """Start every server, and return once each knows the cluster's JetStream metadata leader, from when a stream can
        be declared (measured: about 6 s after start).

        Raises:
            RuntimeError: A server exited first. The message names it, its exit code and the end of its log.
            TimeoutError: A server knew no leader within `timeout` seconds. The message names it and the end of its log.
        """
        self._dir.mkdir(parents=True)
        routes = ", ".join(f"nats://127.0.0.1:{route}" for route in self._routes)
        for node, (port, route) in enumerate(zip(self._ports, self._routes, strict=True)):
            home = self._node_dir(node)
            home.mkdir()
            (home / "resolver.conf").write_text(self._mint.resolver_conf)
            (home / "server.conf").write_text(
                f"listen: 127.0.0.1:{port}\n"
                f"server_name: rask-test-{node}\n"
                f'jetstream: {{ store_dir: "{home / "js"}", max_file_store: 1G, max_memory_store: 64M }}\n'
                f"cluster: {{ name: rask-test, listen: 127.0.0.1:{route}, routes: [{routes}], no_advertise: true }}\n"
                "trace: true\n"
                "logtime: true\n"
                "include resolver.conf\n"
            )
            with (home / "nats-server.log").open("wb") as log:
                self._processes.append(subprocess.Popen([str(self._server), "-c", str(home / "server.conf")], stdout=log, stderr=subprocess.STDOUT))  # noqa: S603
        deadline = time.monotonic() + timeout
        while waiting := [node for node in range(_NODES) if "metadata leader" not in self._log(node)]:
            if exited := [node for node, process in enumerate(self._processes) if process.poll() is not None]:
                raise RuntimeError(f"a server exited before the cluster elected a JetStream metadata leader:\n{self._log_ends(exited)}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"the cluster elected no JetStream metadata leader within {timeout} s:\n{self._log_ends(waiting)}")
            time.sleep(0.1)

    def stop(self) -> None:
        running = [process for process in self._processes if process.poll() is None]
        for process in running:
            process.terminate()
        for process in running:
            process.wait(timeout=15)

    def log_text(self) -> str:
        """Every server's log, the first's first: a client's calls and refusals are logged by the server it is on."""
        return "".join(self._log(node) for node in range(_NODES))

    def _node_dir(self, node: int) -> Path:
        return self._dir / f"node-{node}"

    def _log_ends(self, nodes: list[int]) -> str:
        """Each server's name, its exit code or that it runs, and the end of its own log with the traces left out."""
        ends = []
        for node in nodes:
            code = self._processes[node].poll()
            state = "still running" if code is None else f"exit code {code}"
            said = [line for line in self._log(node).splitlines() if " [TRC] " not in line]
            ends.append("\n".join([f"rask-test-{node} ({state}):", *said[-_LOG_END_LINES:]]))
        return "\n".join(ends)

    def _log(self, node: int) -> str:
        path = self._node_dir(node) / "nats-server.log"
        return path.read_text(errors="replace") if path.exists() else ""


class Link:
    """A TCP relay between one client and the broker that the test can cut, as a network fault or the restart of the
    server a client is attached to looks to the server that holds its consumer: the connection drops, the server stays
    up, and the client's interest is gone until it reconnects.

    It can also lose one subject's traffic while the rest flows (:meth:`drop`): a `PUB` or `HPUB` the client sends on a
    subject a dropped pattern matches never reaches the broker, as a message the network lost, so a request on it goes
    unanswered while the connection, and every other subject, stays up. Only the client's publishes are read for it: the
    protocol frames each one as a line naming its subject and payload size, then the payload.

    Or it can slow the client's side of the path (:meth:`lag`), as a slow route to the server holding a stream's leader
    does: every frame the client sends, a pull and the unsubscribe that gives it up alike, reaches the broker that many
    seconds after it was sent, in order, while what the broker sends comes back at once."""

    def __init__(self, target_port: int) -> None:
        self._target_port = target_port
        #: The port the relay listens on, the one it was given on its first start, kept across a cut.
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._dropped: set[str] = set()
        self._lag = 0.0

    @property
    def url(self) -> str:
        if self.port is None:
            raise RuntimeError("the link has not started, so it listens on no port yet")
        return f"nats://127.0.0.1:{self.port}"

    @property
    def down(self) -> bool:
        """Whether the cut stands: no new connection is taken, and every relayed one has closed."""
        return self._server is None and not self._writers

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._relay, "127.0.0.1", self.port or 0, limit=_LINE_LIMIT)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def cut(self, down: float) -> None:
        """Drop every relayed connection and refuse new ones for `down` seconds."""
        await self.stop()
        await asyncio.sleep(down)
        await self.start()

    def drop(self, pattern: str) -> None:
        """Lose every publish the client sends on a subject `pattern` matches (NATS wildcards), until :meth:`flow`."""
        self._dropped.add(pattern)

    def lag(self, seconds: float) -> None:
        """Deliver every frame the client sends `seconds` after it was sent, until :meth:`flow`."""
        self._lag = seconds

    def flow(self) -> None:
        """Pass every publish on again, at once."""
        self._dropped.clear()
        self._lag = 0.0

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for writer in list(self._writers):
            writer.close()

    async def _relay(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        broker_reader, broker_writer = await asyncio.open_connection("127.0.0.1", self._target_port, limit=_LINE_LIMIT)
        self._writers |= {client_writer, broker_writer}

        loop = asyncio.get_running_loop()
        # The client's frames, each with when it was read, so a lag delays every frame by the same span rather than adding up.
        sent: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()

        async def read_client() -> None:
            try:
                while frame := await self._client_frame(client_reader):
                    sent.put_nowait((loop.time(), frame))
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                sent.put_nowait((loop.time(), b""))

        async def to_broker() -> None:
            try:
                while (frame := await sent.get())[1]:
                    await asyncio.sleep(max(0.0, frame[0] + self._lag - loop.time()))
                    broker_writer.write(frame[1])
                    await broker_writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                broker_writer.close()
                self._writers.discard(broker_writer)

        async def to_client() -> None:
            try:
                while chunk := await broker_reader.read(65536):
                    client_writer.write(chunk)
                    await client_writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                client_writer.close()
                self._writers.discard(client_writer)

        await asyncio.gather(read_client(), to_broker(), to_client())

    async def _client_frame(self, reader: asyncio.StreamReader) -> bytes:
        """The client's next frame as it goes on to the broker: a protocol line, with its payload for a publish, or nothing
        for a publish a dropped pattern matches (and the next frame in its place). Empty once the client has closed."""
        while line := await reader.readline():
            words = line.split()
            if not words or words[0].upper() not in (b"PUB", b"HPUB"):
                return line
            payload = await reader.readexactly(int(words[-1]) + 2)
            if not any(matches(pattern, words[1].decode()) for pattern in self._dropped):
                return line + payload
        return b""


#: The longest protocol line the relay reads: a client's CONNECT carries its user JWT.
_LINE_LIMIT = 1 << 20


def matches(pattern: str, subject: str) -> bool:
    """Whether ``subject`` matches the NATS subject ``pattern``: ``*`` is one token, a final ``>`` one or more. A
    ``subject`` that is itself a pattern (a subscription's) matches token for token."""
    wanted, tokens = pattern.split("."), subject.split(".")
    for index, token in enumerate(wanted):
        if token == ">":
            return len(tokens) > index
        if index >= len(tokens) or token not in ("*", tokens[index]):
            return False
    return len(tokens) == len(wanted)


class Violation(BaseModel):
    """One permission refusal the broker logged."""

    model_config = ConfigDict(frozen=True)

    user_key: str
    connection: str
    kind: str
    subject: str


def violations(log: str) -> list[Violation]:
    return [Violation(connection=m[2] or f"cid:{m[1]}", user_key=m[3], kind=m[4], subject=m[5]) for m in _VIOLATION.finditer(log)]


class Call(BaseModel):
    """One operation a client sent the broker: a publish (`PUB`, `HPUB`) or a subscription (`SUB`), on its subject."""

    model_config = ConfigDict(frozen=True)

    kind: str
    subject: str


def client_calls(log: str) -> dict[str, set[Call]]:
    """Every publish and subscription each authenticated user sent, keyed by the user's public key."""
    seen: dict[str, set[Call]] = {}
    for m in _CLIENT_OP.finditer(log):
        seen.setdefault(m[3], set()).add(Call(kind="subscribe" if m[4] == "SUB" else "publish", subject=m[5]))
    return seen


def unused_grants(permissions: Permissions, calls: set[Call]) -> list[str]:
    """Each of ``permissions``' grants that none of ``calls`` falls under: a row broader than what its user does."""
    unused = [f"publish {grant}" for grant in permissions.publish if not any(c.kind == "publish" and matches(grant, c.subject) for c in calls)]
    return unused + [f"subscribe {grant}" for grant in permissions.subscribe if not any(c.kind == "subscribe" and matches(grant, c.subject) for c in calls)]


async def ignore_refusal(_: Exception) -> None:
    """nats-py's `error_cb`: the broker's log, read once it stops, is what judges every refusal."""


class Client:
    """A nats-py connection authenticated as one user."""

    def __init__(self, nc: NatsClient, answer_within: float) -> None:
        self.nc = nc
        self.js = nc.jetstream()
        self._answer_within = answer_within

    @classmethod
    async def connect(cls, url: str, credential: Credential, name: str, inbox: str = "_INBOX", answer_within: float = 3.0) -> Self:
        """Connect as ``credential``'s user, its replies arriving under ``inbox``: the prefix its subscribe grant admits.

        ``answer_within`` is how long each of its requests waits. A refused request is never answered, so a probe that
        expects refusals waits briefly; a client every call of which is granted waits as long as the service's own JetStream
        calls do (`maintenance.services.queue`'s 10 s), since on the cluster a stream's leader answers one route away, and
        a lane sharing its host with another missed 3 s (measured 2026-11-09).
        """
        nc = await nats.connect(
            url,
            name=name,
            error_cb=ignore_refusal,
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=5,
            user_jwt_cb=credential.jwt_bytes,
            signature_cb=credential.sign_nonce,
            inbox_prefix=inbox,
        )
        return cls(nc, answer_within)

    async def settle(self) -> None:
        """Return once the server has processed everything this client sent.

        TWO round trips: nats-py 2.15 writes a flush's PING ahead of publishes still in its buffer, so the first PONG
        can come back before the server reads them; the second PING leaves after them. Measured: with one flush,
        about half of a burst of refused publishes were answered after it returned.
        """
        await self.nc.flush()
        await self.nc.flush()

    async def request(self, subject: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """A JetStream API request; None when no answer came (a refused request is never answered)."""
        try:
            reply = await self.nc.request(subject, json.dumps(body or {}).encode(), timeout=self._answer_within)
        except (TimeoutError, NoRespondersError):
            return None
        return json.loads(reply.data)

    async def close(self) -> None:
        await self.nc.close()


def stream_job_script(docs: tuple[dict[str, Any], ...], url: str, *, retire: bool = False) -> str:
    """The script the chart's stream Job runs, as the render writes it, dialling ``url`` in place of the bus it names:
    the declare phase (`<release>-nats-stream-r<rev>`) or, with ``retire``, the retire phase
    (`<release>-nats-stream-retire-r<rev>`).

    Raises:
        LookupError: The render holds no such Job, or its script names no bus.
    """
    phase = re.compile(r"-nats-stream-retire-r\d+$" if retire else r"-nats-stream-r\d+$")
    jobs = [d for d in docs if d.get("kind") == "Job" and phase.search(d["metadata"]["name"])]
    if len(jobs) != 1:
        raise LookupError(f"the render holds {len(jobs)} Jobs named *{phase.pattern}")
    script = jobs[0]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    rendered = _JOB_BUS.search(script)
    if rendered is None:
        raise LookupError(f"the {jobs[0]['metadata']['name']} script names no bus")
    return script.replace(rendered.group(1), url)


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
    """The stream Job's rendered script (:func:`stream_job_script`), with its nats CLI on PATH and the creds of
    ``credential``'s user in `NATS_CREDS`."""
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"HOME": str(home), "PATH": f"{cli.parent}:/usr/bin:/bin", "NATS_CREDS": str(credential.creds_file)},
        stdin=subprocess.DEVNULL,
        check=False,
    )
