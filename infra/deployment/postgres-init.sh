#!/bin/sh
set -eu

application_password="$(cat /run/secrets/application_database_password)"
temporal_password="$(cat /run/secrets/temporal_database_password)"

psql --set=ON_ERROR_STOP=1 \
  --set=application_password="$application_password" \
  --set=temporal_password="$temporal_password" \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" <<'SQL'
SELECT format(
  'CREATE ROLE pr_reliability LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
  :'application_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pr_reliability') \gexec
ALTER ROLE pr_reliability LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'application_password';

SELECT 'CREATE DATABASE pr_reliability OWNER pr_reliability'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'pr_reliability') \gexec

SELECT format(
  'CREATE ROLE temporal LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
  :'temporal_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'temporal') \gexec
ALTER ROLE temporal LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'temporal_password';

SELECT 'CREATE ROLE backup_operator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup_operator') \gexec
GRANT pr_reliability, temporal TO backup_operator;
SQL
