#!/bin/sh

set -eu

ARCADEDB_URL="${ARCADEDB_URL:-http://localhost:2480}"
ARCADEDB_USER="${ARCADEDB_USER:-root}"
ARCADEDB_PASSWORD="${ARCADEDB_PASSWORD:-playwithdata}"
ARCADEDB_DATABASE="${ARCADEDB_DATABASE:-platform}"

SCHEMA_PATH = Path(
    "/app/arcadedb/init/001_schema.sql"
)
echo "Waiting for ArcadeDB..."

until wget \
    --user="${ARCADEDB_USER}" \
    --password="${ARCADEDB_PASSWORD}" \
    --spider \
    "${ARCADEDB_URL}/api/v1/ready" \
    >/dev/null 2>&1
do
    sleep 2
done

echo "ArcadeDB is ready."

echo "Checking database '${ARCADEDB_DATABASE}'..."

DATABASES="$(
    wget \
        --user="${ARCADEDB_USER}" \
        --password="${ARCADEDB_PASSWORD}" \
        -qO- \
        "${ARCADEDB_URL}/api/v1/databases"
)"

case "${DATABASES}" in
    *"\"${ARCADEDB_DATABASE}\""*)
        echo "Database '${ARCADEDB_DATABASE}' already exists."
        ;;
    *)
        echo "Creating database '${ARCADEDB_DATABASE}'..."

        wget \
            --user="${ARCADEDB_USER}" \
            --password="${ARCADEDB_PASSWORD}" \
            -qO- \
            --header="Content-Type: application/json" \
            --post-data="{\"command\":\"create database ${ARCADEDB_DATABASE}\"}" \
            "${ARCADEDB_URL}/api/v1/server"

        echo
        echo "Database created."
        ;;
esac

echo "Schema file: ${SCHEMA_FILE}"
echo "Applying schema..."

# Remove comments and split SQL statements on ';'.
# The first CREATE DATABASE statement is intentionally skipped.
awk '
    BEGIN {
        RS=";"
        ORS=""
    }

    {
        gsub(/--[^\n]*/, "", $0)

        if ($0 ~ /^[[:space:]]*$/) {
            next
        }

        if (toupper($0) ~ /^[[:space:]]*CREATE[[:space:]]+DATABASE/) {
            next
        }

        gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)

        if ($0 != "") {
            print $0 "\n"
        }
    }
' "${SCHEMA_FILE}" > /tmp/schema-statements.sql

while IFS= read -r statement; do
    if [ -z "${statement}" ]; then
        continue
    fi

    echo "Applying: ${statement}"

    wget \
        --user="${ARCADEDB_USER}" \
        --password="${ARCADEDB_PASSWORD}" \
        -qO- \
        --header="Content-Type: application/json" \
        --post-data="{\"language\":\"sql\",\"command\":$(printf '%s' "${statement}" | sed 's/\\/\\\\/g; s/"/\\"/g; s/$/\\n/; s/^/"/; s/$/"/')}" \
        "${ARCADEDB_URL}/api/v1/command/${ARCADEDB_DATABASE}"

    echo
done < /tmp/schema-statements.sql

echo "ArcadeDB bootstrap completed."