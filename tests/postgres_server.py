"""The suite's Postgres: the development fixture's pinned server, run per xdist worker, and a database of each test's own.

The server is the one `deploy/fixtures/postgres/chart/values.yaml` pins, laid out as one relocatable directory by
`.dagger/postgres.go`: `make postgres` exports it to `.localbin/postgres`, and `dagger call test` copies it to the same
path inside its container. No docker and no host package is involved; the directory carries every shared library the
server loads that glibc does not provide, and needs glibc 2.39 or newer (the highest symbol version its libraries ask
for, libsystemd's, read with objdump on 2026-11-26 from the image's arm64 build): a host with an older glibc, Ubuntu
22.04's 2.35 among them, cannot run the export.

Running the suite as root is supported only in the CI lane's container (`dagger call test`): the server refuses root, so
it runs there as `nobody`, in a directory of its own (:class:`PostgresServer`).
"""

import asyncio
import os
import pathlib
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from typing import Final

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from catalog.core.schema import MigrateSettings, migrate
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


class CatalogRoles(BaseModel):
    """The catalog's three roles, as the platform's provisioning names them by default."""

    model_config = ConfigDict(frozen=True)

    migrator: str = "rask_migrator"
    catalog: str = "rask_catalog"
    maintenance: str = "rask_maintenance"


#: The roles every worker's server holds, cluster-wide.
ROLES: Final = CatalogRoles()


def write_credential(directory: pathlib.Path, role: str) -> pathlib.Path:
    """Write ``role``'s credential into ``directory`` as the secret store renders one, and return it: the server trusts
    loopback, so the password is empty."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "username").write_text(role, encoding="utf-8")
    (directory / "password").write_text("", encoding="utf-8")
    return directory


class PostgresDatabase(BaseModel):
    """A database of one test's own on the worker's server."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str


class PostgresServer:
    """One server on a loopback TCP port, its log under ``root``, the worker's temporary directory.

    Run as the suite's own account, its data directory is under ``root`` too, so a kept database outlives the session.
    Run as ``nobody`` (the suite as root), nothing under pytest's temporary tree stays reachable to it: pytest makes every
    directory of that tree 0700, and each pytest process that makes a temporary directory without a ``--basetemp`` sets
    `pytest-of-<user>` back to 0700 (pytest 9.1.1, `TempPathFactory.getbasetemp`), as a test that runs pytest in a
    subprocess does. So there the data directory is one of the server's own under the system temporary directory, owned
    by ``nobody`` and removed when the server stops. The log needs no path: the server writes it through the file it
    inherits. Measured 2026-11-28 in the CI lane: a server running as ``nobody`` on data under pytest's tree stops at its
    first checkpoint after such a process starts, refused its own `global/pg_control`
    (`tests/integration/test_a_test_gets_a_postgres_database_of_its_own.py`).

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
        self._own_directory = None if self._account is None else _directory_owned_by(self._account)
        self._data = (root if self._own_directory is None else self._own_directory) / "data"
        try:
            self._run("initdb", "-D", str(self._data), "-U", SUPERUSER, "--auth=trust", "-E", "UTF8", "--no-locale", "--no-sync", "--no-instructions")
            self._process, self.port = self._start(root)
        except BaseException:
            self._remove_own_directory()
            raise

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

    def build_template(self, credentials: pathlib.Path) -> None:
        """Create the catalog's three roles and the template every test's database is cloned from, migrated as the
        migrator by the migration Job's own code (:func:`catalog.core.schema.migrate`), its credential written under
        ``credentials``.

        The template lives as long as the worker's server, one session, so its fixed name is enough: every session builds
        it again from the revisions it finds.
        """
        with psycopg.connect(self._conninfo(self.port, "postgres"), autocommit=True) as connection:
            for role in ROLES.model_dump().values():
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(TEMPLATE), sql.Identifier(ROLES.migrator)))
        asyncio.run(migrate(self.migrate_settings(TEMPLATE, write_credential(credentials, ROLES.migrator))))

    def migrate_settings(self, database: str, credentials: pathlib.Path) -> MigrateSettings:
        """What the migration Job is given, against ``database`` on this server with the credential in ``credentials``."""
        return MigrateSettings.model_validate(
            {
                "database_host": "127.0.0.1",
                "database_port": self.port,
                "database_name": database,
                "database_credentials_dir": credentials,
                "database_catalog_role": ROLES.catalog,
                "database_maintenance_role": ROLES.maintenance,
            }
        )

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
        """Fast shutdown (SIGINT): open sessions are ended. A data directory under ``root`` stays for a kept database to
        be read; one of the server's own is removed with it."""
        self._process.send_signal(signal.SIGINT)
        try:
            self._process.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._remove_own_directory()

    def _remove_own_directory(self) -> None:
        if self._own_directory is not None:
            shutil.rmtree(self._own_directory)


def _directory_owned_by(account: str) -> pathlib.Path:
    """A new directory under the system temporary directory, outside pytest's tree, that only ``account`` may enter."""
    made = pathlib.Path(tempfile.mkdtemp(prefix="rask-postgres-"))
    entry = pwd.getpwnam(account)
    os.chown(made, entry.pw_uid, entry.pw_gid)
    return made
