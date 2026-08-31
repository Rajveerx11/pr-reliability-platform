#!/bin/sh
set -eu

POSTGRES_PWD="$(cat /run/secrets/temporal_database_password)"
export POSTGRES_PWD

exec /etc/temporal/entrypoint.sh "$@"
