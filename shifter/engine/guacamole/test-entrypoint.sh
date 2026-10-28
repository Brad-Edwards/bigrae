#!/usr/bin/env bash
# Prove that a SQL error stops the application before startup.
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
