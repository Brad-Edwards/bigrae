#!/bin/bash
set -euo pipefail

# ------------------------------------------------------------------------------
# Guacamole Entrypoint Wrapper
# ------------------------------------------------------------------------------
# Initializes the PostgreSQL database schema if needed, then starts Guacamole.
# This runs on every container start but is idempotent - it checks if the
# schema exists before attempting initialization.

echo "Guacamole entrypoint starting..."

# ------------------------------------------------------------------------------
# Database Schema Initialization (idempotent)
# ------------------------------------------------------------------------------

# Check if we have PostgreSQL connection info
if [[ -n "${POSTGRESQL_HOSTNAME:-}" ]] && [[ -n "${POSTGRESQL_DATABASE:-}" ]]; then
    echo "Checking if Guacamole database schema exists..."

    # Build connection string
    export PGHOST="${POSTGRESQL_HOSTNAME}"
    export PGPORT="${POSTGRESQL_PORT:-5432}"
    export PGDATABASE="${POSTGRESQL_DATABASE}"
    export PGUSER="${POSTGRESQL_USER:-guacamole_admin}"
    export PGPASSWORD="${POSTGRESQL_PASSWORD:-}"

    # Check if schema exists by querying guacamole_user table
    if psql -c "SELECT 1 FROM guacamole_user LIMIT 1" >/dev/null 2>&1; then
        echo "Database schema already exists, skipping initialization"
    else
        echo "Database schema not found, initializing..."

        # Generate and apply schema. ON_ERROR_STOP makes psql exit non-zero
        # when a SQL statement fails, so a partial schema is not a success.
        if /opt/guacamole/bin/initdb.sh --postgresql | psql -v ON_ERROR_STOP=1; then
            echo "Database schema initialized successfully"
        else
            echo "ERROR: Failed to initialize database schema" >&2
            exit 1
        fi
    fi

    # Remove the upstream default administrator. initdb.sh seeds the account
    # "guacadmin" with the published password "guacadmin", and the PostgreSQL
    # extension accepts it on the public /guacamole login. The portal signs every
    # Guacamole session with JSON auth and never uses a password account, so the
    # seed must not exist. This runs on every start, so it also cleans up an
    # existing database. Deleting the entity cascades to the user and its
    # permissions.
    if psql -v ON_ERROR_STOP=1 -c "DELETE FROM guacamole_entity WHERE type = 'USER' AND name = 'guacadmin'"; then
        echo "Default Guacamole administrator account is absent"
    else
        echo "ERROR: Failed to remove the default Guacamole administrator account" >&2
        exit 1
    fi

    # Clean up
    unset PGHOST PGPORT PGDATABASE PGUSER PGPASSWORD
else
    echo "PostgreSQL connection info not provided, skipping schema check"
fi

# ------------------------------------------------------------------------------
# Start Guacamole
# ------------------------------------------------------------------------------

echo "Starting Guacamole..."

# Execute the original CMD from base image (passed as arguments when ENTRYPOINT is set)
exec "$@"
