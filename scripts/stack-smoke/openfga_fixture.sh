#!/usr/bin/env bash
# Sourced by stack_smoke.sh. Mirrors scripts/run_openfga_integration.sh's
# released TLS server and chart-owned PostgreSQL duty separation.

start_openfga() {
  local image="openfga/openfga@sha256:d53ce5c48413d01e75ecf375f3f74eb35c50f155fc028c41d03dbc7c9838fb38"
  local cli="openfga/cli@sha256:26acde96d90420e53fe361a740dcceba4b67f1c49e4f35117cd0c19c83ac5068"
  local admin_token runtime_token admin_port model_dsl app_uid publication
  admin_token="$(gen_secret)"
  runtime_token="$(gen_secret)"
  FIXTURE_DIR="$(mktemp -d)"
  chmod 0755 "$FIXTURE_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -keyout "$FIXTURE_DIR/tls.key" -out "$FIXTURE_DIR/tls.crt" \
    -subj '/CN=localhost' \
    -addext "subjectAltName=DNS:localhost,DNS:${FGA_RUNTIME},IP:127.0.0.1" >/dev/null 2>&1
  chmod 0644 "$FIXTURE_DIR/tls.key" "$FIXTURE_DIR/tls.crt"
  printf '%s' "$admin_token" > "$FIXTURE_DIR/admin-token"
  printf '%s' "$runtime_token" > "$FIXTURE_DIR/runtime-token"
  printf '%s' '{"schema":"shifter.identity-cutover-facts/v1","account_types_by_organization":{},"owner_user_ids_by_workspace":{},"bootstrap_personal_token_eligibility":true}' > "$FIXTURE_DIR/cutover.json"
  printf '{"schema":"shifter.identity-bootstrap-facts/v1","issuer":"%s","subject":"stack-smoke-operator"}' "$STUB_ISSUER_URL" > "$FIXTURE_DIR/bootstrap.json"
  chmod 0600 "$FIXTURE_DIR/"{admin-token,runtime-token,cutover.json,bootstrap.json}

  docker exec "$PG" createdb -h 127.0.0.1 -U "$DB_USER" openfga
  smoke_run --rm --network "$SMOKE_NETWORK" \
    -e OPENFGA_DATASTORE_ENGINE=postgres \
    -e "OPENFGA_DATASTORE_URI=postgres://${DB_USER}:${DB_PASSWORD}@${PG}:5432/openfga?sslmode=disable" \
    "$image" migrate
  docker exec -i "$PG" psql -h 127.0.0.1 -U "$DB_USER" -d openfga -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE smoke_fga_runtime LOGIN PASSWORD 'synthetic-runtime-password';
CREATE ROLE smoke_fga_admin LOGIN PASSWORD 'synthetic-admin-password';
SQL
  docker exec -i "$PG" psql -h 127.0.0.1 -U "$DB_USER" -d openfga \
    -v runtime_login=smoke_fga_runtime -v admin_login=smoke_fga_admin \
    < "$REPO_ROOT/platform/charts/shifter/files/openfga-duties.sql"

  local name login password token
  for name in "$FGA_ADMIN" "$FGA_RUNTIME"; do
    if [[ "$name" == "$FGA_ADMIN" ]]; then
      login=smoke_fga_admin password=synthetic-admin-password token="$admin_token"
    else
      login=smoke_fga_runtime password=synthetic-runtime-password token="$runtime_token"
    fi
    smoke_run -d --name "$name" --network "$SMOKE_NETWORK" -p 127.0.0.1::8080 \
      -v "$FIXTURE_DIR/tls.crt:/smoke-tls/tls.crt:ro" -v "$FIXTURE_DIR/tls.key:/smoke-tls/tls.key:ro" \
      -e OPENFGA_DATASTORE_ENGINE=postgres \
      -e "OPENFGA_DATASTORE_URI=postgres://${login}:${password}@${PG}:5432/openfga?sslmode=disable" \
      -e OPENFGA_AUTHN_METHOD=preshared -e "OPENFGA_AUTHN_PRESHARED_KEYS=${token}" \
      -e OPENFGA_HTTP_TLS_ENABLED=true -e OPENFGA_HTTP_TLS_CERT=/smoke-tls/tls.crt \
      -e OPENFGA_HTTP_TLS_KEY=/smoke-tls/tls.key -e OPENFGA_GRPC_ADDR=127.0.0.1:8081 \
      -e OPENFGA_PLAYGROUND_ENABLED=false -e OPENFGA_PROFILER_ENABLED=false "$image" run >/dev/null
    admin_port="$(docker port "$name" 8080/tcp | sed -n 's/.*://p')"
    wait_for 60 "$name TLS readiness" curl --fail --silent --max-time 5 --cacert "$FIXTURE_DIR/tls.crt" "https://localhost:${admin_port}/healthz"
  done
  admin_port="$(docker port "$FGA_ADMIN" 8080/tcp | sed -n 's/.*://p')"
  model_dsl="$(smoke_run --rm --network "$SMOKE_NETWORK" "${common_env[@]}" --entrypoint python "$SMOKE_IMAGE" manage.py render_openfga_model)"
  smoke_run --rm "$cli" model transform "$model_dsl" --input-format fga --output-format json > "$FIXTURE_DIR/model.json"
  publication="$(python3 "$SCRIPT_DIR/publish_openfga_model.py" \
    --url "https://localhost:${admin_port}" --ca-file "$FIXTURE_DIR/tls.crt" \
    --token-file "$FIXTURE_DIR/admin-token" --model-file "$FIXTURE_DIR/model.json")"
  read -r OPENFGA_STORE_ID OPENFGA_MODEL_ID <<< "$publication"
  [[ -n "$OPENFGA_STORE_ID" && -n "$OPENFGA_MODEL_ID" ]] || fail 'OpenFGA publication did not return store/model identifiers'

  # Private files must be readable by the image user, never world-readable.
  app_uid="$(smoke_run --rm --entrypoint id "$SMOKE_IMAGE" -u)"
  smoke_run --rm --user 0 --entrypoint chown -v "$FIXTURE_DIR:/smoke-state" "$SMOKE_IMAGE" \
    "$app_uid" /smoke-state/runtime-token /smoke-state/cutover.json /smoke-state/bootstrap.json
  common_env+=(
    -v "$FIXTURE_DIR/runtime-token:/run/secrets/openfga-token:ro"
    -v "$FIXTURE_DIR/tls.crt:/smoke-tls/tls.crt:ro"
    -e "OPENFGA_API_URL=https://${FGA_RUNTIME}:8080"
    -e "OPENFGA_STORE_ID=${OPENFGA_STORE_ID}" -e "OPENFGA_MODEL_ID=${OPENFGA_MODEL_ID}"
    -e OPENFGA_API_TOKEN_FILE=/run/secrets/openfga-token -e OPENFGA_CA_CERT_PATH=/smoke-tls/tls.crt
  )
}
