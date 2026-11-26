"""The suite's Postgres: the development fixture's pinned server, run per xdist worker, and a database of each test's own.

The server is the one `deploy/fixtures/postgres/chart/values.yaml` pins, laid out as one relocatable directory by
`.dagger/postgres.go`: `make postgres` exports it to `.localbin/postgres`, and `dagger call test` copies it to the same
path inside its container. No docker and no host package is involved; the directory carries every shared library the
server loads that glibc does not provide, and needs glibc 2.39 or newer (the highest symbol version its libraries ask
for, libsystemd's, read with objdump on 2026-11-26 from the image's arm64 build): a host with an older glibc, Ubuntu
22.04's 2.35 among them, cannot run the export.

Running the suite as root is supported only in the CI lane's container (`dagger call test`): the server refuses root, so
it runs there as `nobody`.
"""

import os
import pathlib
import pwd
import signal
import subprocess
import tempfile
import time
import uuid
from typing import Final

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from tests.loopback import free_ports


#: Where `make postgres` exports the server, and where `dagger call test` puts it.
POSTGRES_INSTALL: Final = pathlib.Path(__file__).resolve().parent.parent / ".localbin" / "postgres"

#: The superuser initdb creates; local trust authentication, so it has no password.
SUPERUSER: Final = "rask"

#: The database each test's own is cloned from (`CREATE DATABASE … TEMPLATE`).
TEMPLATE: Final = "rask_template"

#: Durability buys a throwaway server nothing: no fsync, no full-page writes, commits acknowledged before the WAL flush.
_THROWAWAY_SETTINGS: Final = ("fsync=off", "full_page_writes=off", "synchronous_commit=off", "jit=off")

#: The account the server runs as when the suite runs as root (the Dagger lane): the server refuses to run as root.
_UNPRIVILEGED: Final = "nobody"

#: How long a fast shutdown may take before the server is killed.
_STOP_SECONDS: Final = 30


class PostgresInstallMissingError(FileNotFoundError):
    """No server in the install directory the suite reads."""


class PostgresDatabase(BaseModel):
    """A database of one test's own on the worker's server."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str


class PostgresServer:
    """One server on a loopback TCP port, its data directory under the worker's temporary directory.

    It listens on TCP only (`unix_socket_directories` empty): a socket under pytest's temporary path exceeds the 107-byte
    limit on a socket's path.
    """

    def __init__(self, install: pathlib.Path, root: pathlib.Path) -> None:
        bin_dirs = sorted(install.glob("lib/postgresql/*/bin"))
        if not bin_dirs or not (bin_dirs[-1] / "postgres").is_file():
            raise PostgresInstallMissingError(f"no Postgres server under {install}: `make postgres` exports the development fixture's pinned one")
        self._bin = bin_dirs[-1]
        self._env = {"PATH": os.environ.get("PATH", ""), "LD_LIBRARY_PATH": str(install / "libs")}
        self._account = _UNPRIVILEGED if os.geteuid() == 0 else None
        self._data = root / "data"
        if self._account is not None:
            _hand_to(root, self._account)
        self._run("initdb", "-D", str(self._data), "-U", SUPERUSER, "--auth=trust", "-E", "UTF8", "--no-locale", "--no-sync", "--no-instructions")
        self._process, self.port = self._start(root)

    def _run(self, program: str, *args: str) -> None:
        done = subprocess.run([str(self._bin / program), *args], env=self._env, user=self._account, check=False, capture_output=True)
        if done.returncode != 0:
            raise RuntimeError(f"{program} exited {done.returncode}: {done.stderr.decode(errors='replace')[-2000:]}")

    def _start(self, root: pathlib.Path) -> tuple[subprocess.Popen[bytes], int]:
        """Start the server on a free port, retried on a new one when another worker took it in between."""
        failures: list[str] = []
        for attempt in range(3):
            (port,) = free_ports(1)
            settings = [f"-c{setting}" for setting in (*_THROWAWAY_SETTINGS, "listen_addresses=127.0.0.1", "unix_socket_directories=")]
            log = root / f"postgres-{attempt}.log"
            # A file, not a pipe: nothing reads a running server's output, and a full pipe would block it mid-session.
            with log.open("wb") as output:
                process = subprocess.Popen(
                    [str(self._bin / "postgres"), "-D", str(self._data), "-p", str(port), *settings],
                    env=self._env,
                    user=self._account,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            if self._serves(process, port):
                return process, port
            process.kill()
            process.wait(timeout=10)
            failures.append(log.read_text(errors="replace")[-2000:])
        raise RuntimeError(f"Postgres did not start: {failures}")

    @staticmethod
    def _conninfo(port: int, database: str) -> str:
        return f"postgresql://{SUPERUSER}@127.0.0.1:{port}/{database}"

    def _serves(self, process: subprocess.Popen[bytes], port: int) -> bool:
        """Whether this server answers on ``port`` within 30 s; False as soon as it exits.

        A connect alone does not say which server answered: another worker's may have bound the port first, so the
        answer counts only while this process runs and the server reports this server's data directory.
        """
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            try:
                with psycopg.connect(self._conninfo(port, "postgres"), connect_timeout=1) as connection:
                    row = connection.execute("SHOW data_directory").fetchone()
            except psycopg.OperationalError:
                time.sleep(0.05)
                continue
            return process.poll() is None and row is not None and pathlib.Path(str(row[0])).resolve() == self._data.resolve()
        return False

    def build_template(self) -> None:
        """Create the template every test's database is cloned from.

        The template is the one seam that gives a test's database its schema: empty in this change, and LH-613 fills this
        method with the catalog's migrations. It lives as long as the worker's server, one session, so its fixed name is
        enough: every session builds it again from the migrations it finds.
        """
        with psycopg.connect(self._conninfo(self.port, "postgres"), autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEMPLATE)))

    def create_database(self) -> PostgresDatabase:
        """A new database cloned from the template, under a name no other test holds."""
        name = f"test_{uuid.uuid4().hex[:16]}"
        with psycopg.connect(self._conninfo(self.port, "postgres"), autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name), sql.Identifier(TEMPLATE)))
        return PostgresDatabase(name=name, url=self._conninfo(self.port, name))

    def drop_database(self, database: PostgresDatabase) -> None:
        """Drop a test's database, closing any connection the test left open."""
        with psycopg.connect(self._conninfo(self.port, "postgres"), autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database.name)))

    def stop(self) -> None:
        """Fast shutdown (SIGINT): open sessions are ended, the data directory stays for a kept database to be read."""
        self._process.send_signal(signal.SIGINT)
        try:
            self._process.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


def _hand_to(root: pathlib.Path, account: str) -> None:
    """Give ``account`` its data root, for the CI lane's container, where the suite runs as root.

    pytest makes each directory of its temporary tree 0700 (`pytest-of-root`, `pytest-<n>`, the worker's `popen-gw<n>`),
    so each between the system temporary directory and the data root is opened for traversal, and nothing outside pytest's
    tree is changed. A data root outside the system temporary directory (a `--basetemp` elsewhere) is refused.
    """
    system_temp = pathlib.Path(tempfile.gettempdir()).resolve()
    resolved = root.resolve()
    if system_temp not in resolved.parents:
        raise RuntimeError(f"as root, the suite's Postgres needs its data under {system_temp}, pytest's default base temporary directory; got {resolved}")
    entry = pwd.getpwnam(account)
    os.chown(resolved, entry.pw_uid, entry.pw_gid)
    for parent in resolved.parents:
        if parent == system_temp:
            break
        mode = parent.stat().st_mode
        if mode & 0o011 != 0o011:
            parent.chmod(mode | 0o011)
