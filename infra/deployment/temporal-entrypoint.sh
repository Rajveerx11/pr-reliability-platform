#!/bin/sh
set -eu

POSTGRES_PWD="$(cat /run/secrets/postgres_password)"
export POSTGRES_PWD

exec /etc/temporal/entrypoint.sh "$@"
