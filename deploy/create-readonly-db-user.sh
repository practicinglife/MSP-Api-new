#!/bin/bash
# Creates a read-only PostgreSQL user for direct DB queries (DBeaver, reporting tools).
# Run once on the server, from the project folder:
#   bash deploy/create-readonly-db-user.sh                 # generates a random password
#   bash deploy/create-readonly-db-user.sh '<password>'    # or choose one
#
# Note: a read-only user can still read everything in the database (device inventory, audit log, hashed
# keys, encrypted credentials). Postgres is bound to 127.0.0.1 by docker-compose.yml — reach it through an
# SSH tunnel rather than opening port 5432 on the network.
set -euo pipefail

DB="msp_tools"
USER="msp_readonly"
PASS="${1:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)}"
HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo "=== Creating read-only user: $USER on db: $DB ==="

sudo docker compose exec -T postgres psql -U postgres -d "$DB" <<SQL
-- Create user (idempotent)
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$USER') THEN
    CREATE USER $USER WITH PASSWORD '$PASS';
  ELSE
    ALTER USER $USER WITH PASSWORD '$PASS';
  END IF;
END
\$\$;

GRANT CONNECT ON DATABASE $DB TO $USER;
GRANT USAGE ON SCHEMA public TO $USER;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO $USER;

SELECT 'Done. User $USER created with SELECT on all tables.' AS result;
SQL

echo ""
echo "=== Connection details (store the password in your password manager) ==="
echo "  Host:     ${HOST:-<server address>}   (Postgres listens on 127.0.0.1 — use an SSH tunnel)"
echo "  Port:     5432"
echo "  Database: $DB"
echo "  User:     $USER"
echo "  Password: $PASS"
echo ""
echo "SSH tunnel, then connect to localhost:5432:"
echo "  ssh -L 5432:127.0.0.1:5432 <user>@${HOST:-<server>}"
