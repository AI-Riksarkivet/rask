-- The catalog's database and its three roles, as the platform's DBA creates them (ADR 0230 §8, docs/OPERATORS.md).
-- Run as the server's superuser on every install of the fixture, on an empty server and on one that already holds
-- them, so every statement converges: a role or the database is created only where missing, and each role's
-- attributes and password, the database's owner and its connect grants are set again on every run.
--
-- psql variables (-v): migrator, catalog, maintenance (the role names), database (the catalog's database), and
-- passwords (a directory holding one file per role, named migrator, catalog and maintenance, each its password).
\set ON_ERROR_STOP on
\set migrator_password `cat :'passwords'/migrator`
\set catalog_password `cat :'passwords'/catalog`
\set maintenance_password `cat :'passwords'/maintenance`

SELECT format('CREATE ROLE %I', role)
FROM unnest(ARRAY[:'migrator', :'catalog', :'maintenance']) AS role
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role) \gexec

-- No role but the server's superuser may create roles or databases, and only the migrator owns anything.
ALTER ROLE :"migrator" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'migrator_password';
ALTER ROLE :"catalog" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'catalog_password';
ALTER ROLE :"maintenance" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'maintenance_password';

SELECT format('CREATE DATABASE %I OWNER %I', :'database', :'migrator')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'database') \gexec

ALTER DATABASE :"database" OWNER TO :"migrator";
REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO :"catalog", :"maintenance";
