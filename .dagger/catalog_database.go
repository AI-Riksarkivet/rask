package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// A LANE'S CATALOG OPENS ITS DATABASE AS THE CHART'S DOES (ADR 0230): a Postgres holding the database `catalog`, made
// with its three roles by the statements the platform's DBA runs (the development fixture's own provision.sql), its
// schema applied by `python -m catalog.schema migrate` as the migrator, and the catalog connecting as its own role with
// its credential in files at the path the chart mounts it. A catalog with no database, or one whose schema is not its
// image's revision, refuses to start, so a lane that skipped a step fails at the catalog's boot.

// The names the chart's defaults give (chart/values.yaml `postgres.catalog`, mirrored by the fixture's `catalog`).
const (
	catalogDatabaseName     = "catalog"
	catalogDatabaseMigrator = "rask_migrator"
	catalogDatabaseRole     = "rask_catalog"
	catalogDatabaseMaintain = "rask_maintenance"
)

const (
	// catalogDatabaseHost is the session hostname the lane binds the server as.
	catalogDatabaseHost = "catalog-postgres"
	// catalogDatabaseCredentialDir is where the chart mounts a role's `username` and `password`
	// (`lance.databaseCredentialDir`), read by the process at every new connection.
	catalogDatabaseCredentialDir = "/var/run/secrets/rask/database"
	// catalogImageUser owns a credential mounted into the catalog's image: its numeric USER (.docker/rest-catalog.dockerfile),
	// which reads the file the secret mount leaves at mode 0400.
	catalogImageUser = "10001"
	// postgresImageUser is the stock image's `postgres` account, which its entrypoint drops to before it reads
	// POSTGRES_PASSWORD_FILE a second time, and as which the provisioning step runs, as the fixture's Job runs as uid 999.
	postgresImageUser = "postgres"
	// provisionSQL and provisionScript are the fixture's provisioning files, run unchanged: the script builds the
	// superuser's pgpass, waits for the server and runs the statements with the role names as psql variables.
	provisionSQL    = "deploy/fixtures/postgres/chart/files/provision.sql"
	provisionScript = "deploy/fixtures/postgres/chart/files/provision.sh"
)

// catalogDatabase is a lane's catalog database, provisioned and migrated, and the catalog role's password.
type catalogDatabase struct {
	server   *dagger.Service
	password *dagger.Secret
}

// catalogDatabase starts the fixture's pinned Postgres, provisions the database and its roles with provision.sql, and
// migrates its schema with the catalog image, each step forced to finish before the next (`Sync`).
//
// EVERY PASSWORD IS THIS RUN'S, and reaches each process as a file: the superuser's through POSTGRES_PASSWORD_FILE, the
// roles' as the files provision.sql reads, and the migrator's and the catalog's at the chart's credential path. None is
// an environment variable.
//
// THE DATA DIRECTORY IS A PER-RUN VOLUME, for the reason the governed stack's stores keep one: a step that finishes drops
// the server to zero references and Dagger stops it, so the catalog starts it again and must find the database the
// steps made. The run's name also keeps Dagger from reusing a provisioning or migration exec of an earlier run against
// a server that never saw it.
func (m *Rask) catalogDatabase(ctx context.Context, src *dagger.Directory) (*catalogDatabase, error) {
	image, _, err := postgresImage(ctx, src.File(postgresValues))
	if err != nil {
		return nil, err
	}
	passwords := map[string]*dagger.Secret{}
	for _, name := range []string{"superuser", "migrator", "catalog", "maintenance"} {
		if passwords[name], err = sessionSecret("catalog-database-" + name); err != nil {
			return nil, err
		}
	}
	owned := dagger.ContainerWithMountedSecretOpts{Owner: postgresImageUser}

	server := dag.Container().
		From(image).
		WithMountedSecret("/run/secrets/postgres/password", passwords["superuser"], owned).
		WithEnvVariable("POSTGRES_PASSWORD_FILE", "/run/secrets/postgres/password").
		WithMountedCache("/var/lib/postgresql/data", dag.CacheVolume("rask-catalog-database-"+runToken())).
		WithExposedPort(5432).
		WithDefaultArgs([]string{"docker-entrypoint.sh", "postgres"}).
		AsService()

	// pg_isready reaches the server over TCP, which the image's entrypoint opens only once its first initialisation is
	// done, so the step never provisions the temporary server that initialisation runs.
	if _, err := dag.Container().
		From(image).
		WithUser(postgresImageUser).
		WithServiceBinding(catalogDatabaseHost, server).
		WithFile("/etc/rask/provision/provision.sql", src.File(provisionSQL)).
		WithFile("/etc/rask/provision/provision.sh", src.File(provisionScript)).
		WithMountedSecret("/run/secrets/postgres-superuser/password", passwords["superuser"], owned).
		WithMountedSecret("/run/secrets/database-roles/migrator", passwords["migrator"], owned).
		WithMountedSecret("/run/secrets/database-roles/catalog", passwords["catalog"], owned).
		WithMountedSecret("/run/secrets/database-roles/maintenance", passwords["maintenance"], owned).
		WithEnvVariable("PGHOST", catalogDatabaseHost).
		WithEnvVariable("PGUSER", "postgres").
		WithEnvVariable("PGDATABASE", "postgres").
		WithEnvVariable("PGPASSFILE", "/tmp/pgpass").
		WithEnvVariable("PGCONNECT_TIMEOUT", "5").
		WithExec([]string{"sh", "/etc/rask/provision/provision.sh",
			catalogDatabaseMigrator, catalogDatabaseRole, catalogDatabaseMaintain, catalogDatabaseName}).
		Sync(ctx); err != nil {
		return nil, err
	}

	// The release's migration Job (chart/templates/catalog-migrate.yaml): the catalog image as the migrator, naming the
	// roles each revision grants to.
	if _, err := onDatabase(m.Image(src, "rest-catalog", "", "", "", nil, ""), server, catalogDatabaseMigrator, passwords["migrator"]).
		WithEnvVariable("RASK_DATABASE_CATALOG_ROLE", catalogDatabaseRole).
		WithEnvVariable("RASK_DATABASE_MAINTENANCE_ROLE", catalogDatabaseMaintain).
		WithExec([]string{"python", "-m", "catalog.schema", "migrate"}).
		Sync(ctx); err != nil {
		return nil, err
	}
	return &catalogDatabase{server: server, password: passwords["catalog"]}, nil
}

// attach connects a catalog container to the database as the catalog's role, with the pool the chart's defaults size
// (`postgres.catalog.pool`).
func (db *catalogDatabase) attach(ctr *dagger.Container) *dagger.Container {
	return onDatabase(ctr, db.server, catalogDatabaseRole, db.password).
		WithEnvVariable("RASK_DATABASE_POOL_SIZE", "4").
		WithEnvVariable("RASK_DATABASE_ACQUIRE_TIMEOUT_SECONDS", "5").
		WithEnvVariable("RASK_DATABASE_RECYCLE_SECONDS", "1800")
}

// onDatabase gives `ctr` the database as the chart gives a pod that connects as `role` (`lance.databaseEnv`,
// `lance.databaseCredential`): the server's address, the database, and the role's credential files.
func onDatabase(ctr *dagger.Container, server *dagger.Service, role string, password *dagger.Secret) *dagger.Container {
	return ctr.
		WithServiceBinding(catalogDatabaseHost, server).
		WithNewFile(catalogDatabaseCredentialDir+"/username", role).
		WithMountedSecret(catalogDatabaseCredentialDir+"/password", password, dagger.ContainerWithMountedSecretOpts{Owner: catalogImageUser}).
		WithEnvVariable("RASK_DATABASE_HOST", catalogDatabaseHost).
		WithEnvVariable("RASK_DATABASE_PORT", "5432").
		WithEnvVariable("RASK_DATABASE_NAME", catalogDatabaseName).
		WithEnvVariable("RASK_DATABASE_CREDENTIALS_DIR", catalogDatabaseCredentialDir)
}
