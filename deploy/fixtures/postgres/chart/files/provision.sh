# The catalog's database and its three roles, provisioned with provision.sql beside this file, by the fixture's Job
# (templates/provision.yaml) and by the Dagger lane's database (.dagger/catalog_database.go), which run it as it stands.
#
#   sh provision.sh <migrator> <catalog> <maintenance> <database>
#
# libpq's PG* variables name the server and its superuser, PGPASSFILE where the pgpass goes. The superuser's password
# is the file /run/secrets/postgres-superuser/password, and each role's is a file named for it under
# /run/secrets/database-roles. Nothing secret is an argument or a variable.
set -eu
umask 077
# A pgpass field escapes `:` and `\` (libpq, "The Password File"); libpq ignores one any other account may read.
printf '*:*:*:%s:%s\n' "$PGUSER" "$(sed 's/[\\:]/\\&/g' /run/secrets/postgres-superuser/password)" > "$PGPASSFILE"
until pg_isready -q; do echo wait-postgres; sleep 2; done
psql -X -q -v migrator="$1" -v catalog="$2" -v maintenance="$3" -v database="$4" \
  -v passwords=/run/secrets/database-roles -f "$(dirname "$0")/provision.sql"
echo "provisioned database $4 and roles $1, $2, $3"
