"""An operator-mode NATS cluster with test-only keys, the signer that grants each pod a session, and the clients the
bus-auth proof drives through it.

The rig behind `test_nats_admits_each_client_only_to_its_own_subjects.py` ([[XC-078]], [[XC-199]]) and the maintenance
worker's queue module. It mints, with `nsc`, an operator, a SYS account, an APP account with JetStream and an AUTH
account, and runs the NATS fixture's three `nats-server`s in operator mode with a MEMORY resolver, clustered as the
estate runs them, TLS on the client port from a CA of the run's own, and the AUTH account's bearer sentinel as the
servers' `default_sentinel` (docs/architecture/bus-credentials-findings.md § 4).

A bus user is a SESSION USER or a STATIC USER (`bus_users`). A session user is a row of the permission table the chart
renders for the signer (the ConfigMap `<release>-nats-permissions`); its pod holds no bus credential: it presents only
its ServiceAccount token, and the signer, two `bus-signer` processes on the AUTH account's callout, checks it with a
TokenReview the rig answers (`Reviewer`) and grants a session signed by that user's scoped signing key on APP, whose
scope is the user's row; the session ends after the table's `sessionSeconds`. A static user (the development store's,
nats-box's, and the rig's own observers) is an APP user nsc mints, its JWT and seed in a creds file, as the dev seed
mints it. Every binary is the one the estate deploys: `.dagger/nats_auth.go` copies the NATS ones out of the images
the chart and the NATS fixture deploy, builds the signer from the dockerfile its fixture image is built from, and names
each source in `images.json`, which the test compares with the render.

The keys are generated in the run's temp directory and nothing here prints a seed or a token: a `Credential` holds its
seed privately, a `Session` its token as a secret, and the signer logs a decision, never a token. Seeds reach disk
only under the rig's own directories (nsc's keystore and creds, the signers' keys); a pod's directory holds its token
and the bus's CA and nothing else (`Pod`). The broker's trace log carries each static client's CONNECT, which holds
its user JWT and a signature over that run's nonce, and each session's CONNECT with its token redacted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Self

import nats
import nkeys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from nats.errors import NoRespondersError
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr

from lakehouse.events.bus import BusSettings
from lakehouse.events.signing import SigningKey
from tests.chart.chart_render import bus_sessions, containers, env_of, nats_users
from tests.loopback import free_ports


if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from nats.aio.client import Client as NatsClient


#: Where the lane put the binaries (`nsc`, `nats-server`, `bus-signer`, `job/nats`, `box/nats`) and `images.json`.
TOOLS_ENV = "RASK_NATS_AUTH_TOOLS"
#: The values file holding the permission table under `nats.auth.users`; the chart's own when unset.
VALUES_ENV = "RASK_NATS_AUTH_VALUES"

_OPERATOR = "rask-test"
_ACCOUNT = "APP"
_AUTH = "AUTH"
_CREDS_JWT = re.compile(r"-----BEGIN NATS USER JWT-----\s+(\S+)\s+------END NATS USER JWT------")
_CREDS_SEED = re.compile(r"-----BEGIN USER NKEY SEED-----\s+(\S+)\s+------END USER NKEY SEED------")
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
#: Who a client is in the broker's log: a static user by its public key (`jwt:U…`), a session by the name the signer
#: wrote into the user it issued (`user:<name>`, nats-server 2.15.2 `auth_callout.go:332`), whose key is per connection.
_WHO = r'"[^"]*/(?:jwt:(U[A-Z2-7]+)|user:([^"]+))"'
#: `[TRC] <addr> - cid:7 - "<lang:version:name>" - "<account>/APP/<who>" - <<- [PUB <subject> ...]`: a client's own op.
_CLIENT_OP = re.compile(rf'cid:(\d+) - (?:"([^"]*)" - )?{_WHO} - <<- \[(PUB|HPUB|SUB) (\S+)')
#: The stream Job's rendered script names the bus it dials on its first line, `S="<url>"`.
_JOB_BUS = re.compile(r'S="((?:nats|tls)://[^"]+)"')
#: `[ERR] <addr> - cid:7 - "<name>" - "<account>/APP/<who>" - Publish Violation - Subject "<subject>"`.
_VIOLATION = re.compile(rf'\[ERR\] .*?cid:(\d+) - (?:"([^"]*)" - )?{_WHO} - (Publish|Subscription) Violation - Subject "([^"]+)"')
#: A signer's subscription to the servers' authorization requests, as the broker traces it.
_SIGNER_SUBSCRIBED = "<<- [SUB $SYS.REQ.USER.AUTH bus-signer"

#: The namespace `tests.chart.chart_render.render` renders into (`helm template` with none given), which a ServiceAccount
#: of the rig's own is in, beside the release's.
NAMESPACE: Final = "default"
#: What the lane renders the chart with besides the values file: the CA the bus's certificate chains to, named as the
#: NATS fixture names its own (`deploy/values-local.yaml`), so every pod and the stream Job are given a CA file.
RENDER_ARGS: Final = ("--set", "nats.ca.configMap=rask-nats-ca")


def service_account(name: str) -> str:
    """The subject a TokenReview names a ServiceAccount of the release's namespace by."""
    return f"system:serviceaccount:{NAMESPACE}:{name}"


class Tools(BaseModel):
    """The lane's binaries, and the source each came from."""

    model_config = ConfigDict(frozen=True)

    nsc: Path
    nats_server: Path
    #: The stand-in signer, built from `deploy/fixtures/nats/callout` as its fixture image is.
    signer: Path
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
            signer=base / "bus-signer",
            job_cli=base / "job" / "nats",
            box_cli=base / "box" / "nats",
            images=json.loads((base / "images.json").read_text()),
        )
        missing = [str(p) for p in (tools.nsc, tools.nats_server, tools.signer, tools.job_cli, tools.box_cli) if not p.is_file()]
        if missing:
            raise LookupError(f"{TOOLS_ENV}={root} lacks {missing}")
        return tools


class Permissions(BaseModel):
    """One user's row of the chart's permission table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publish: tuple[str, ...] = ()
    subscribe: tuple[str, ...] = ()

    @property
    def inbox(self) -> str:
        """The prefix the replies to this user's own connection arrive under: the inbox its subscribe grant gives it
        alone (``_INBOX_<user>``) where it has one, else the shared ``_INBOX``."""
        inboxes = {subject.removesuffix(".>") for subject in self.subscribe if subject.startswith("_INBOX") and subject.endswith(".>")}
        (inbox,) = inboxes - {"_INBOX"} or inboxes
        return inbox


class SessionUser(BaseModel):
    """One session user's row of the permission table the signer reads."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    service_account: str = Field(alias="serviceAccount")
    publish: tuple[str, ...]
    subscribe: tuple[str, ...]

    def permissions(self) -> Permissions:
        return Permissions(publish=self.publish, subscribe=self.subscribe)


class PermissionTable(BaseModel):
    """`permissions.json`: the audience a pod's token names, how long a session lasts, and each session user."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    audience: str
    session_seconds: int = Field(alias="sessionSeconds")
    users: dict[str, SessionUser]

    def as_json(self) -> str:
        return self.model_dump_json(by_alias=True)


def bus_users(docs: tuple[dict[str, Any], ...]) -> tuple[PermissionTable, dict[str, Permissions]]:
    """The render's bus users: the permission table its signer reads (`<release>-nats-permissions`, `permissions.json`,
    as it stands), and the rows of its static users, which the dev seed issues as nsc users.

    Raises:
        LookupError: The render holds no permission table.
    """
    table = bus_sessions(docs)
    if not table:
        raise LookupError("the render holds no <release>-nats-permissions table")
    static = {user: Permissions(publish=tuple(sorted(pub)), subscribe=tuple(sorted(sub))) for user, (pub, sub) in nats_users(docs).items()}
    return PermissionTable.model_validate(table), static


def _stream_job_environment(docs: tuple[dict[str, Any], ...], token: Path, ca: Path, home: Path) -> dict[str, str]:
    """The environment the render gives the stream Job's container, its files as the rig holds them: the token the kubelet
    projects for the bus's audience, and the CA its ConfigMap mounts, at the paths the render gives a service's pod.

    Raises:
        LookupError: The Job names a file the rig cannot stand in for.
    """
    job = next(env_of(c) for workload, _, c in containers(docs) if workload.startswith("Job/") and "-nats-stream-r" in workload)
    service = next(env_of(c) for _, _, c in containers(docs) if "RASK_BUS_TOKEN_FILE" in env_of(c))
    files = {service["RASK_BUS_TOKEN_FILE"]: str(token), service.get("RASK_BUS_CA_FILE", ""): str(ca)}
    environment = {}
    for name, value in job.items():
        if name == "HOME":
            environment[name] = str(home)
        elif value in files:
            environment[name] = files[value]
        elif value.startswith("/"):
            raise LookupError(f"the stream Job names {value} in {name}, which the rig has no stand-in for")
        else:
            environment[name] = value
    return environment


class Credential(BaseModel):
    """One static user's JWT and NKEY seed, as nsc wrote them into the user's creds file."""

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

    def jwt_bytes(self) -> bytes:
        """nats-py's `user_jwt_cb`."""
        return self.jwt.encode()

    def sign_nonce(self, nonce: str) -> bytes:
        """nats-py's `signature_cb`: the server's nonce signed with the user's seed, as nats-py's own creds path does."""
        return base64.b64encode(SigningKey.from_seed(self._seed).sign(nonce.encode()))

    def options(self) -> dict[str, Any]:
        """What nats-py presents for this user."""
        return {"user_jwt_cb": self.jwt_bytes, "signature_cb": self.sign_nonce}


class Session(BaseModel):
    """A session user's pod as a bare client presents itself: its token, under its bus user's name."""

    model_config = ConfigDict(frozen=True)

    user: str
    token: SecretStr

    def options(self) -> dict[str, Any]:
        """What nats-py presents for this user; its connection's name is the user it asks the signer for."""
        return {"token": self.token.get_secret_value(), "name": self.user}


type Presenting = Credential | Session


class Pki(BaseModel):
    """The run's own CA and the certificate every server, and the relay standing in for one, serves the client port with."""

    model_config = ConfigDict(frozen=True)

    ca: Path
    cert: Path
    key: Path

    @classmethod
    def make(cls, home: Path) -> Self:
        """A CA and a certificate for 127.0.0.1 it signs, written under ``home``. Python 3.13's default context refuses a
        CA without key usage or an issuer link, so both carry them (measured 2026-11-15)."""
        home.mkdir(parents=True)
        now = datetime.datetime.now(datetime.UTC)
        ca_key, server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"rask-test-ca-{secrets.token_hex(4)}")])
        since, until = now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=1)
        ca = (
            x509.CertificateBuilder(
                subject_name=ca_name, issuer_name=ca_name, public_key=ca_key.public_key(), serial_number=1, not_valid_before=since, not_valid_after=until
            )
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)  # noqa: FBT003 — x509's positional flags
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        server = (
            x509.CertificateBuilder(
                subject_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nats")]),
                issuer_name=ca_name,
                public_key=server_key.public_key(),
                serial_number=2,
                not_valid_before=since,
                not_valid_after=until,
            )
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        pem = serialization.Encoding.PEM
        made = cls(ca=home / "ca.crt", cert=home / "server.crt", key=home / "server.key")
        made.ca.write_bytes(ca.public_bytes(pem))
        made.cert.write_bytes(server.public_bytes(pem))
        made.key.write_bytes(server_key.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        made.key.chmod(0o600)
        return made

    def trust(self) -> ssl.SSLContext:
        """A client's verification of the bus's certificate against this CA."""
        return ssl.create_default_context(cafile=str(self.ca))

    def serving(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert), str(self.key))
        return context


def _account_key() -> tuple[str, str]:
    """A fresh account nkey: its seed and its public key."""
    seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_ACCOUNT)
    return seed.decode(), nkeys.from_seed(bytearray(seed)).public_key.decode()


class Mint(BaseModel):
    """The trust root the servers and the signers are given, as nsc keeps it in ``home``.

    All of it is public but the keystore and the signer's files: the resolver config (every account JWT), the sentinel
    (a bearer user JWT that may do nothing), each static user's creds, and each session user's scoped signing key, whose
    seed only the signers read.
    """

    model_config = ConfigDict(frozen=True)

    nsc: Path
    home: Path
    resolver_conf: str
    credentials: dict[str, Credential]
    #: The SYS account's user, which pushes an account's new JWT to the servers.
    system: Credential
    #: The AUTH account's callout user, the signers' own connection.
    callout: Path
    #: The AUTH account signing key's seed file, which signs each callout response.
    issuer_seed: Path
    #: A directory of one scoped-key seed file per session user, named after the user.
    keys: Path
    sentinel: str
    app_account: str
    auth_account: str
    #: Each session user's scoped signing key on APP, by its public key.
    scoped: dict[str, str]

    def user_of(self, who: str) -> str:
        """The user the broker's log names ``who`` for: a session by its name, a static user by its public key."""
        if who.startswith("user:"):
            return who.removeprefix("user:")
        return next((c.user for c in self.credentials.values() if c.public_key == who), who)

    def who(self, user: str) -> str:
        """How the broker's log names ``user``'s connections (:meth:`user_of`'s inverse)."""
        return self.credentials[user].public_key if user in self.credentials else f"user:{user}"

    def scope(self, user: str, was: Permissions, permissions: Permissions) -> None:
        """Change ``user``'s scoped key's scope from ``was`` to ``permissions`` in place: the same key, another scope.

        Raises:
            RuntimeError: nsc refused the edit.
        """
        dropped = [arg for subject in sorted({*was.publish, *was.subscribe}) for arg in ("--rm", subject)]
        if dropped:
            _nsc(self.nsc, self.home, "edit", "signing-key", "--account", _ACCOUNT, "--sk", self.scoped[user], *dropped)
        _nsc(self.nsc, self.home, "edit", "signing-key", "--account", _ACCOUNT, "--sk", self.scoped[user], "--role", user, *_grants(permissions))

    def withdraw(self, user: str) -> None:
        """Remove ``user``'s scoped key from APP.

        Raises:
            RuntimeError: nsc refused the edit.
        """
        _nsc(self.nsc, self.home, "edit", "account", "--name", _ACCOUNT, "--rm-sk", self.scoped[user])

    def app_jwt(self) -> str:
        """APP's account JWT as nsc holds it now."""
        found = _JWT.search(_nsc(self.nsc, self.home, "describe", "account", _ACCOUNT, "--raw"))
        if found is None:
            raise RuntimeError("nsc describes APP without its JWT")
        return found.group(0)


def mint(nsc: Path, static: dict[str, Permissions], sessions: dict[str, Permissions], home: Path, keys: Path) -> Mint:
    """Operator + SYS (`--sys`); APP with JetStream inside the chart's 1Gi store, one user per static row and one scoped
    signing key per session user (role: the user's name, scope: its row); AUTH with the callout user, the bearer sentinel
    denied every subject, one signing key for the callout's answers, and its callout naming APP as the account it binds.

    Raises:
        RuntimeError: an nsc command failed; its stderr carries no seed.
    """
    _nsc(nsc, home, "add", "operator", "--name", _OPERATOR, "--sys")
    _nsc(nsc, home, "add", "account", "--name", _ACCOUNT)
    _nsc(nsc, home, "edit", "account", "--name", _ACCOUNT, "--js-disk-storage", "512M", "--js-mem-storage", "64M")
    credentials = {user: _issue(nsc, home, _ACCOUNT, user, _grants(permissions)) for user, permissions in static.items()}
    keys.mkdir(parents=True)
    scoped: dict[str, str] = {}
    for user, permissions in sessions.items():
        seed, public = _account_key()
        (keys / user).write_text(seed)
        (keys / user).chmod(0o600)
        _nsc(nsc, home, "edit", "account", "--name", _ACCOUNT, "--sk", public)
        _nsc(nsc, home, "edit", "signing-key", "--account", _ACCOUNT, "--sk", public, "--role", user, *_grants(permissions))
        scoped[user] = public
    _nsc(nsc, home, "add", "account", "--name", _AUTH)
    issuer, issuer_public = _account_key()
    issuer_seed = keys.parent / "issuer.seed"
    issuer_seed.write_text(issuer)
    issuer_seed.chmod(0o600)
    _nsc(nsc, home, "edit", "account", "--name", _AUTH, "--sk", issuer_public)
    callout = _issue(nsc, home, _AUTH, "callout", [])
    sentinel = _issue(nsc, home, _AUTH, "sentinel", ["--bearer", "--deny-pubsub", ">"])
    app_account = json.loads(_nsc(nsc, home, "describe", "account", _ACCOUNT, "--json"))["sub"]
    auth_account = json.loads(_nsc(nsc, home, "describe", "account", _AUTH, "--json"))["sub"]
    _nsc(nsc, home, "edit", "authcallout", "--account", _AUTH, "--auth-user", callout.public_key, "--allowed-account", app_account)
    return Mint(
        nsc=nsc,
        home=home,
        resolver_conf=_nsc(nsc, home, "generate", "config", "--mem-resolver", "--sys-account", "SYS"),
        credentials=credentials,
        system=Credential.from_creds("sys", home / "creds" / _OPERATOR / "SYS" / "sys.creds"),
        callout=callout.creds_file,
        issuer_seed=issuer_seed,
        keys=keys,
        sentinel=sentinel.jwt,
        app_account=app_account,
        auth_account=auth_account,
        scoped=scoped,
    )


def _grants(permissions: Permissions) -> list[str]:
    return [arg for subject in permissions.publish for arg in ("--allow-pub", subject)] + [
        arg for subject in permissions.subscribe for arg in ("--allow-sub", subject)
    ]


def _issue(nsc: Path, home: Path, account: str, user: str, args: list[str]) -> Credential:
    _nsc(nsc, home, "add", "user", "--account", account, "--name", user, *args)
    return Credential.from_creds(user, home / "creds" / _OPERATOR / account / f"{user}.creds")


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
    traced, TLS on the client port, the AUTH account's sentinel for a client that presents no JWT), routed into one
    cluster, so each stream the chart declares keeps a copy on every server, as it does live.

    A copied stream refuses some puts unlike one server: a put expecting its subject to hold nothing is refused with 10164
    while another put for that subject is still being agreed, and with 10071 once that one is stored, where one server
    answers 10071 to both (measured on nats-server 2.15.2, 2026-11-09: 40 of 40 racing puts). Every client connects to the
    first server, and the cluster advertises no other (`no_advertise`), so a client stays on the connection the test gave
    it and a `Link` in front of that server cuts it. The routes run on loopback in the clear and without the fixture's
    route credential, which this rig does not prove.
    """

    def __init__(self, server: Path, mint: Mint, pki: Pki, workdir: Path) -> None:
        self._server, self._mint, self.pki, self._dir = server, mint, pki, workdir
        self._processes: list[subprocess.Popen[bytes]] = []
        ports = free_ports(2 * _NODES)
        self._ports, self._routes = ports[:_NODES], ports[_NODES:]
        self.port = self._ports[0]
        self.url = f"tls://127.0.0.1:{self.port}"

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
                f'tls: {{ cert_file: "{self.pki.cert}", key_file: "{self.pki.key}", timeout: 5, handshake_first: auto }}\n'
                f"default_sentinel: {self._mint.sentinel}\n"
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
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    async def push(self, account: str, jwt: str, within: float = 10.0) -> list[str]:
        """Push ``account``'s new JWT to every server as the SYS account's user (`$SYS.REQ.ACCOUNT.<id>.CLAIMS.UPDATE`,
        which each server's resolver answers); the servers that did not take it, by name or as how many went unheard."""
        client = await nats.connect(self.url, tls=self.pki.trust(), name="rig-system", allow_reconnect=False, connect_timeout=5, **self._mint.system.options())
        try:
            inbox = client.new_inbox()
            answers = await client.subscribe(inbox)
            await client.publish(f"$SYS.REQ.ACCOUNT.{account}.CLAIMS.UPDATE", jwt.encode(), reply=inbox)
            taken: set[str] = set()
            refused: list[str] = []
            deadline = asyncio.get_running_loop().time() + within
            while len(taken) + len(refused) < _NODES and (left := deadline - asyncio.get_running_loop().time()) > 0:
                try:
                    reply = json.loads((await answers.next_msg(timeout=left)).data)
                except TimeoutError:
                    break
                server = (reply.get("server") or {}).get("name", "?")
                if reply.get("error"):
                    refused.append(f"{server}: {reply['error']}")
                else:
                    taken.add(server)
        finally:
            await client.close()
        unheard = _NODES - len(taken) - len(refused)
        return refused + ([f"{unheard} servers did not answer"] if unheard else [])

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


class Reviewer:
    """The Kubernetes TokenReview API as the signers call it: it authenticates a token it issued, for the audience the
    table names, as the ServiceAccount it issued it to, and refuses every other token and a caller without its own
    bearer. It counts each review by the ServiceAccount it named, and never keeps a token it was not asked to issue."""

    def __init__(self, audience: str, home: Path) -> None:
        self.audience = audience
        #: The signers' own bearer for the review (`--token-review-token`), as a pod's ServiceAccount token would be.
        self.bearer_file = home / "reviewer-bearer"
        home.mkdir(parents=True, exist_ok=True)
        self.bearer_file.write_text(secrets.token_urlsafe(32))
        self._issued: dict[str, str] = {}
        self._reviewed: list[str] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the reviewer has not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}/apis/authentication.k8s.io/v1/tokenreviews"

    def issue(self, service_account: str) -> str:
        """A new token of ``service_account``'s, for the bus's audience."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._issued[token] = service_account
        return token

    def tokens(self) -> list[str]:
        """Every token this reviewer issued, for a check that none is written where it must not be."""
        with self._lock:
            return list(self._issued)

    def reviewed(self, service_account: str) -> int:
        """How many reviews authenticated ``service_account``."""
        with self._lock:
            return sum(1 for token in self._reviewed if self._issued[token] == service_account)

    def reviews_of(self, token: str) -> int:
        """How many reviews authenticated ``token``."""
        with self._lock:
            return self._reviewed.count(token)

    def start(self) -> None:
        reviewer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's name for a POST
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if self.headers.get("Authorization") != f"Bearer {reviewer.bearer_file.read_text()}":
                    self._answer(401, {"kind": "Status", "status": "Failure", "reason": "Unauthorized"})
                    return
                spec = body.get("spec") or {}
                self._answer(201, {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview", "status": reviewer.review(spec)})

            def _answer(self, code: int, body: dict[str, Any]) -> None:
                text = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — http.server's parameter name
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._server.serve_forever, name="token-review", daemon=True).start()

    def review(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """A TokenReview's status for ``spec``: authenticated as the token's ServiceAccount, for the audience asked,
        only when this reviewer issued the token and the audience asked is the bus's."""
        with self._lock:
            token = str(spec.get("token", ""))
            account = self._issued.get(token)
            if account is None or self.audience not in (spec.get("audiences") or []):
                return {"authenticated": False, "error": "token not issued for this audience"}
            self._reviewed.append(token)
        return {"authenticated": True, "user": {"username": account}, "audiences": [self.audience]}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


class Decision(BaseModel):
    """One line a signer logged: a session granted or refused, the bus user asked for and the ServiceAccount reviewed."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event: str
    user: str = ""
    service_account: str = ""
    reason: str = ""


class Signers:
    """The stand-in signer as its fixture runs it: two `bus-signer` processes in one queue group on the AUTH account's
    callout, reading the rig's permission table (re-read when it changes) and each session user's scoped key, and asking
    the rig's `Reviewer` about each token."""

    def __init__(self, binary: Path, mint: Mint, broker: Broker, reviewer: Reviewer, table: PermissionTable, home: Path, count: int = 2) -> None:
        self._binary, self._mint, self._broker, self._reviewer, self._home, self._count = binary, mint, broker, reviewer, home, count
        self.table_file = home / "permissions.json"
        self._processes: list[subprocess.Popen[bytes]] = []
        home.mkdir(parents=True, exist_ok=True)
        self.write(table)
        self.table = table

    def write(self, table: PermissionTable) -> None:
        """Replace the table the signers read, whole: a reader never sees one half written."""
        staged = self.table_file.with_suffix(".staged")
        staged.write_text(table.as_json())
        os.replace(staged, self.table_file)
        self.table = table

    def start(self, timeout: float = 20.0) -> None:
        """Start the signers, and return once each has subscribed to the servers' authorization requests.

        Raises:
            RuntimeError: A signer exited first; the message carries the end of its log.
            TimeoutError: Not every signer subscribed within ``timeout`` seconds.
        """
        before = self._broker.log_text().count(_SIGNER_SUBSCRIBED)
        accounts = {"auth": self._mint.auth_account, "app": self._mint.app_account}
        for account, public in accounts.items():
            (self._home / f"{account}_account").write_text(public)
        argv = [
            *(str(self._binary), "--url", self._broker.url, "--creds", str(self._mint.callout), "--ca", str(self._broker.pki.ca)),
            *("--issuer-seed", str(self._mint.issuer_seed)),
            *("--auth-account-file", str(self._home / "auth_account"), "--app-account-file", str(self._home / "app_account")),
            *("--keys", str(self._mint.keys), "--table", str(self.table_file), "--token-review-url", self._reviewer.url),
            *("--token-review-ca", str(self._broker.pki.ca), "--token-review-token", str(self._reviewer.bearer_file)),
        ]
        started = []
        for n in range(self._count):
            with (self._home / f"signer-{n}.log").open("ab") as log:
                started.append(subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT))  # noqa: S603
        self._processes += started
        deadline = time.monotonic() + timeout
        while self._broker.log_text().count(_SIGNER_SUBSCRIBED) < before + self._count:
            if exited := [process for process in started if process.poll() is not None]:
                raise RuntimeError(f"a signer exited ({exited[0].returncode}) before it subscribed: {self.log_text()[-800:]}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"the signers did not subscribe within {timeout} s: {self.log_text()[-800:]}")
            time.sleep(0.05)

    def stop(self) -> None:
        running, self._processes = [process for process in self._processes if process.poll() is None], []
        for process in running:
            process.terminate()
        for process in running:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def log_text(self) -> str:
        return "".join(path.read_text(errors="replace") for path in sorted(self._home.glob("signer-*.log")))

    def decisions(self) -> list[Decision]:
        """Every decision the signers logged, in each signer's order."""
        return [Decision.model_validate_json(line) for line in self.log_text().splitlines() if line.startswith('{"event":')]


class Pod(BaseModel):
    """What a session user's pod is given for the bus, and all it is given: a directory holding the token the kubelet
    projects for the bus's audience, and the CA the bus's certificate chains to."""

    model_config = ConfigDict(frozen=True)

    user: str
    home: Path

    @property
    def token_file(self) -> Path:
        return self.home / "token"

    @property
    def ca_file(self) -> Path:
        return self.home / "ca.crt"

    def env(self, url: str, session_seconds: int) -> dict[str, str]:
        """The environment the chart renders for the pod's bus connection (`lance.busEnv`)."""
        return {
            "RASK_BUS_URL": url,
            "RASK_BUS_USER": self.user,
            "RASK_BUS_TOKEN_FILE": str(self.token_file),
            "RASK_BUS_CA_FILE": str(self.ca_file),
            "RASK_BUS_SESSION_SECONDS": str(session_seconds),
        }

    def settings(self, url: str, session_seconds: int) -> BusSettings:
        return BusSettings.model_validate(self.env(url, session_seconds))

    def presenting(self, session_seconds: int) -> dict[str, str]:
        """What the maintenance worker's queue connection reads of the chart's bus environment: its token, the CA and the
        session's length (its bus is `MAINTENANCE_NATS_URL`, and its user the queue's own)."""
        return {name: value for name, value in self.env("", session_seconds).items() if name not in ("RASK_BUS_URL", "RASK_BUS_USER")}


class Rig:
    """The bus, its trust root, its signers and the reviewer they ask, for one module; and the pods and clients a test
    connects through them.

    ``sessions`` and ``static`` are the users minted besides the render's: the rig's own observers and probes, each named
    so no table user's call set carries the test's own calls.
    """

    def __init__(
        self,
        tools: Tools,
        docs: tuple[dict[str, Any], ...],
        home: Path,
        *,
        sessions: Mapping[str, Permissions] | None = None,
        static: Mapping[str, Permissions] | None = None,
        session_seconds: int,
    ) -> None:
        self.tools, self.docs, self.home = tools, docs, home
        self.rendered, self.rendered_static = bus_users(docs)
        rig_sessions = {
            user: SessionUser(service_account=service_account(user), publish=permissions.publish, subscribe=permissions.subscribe)
            for user, permissions in (sessions or {}).items()
        }
        self.table = self.rendered.model_copy(update={"session_seconds": session_seconds, "users": {**self.rendered.users, **rig_sessions}})
        self.static = {**self.rendered_static, **(static or {})}
        self.pki = Pki.make(home / "pki")
        self.mint = mint(tools.nsc, self.static, {user: row.permissions() for user, row in self.table.users.items()}, home / "nsc", home / "signer" / "keys")
        self.broker = Broker(tools.nats_server, self.mint, self.pki, home / "broker")
        self.reviewer = Reviewer(self.table.audience, home / "signer")
        self.signers = Signers(tools.signer, self.mint, self.broker, self.reviewer, self.table, home / "signer")
        self._pods = 0

    def start(self) -> None:
        """Start the servers, the reviewer and the signers; whatever started is stopped again if a later one fails."""
        try:
            self.broker.start()
            self.reviewer.start()
            self.signers.start()
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.signers.stop()
        with contextlib.suppress(Exception):
            self.reviewer.stop()
        self.broker.stop()

    @property
    def url(self) -> str:
        return self.broker.url

    def pod(self, user: str, *, account: str | None = None) -> Pod:
        """A new pod of ``user``'s: a directory of its own holding a token the reviewer issued to ``account`` (``user``'s
        ServiceAccount by default) and the bus's CA."""
        self._pods += 1
        pod = Pod(user=user, home=self.home / "pods" / f"{user}-{self._pods}")
        pod.home.mkdir(parents=True)
        pod.token_file.write_text(self.reviewer.issue(account or self.table.users[user].service_account))
        shutil.copyfile(self.pki.ca, pod.ca_file)
        return pod

    def presenting(self, user: str) -> Presenting:
        """What a bare client connecting as ``user`` presents: a static user's creds, or a token of a session user's."""
        if user in self.mint.credentials:
            return self.mint.credentials[user]
        return Session(user=user, token=SecretStr(self.reviewer.issue(self.table.users[user].service_account)))

    def permissions(self, user: str) -> Permissions:
        return self.table.users[user].permissions() if user in self.table.users else self.static[user]

    async def client(self, user: str, *, name: str | None = None, inbox: str | None = None, answer_within: float = 3.0, url: str | None = None) -> Client:
        """A bare nats-py connection as ``user``, its replies under ``inbox`` (its row's own by default). A session user's
        connection is named after it, as the signer requires; a static one's ``name``, or the user's."""
        presenting = self.presenting(user)
        named = user if isinstance(presenting, Session) else name or user
        return await Client.connect(url or self.url, presenting, named, inbox or self.permissions(user).inbox, answer_within, trust=self.pki.trust())

    @contextlib.contextmanager
    def sessions_of(self, seconds: int) -> Generator[None]:
        """Have the signers grant sessions of ``seconds`` while the block runs, and the table's own length after."""
        was = self.table
        self.signers.write(was.model_copy(update={"session_seconds": seconds}))
        try:
            yield
        finally:
            self.signers.write(was)

    async def scope(self, user: str, permissions: Permissions) -> list[str]:
        """Change ``user``'s scoped key's scope in place and push APP's JWT; the servers that did not take it."""
        await asyncio.to_thread(self.mint.scope, user, self.permissions(user), permissions)
        self.table = self.table.model_copy(update={"users": {**self.table.users, user: self.table.users[user].model_copy(update=permissions.model_dump())}})
        return await self.broker.push(self.mint.app_account, await asyncio.to_thread(self.mint.app_jwt))

    async def withdraw(self, user: str) -> list[str]:
        """Remove ``user``'s scoped key from APP and push APP's JWT; the servers that did not take it."""
        await asyncio.to_thread(self.mint.withdraw, user)
        return await self.broker.push(self.mint.app_account, await asyncio.to_thread(self.mint.app_jwt))

    def run_cli(self, args: list[str], user: str, home: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        """One nats-box CLI command as the static user ``user``, over TLS verified against the rig's CA."""
        credential = self.mint.credentials[user]
        return subprocess.run(  # noqa: S603
            [str(self.tools.box_cli), "--server", self.url, "--tlsca", str(self.pki.ca), "--creds", str(credential.creds_file), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            check=False,
        )

    def run_stream_job(self, script: str, home: Path, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
        """The stream Job's rendered script (:func:`stream_job_script`) as its pod runs it, bus user `admin`: the Job's
        own nats CLI on PATH, no creds file anywhere, and the environment the render gives the Job, its token file a token
        the reviewer issued to the Job's ServiceAccount and its CA file the bus's CA.
        """
        pod = self.pod("admin")
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**_stream_job_environment(self.docs, pod.token_file, pod.ca_file, home), "PATH": f"{self.tools.job_cli.parent}:/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            check=False,
        )


class Link:
    """A TCP relay between one client and the broker that the test can cut, as a network fault or the restart of the
    server a client is attached to looks to the server that holds its consumer: the connection drops, the server stays
    up, and the client's interest is gone until it reconnects.

    It ends TLS on both sides with the rig's own CA, so the client still verifies the bus's certificate and the relay
    reads the protocol in between. Each side's handshake comes before the broker's greeting, as the seam's does.

    It can also lose one subject's traffic while the rest flows (:meth:`drop`): a `PUB` or `HPUB` the client sends on a
    subject a dropped pattern matches never reaches the broker, as a message the network lost, so a request on it goes
    unanswered while the connection, and every other subject, stays up. Only the client's publishes are read for it: the
    protocol frames each one as a line naming its subject and payload size, then the payload.

    Or it can slow the client's side of the path (:meth:`lag`), as a slow route to the server holding a stream's leader
    does: every frame the client sends, a pull and the unsubscribe that gives it up alike, reaches the broker that many
    seconds after it was sent, in order, while what the broker sends comes back at once."""

    def __init__(self, target_port: int, pki: Pki) -> None:
        self._target_port = target_port
        self._pki = pki
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
        return f"tls://127.0.0.1:{self.port}"

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
        self._writers.add(client_writer)
        try:
            broker_reader, broker_writer = await asyncio.open_connection("127.0.0.1", self._target_port, limit=_LINE_LIMIT)
        except OSError:
            client_writer.close()
            self._writers.discard(client_writer)
            return
        self._writers.add(broker_writer)
        try:
            async with asyncio.timeout(_HANDSHAKE_SECONDS):
                await broker_writer.start_tls(self._pki.trust(), server_hostname="127.0.0.1")
                await client_writer.start_tls(self._pki.serving())
        except (ConnectionError, OSError, ssl.SSLError, TimeoutError):
            for writer in (client_writer, broker_writer):
                writer.close()
                self._writers.discard(writer)
            return

        loop = asyncio.get_running_loop()
        # The client's frames, each with when it was read, so a lag delays every frame by the same span rather than adding up.
        sent: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()

        async def read_client() -> None:
            try:
                while frame := await self._client_frame(client_reader):
                    sent.put_nowait((loop.time(), frame))
            except (ConnectionError, OSError, ssl.SSLError, asyncio.IncompleteReadError):
                pass
            finally:
                sent.put_nowait((loop.time(), b""))

        async def to_broker() -> None:
            try:
                while (frame := await sent.get())[1]:
                    await asyncio.sleep(max(0.0, frame[0] + self._lag - loop.time()))
                    broker_writer.write(frame[1])
                    await broker_writer.drain()
            except (ConnectionError, OSError, ssl.SSLError):
                pass
            finally:
                broker_writer.close()
                self._writers.discard(broker_writer)

        async def to_client() -> None:
            try:
                while chunk := await broker_reader.read(65536):
                    client_writer.write(chunk)
                    await client_writer.drain()
            except (ConnectionError, OSError, ssl.SSLError):
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
#: How long the relay gives the broker's greeting and both handshakes.
_HANDSHAKE_SECONDS = 10.0


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
    """One permission refusal the broker logged, its user as the log names it (`Mint.user_of` reads it)."""

    model_config = ConfigDict(frozen=True)

    user_key: str
    connection: str
    kind: str
    subject: str


def violations(log: str) -> list[Violation]:
    return [Violation(connection=m[2] or f"cid:{m[1]}", user_key=m[3] or f"user:{m[4]}", kind=m[5], subject=m[6]) for m in _VIOLATION.finditer(log)]


class Call(BaseModel):
    """One operation a client sent the broker: a publish (`PUB`, `HPUB`) or a subscription (`SUB`), on its subject."""

    model_config = ConfigDict(frozen=True)

    kind: str
    subject: str


def client_calls(log: str) -> dict[str, set[Call]]:
    """Every publish and subscription each authenticated user sent, keyed as the log names the user (`Mint.who`)."""
    seen: dict[str, set[Call]] = {}
    for m in _CLIENT_OP.finditer(log):
        seen.setdefault(m[3] or f"user:{m[4]}", set()).add(Call(kind="subscribe" if m[5] == "SUB" else "publish", subject=m[6]))
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
    async def connect(cls, url: str, presenting: Presenting, name: str, inbox: str = "_INBOX", answer_within: float = 3.0, *, trust: ssl.SSLContext) -> Self:
        """Connect as ``presenting``'s user over TLS verified by ``trust``, its replies arriving under ``inbox``: the
        prefix its subscribe grant admits.

        ``answer_within`` is how long each of its requests waits. A refused request is never answered, so a probe that
        expects refusals waits briefly; a client every call of which is granted waits as long as the service's own JetStream
        calls do (`maintenance.services.queue`'s 10 s), since on the cluster a stream's leader answers one route away, and
        a lane sharing its host with another missed 3 s (measured 2026-11-09).
        """
        nc = await nats.connect(
            url,
            **{"name": name, **presenting.options()},
            error_cb=ignore_refusal,
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=5,
            inbox_prefix=inbox,
            tls=trust,
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


#: An NKEY seed of any kind (user, account, operator), as nsc and nkeys write one.
SEED: Final = re.compile(r"\bS[UAO][A-Z2-7]{56}\b")


def nats_user_jwts(text: str) -> list[str]:
    """Each NATS user JWT in ``text`` (a JWT whose claims say `nats.type: user`), by its subject."""
    found = []
    for candidate in _JWT.findall(text):
        payload = candidate.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except ValueError:
            continue
        if isinstance(claims, dict) and (claims.get("nats") or {}).get("type") == "user":
            found.append(str(claims.get("sub")))
    return found


def secrets_in(paths: list[Path], environments: Mapping[str, Mapping[str, str]], tokens: list[str]) -> list[str]:
    """Every NKEY seed and NATS user JWT under ``paths`` (each file read whole) or in ``environments`` (by process), and
    every bus token in ``environments``: what no process holding only its pod's token may hold."""
    findings = []
    for root in paths:
        for path in [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file()):
            text = path.read_text(errors="replace")
            if SEED.search(text):
                findings.append(f"{path} holds an NKEY seed")
            findings += [f"{path} holds the NATS user JWT of {subject}" for subject in nats_user_jwts(text)]
    for process, environment in environments.items():
        for name, value in environment.items():
            if SEED.search(value):
                findings.append(f"{process}'s environment holds an NKEY seed in {name}")
            findings += [f"{process}'s environment holds the NATS user JWT of {subject} in {name}" for subject in nats_user_jwts(value)]
            if any(token in value for token in tokens):
                findings.append(f"{process}'s environment holds a bus token in {name}")
    return findings
