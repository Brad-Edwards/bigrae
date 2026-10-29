#!/usr/bin/env bash
# Prove that a SQL error stops the application before startup, and that the
# upstream default administrator account does not survive startup.
set -euo pipefail

wrapper=$(realpath "${1:-$(dirname "$0")/entrypoint.sh}")
fixture=$(mktemp -d)
container="guacamole-entrypoint-quality-$$"
cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf "$fixture"
}
trap cleanup EXIT

cat > "$fixture/initdb.sh" <<'SQL'
#!/bin/sh
printf 'SELECT missing_quality_test_function();\n'
SQL
chmod +x "$fixture/initdb.sh"

docker run --detach --rm --network none --name "$container" \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -e POSTGRESQL_HOSTNAME=127.0.0.1 \
    -e POSTGRESQL_USER=postgres \
    -e POSTGRESQL_DATABASE=postgres \
    -v "$fixture/initdb.sh:/opt/guacamole/bin/initdb.sh:ro" \
    -v "$wrapper:/quality-entrypoint.sh:ro" postgres:16 >/dev/null

ready=false
for _attempt in $(seq 1 30); do
    if docker exec "$container" pg_isready -h 127.0.0.1 -U postgres -d postgres >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    echo 'Test database did not become ready' >&2
    exit 1
fi

if docker exec "$container" bash /quality-entrypoint.sh /bin/true > "$fixture/result" 2>&1; then
    echo 'SQL failure incorrectly allowed application startup' >&2
    exit 1
fi
grep -q 'missing_quality_test_function' "$fixture/result"
grep -q 'Failed to initialize database schema' "$fixture/result"
echo 'SQL failure prevented application startup'

# The upstream schema seeds the default administrator. Model the tables that
# the removal touches. Overwrite the fixture in place so that the bind mount
# sees the new content.
cat > "$fixture/initdb.sh" <<'SQL'
#!/bin/sh
cat <<'SCHEMA'
CREATE TABLE guacamole_entity (
    entity_id serial PRIMARY KEY,
    name varchar(128) NOT NULL,
    type varchar(16) NOT NULL
);
CREATE TABLE guacamole_user (
    user_id serial PRIMARY KEY,
    entity_id integer NOT NULL REFERENCES guacamole_entity (entity_id) ON DELETE CASCADE
);
INSERT INTO guacamole_entity (name, type) VALUES ('guacadmin', 'USER'), ('portal-user@example.com', 'USER');
INSERT INTO guacamole_user (entity_id) SELECT entity_id FROM guacamole_entity;
SCHEMA
SQL

query() {
    docker exec "$container" psql -h 127.0.0.1 -U postgres -d postgres -tAc "$1"
}

assert_default_admin_absent() {
    if [[ "$(query "SELECT count(*) FROM guacamole_entity WHERE type = 'USER' AND name = 'guacadmin'")" != 0 ]]; then
        echo "The default administrator survived startup ($1)" >&2
        exit 1
    fi
    if [[ "$(query "SELECT count(*) FROM guacamole_entity WHERE name = 'portal-user@example.com'")" != 1 ]]; then
        echo "Startup removed an account other than the default administrator ($1)" >&2
        exit 1
    fi
    if [[ "$(query "SELECT count(*) FROM guacamole_user")" != 1 ]]; then
        echo "The default administrator user row survived startup ($1)" >&2
        exit 1
    fi
}

# A new database: the schema is created, then the seeded account is removed.
docker exec "$container" bash /quality-entrypoint.sh /bin/true > "$fixture/result" 2>&1
assert_default_admin_absent 'new database'

# An existing database that still has the seeded account: removed on start.
query "INSERT INTO guacamole_entity (name, type) VALUES ('guacadmin', 'USER')" >/dev/null
query "INSERT INTO guacamole_user (entity_id) SELECT entity_id FROM guacamole_entity WHERE name = 'guacadmin'" >/dev/null
docker exec "$container" bash /quality-entrypoint.sh /bin/true > "$fixture/result" 2>&1
grep -q 'Database schema already exists' "$fixture/result"
assert_default_admin_absent 'existing database'
echo 'Startup removed the default administrator account'
