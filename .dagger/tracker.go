package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// postgresImage is the tracker's integration backend. PG16 matches the estate: the chart's AGE image is
// `apache/age:release_PG16_1.5.0` (see e2e.go) and CloudNativePG runs the same major, so a test proving
// the Postgres tracker works proves it against the version production uses.
const postgresImage = "postgres:16-alpine"

// TrackerPostgres runs `packages/tracker/tests/test_postgres.py` against a REAL PostgreSQL.
//
// Those six tests were opt-in behind `--postgresql-port`, and **nothing passed it** — no Makefile
// target, no script, no CI job, no Dagger function. `packages/tracker`'s stated contract is
// backend-agnosticism (SQLite for dev, Postgres for prod) and its production backend had no runner, so
// every green tracker run proved only the SQLite half. An opt-in with no opter-in is a deletion that
// still reads as coverage.
//
// It runs the server as a Dagger SERVICE, not a container the test starts, because the repository rule
// admits no exception: "Any container — ephemeral brokers, one-off fixtures, ad-hoc debugging — goes
// through Dagger", and this is exactly the "one-off fixture" the rule names. `postgresql_noproc()` in
// the suite is already the right factory for that — it connects to a server someone else runs rather
// than spawning `pg_ctl` itself, which is why no `--postgresql-exec` is needed here.
func (m *Rask) TrackerPostgres(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	postgres := dag.Container().
		From(postgresImage).
		WithEnvVariable("POSTGRES_USER", "tracker").
		WithEnvVariable("POSTGRES_PASSWORD", "tracker").
		// TRUST auth, and the reason is a property of the SUITE rather than a shortcut. `pg_tracker`
		// rebuilds a DSN string from `postgresql_conn.info`, and for a `postgresql_noproc()` server
		// `info.password` comes back EMPTY — so the DSN it hands `PostgresTracker` carries no password and
		// scram auth refuses it with `fe_sendauth: no password supplied`. Five of the six integration tests
		// use the live connection object and passed; only `test_postgres_via_factory_end_to_end`, which
		// goes through the DSN, hit it. Trust auth on a throwaway server that outlives nothing is the
		// right fix here; teaching the fixture to carry the password is a change to a suite whose job is
		// to test the tracker, not pytest-postgresql.
		WithEnvVariable("POSTGRES_HOST_AUTH_METHOD", "trust").
		// The MAINTENANCE database, which must NOT be the one the suite creates. Setting both to
		// "tracker" made pytest-postgresql try to CREATE a database the image had already made, and all
		// six tests errored `DuplicateDatabase: database "tracker" already exists` — a green server and
		// a red suite, for a reason that looks nothing like the cause.
		WithEnvVariable("POSTGRES_DB", "postgres").
		WithExposedPort(5432).
		// DEFAULT ARGS, not WithExec. The official image refuses to run the server as root ("must be
		// started under an unprivileged user ID to prevent possible system security compromise") — its
		// entrypoint is what does initdb and drops privileges, and a WithExec of `postgres` directly
		// bypasses it. This is also the form CLAUDE.md prescribes for an ad-hoc Dagger service.
		//
		// `fsync=off` is safe and meaningfully faster for a throwaway server: the data does not outlive
		// the run, so durability buys nothing and costs a flush per commit on a suite whose whole point
		// is batched writes.
		WithDefaultArgs([]string{"docker-entrypoint.sh", "postgres", "-c", "fsync=off", "-c", "full_page_writes=off"}).
		AsService()

	return m.base(src).
		WithServiceBinding("postgres", postgres).
		WithExec([]string{
			"uv", "run", "--no-sync", "pytest",
			"packages/tracker/tests/test_postgres.py",
			"-v",
			"--postgresql-host=postgres",
			"--postgresql-port=5432",
			"--postgresql-user=tracker",
			"--postgresql-password=tracker",
			"--postgresql-dbname=tracker_test",
		}).
		Stdout(ctx)
}
