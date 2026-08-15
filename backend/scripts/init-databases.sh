#!/bin/bash
# Create the databases the project expects, on first container init.
#
#   studium          -- development
#   studium_test     -- integrity and query-shape tests
#   studium_migrate  -- migration round-trip tests, which drop and recreate the
#                       public schema and so must not share with anything else
#
# Idempotent, because the two compose files set a different POSTGRES_DB and
# whichever one that is has already been created by the entrypoint before this
# script runs. CREATE DATABASE has no IF NOT EXISTS, hence the lookup.
set -euo pipefail

for db in studium studium_test studium_migrate; do
    exists=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '${db}'")
    if [ "$exists" != "1" ]; then
        psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
            -c "CREATE DATABASE ${db} OWNER ${POSTGRES_USER}"
        echo "created database ${db}"
    fi
done
