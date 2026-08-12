#!/usr/bin/env bash
# Apply scripts/migrations.sql to the running Postgres container. Idempotent.
#
# Targets the Compose project in this directory. If you run several instances of this
# stack, pass the project explicitly:  COMPOSE_PROJECT_NAME=other ./scripts/apply_migrations.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

target=$(docker compose ps -q database)
[ -z "$target" ] && { echo "no database container running for this project" >&2; exit 1; }
echo "applying to container ${target:0:12} (database '$DB_DATABASE')"

docker compose exec -T database \
  psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_DATABASE" < scripts/migrations.sql
echo "migrations applied"
