#!/bin/sh
set -eu

case "${SANDBOX_STAGING_DIRECTORY:-}" in
  /run/user/*/pr-reliability-sandbox-staging) ;;
  *) echo "unsafe sandbox staging directory" >&2; exit 125 ;;
esac

find "${SANDBOX_STAGING_DIRECTORY}" -mindepth 1 -maxdepth 1 -type d \
  \( -name 'pr-review-source-*' -o -name 'pr-proof-gate-*' -o \
    -name 'pr-review-checkout-*' -o -name 'pr-review-workspace-*' \) -exec rm -rf -- {} +

find "${SANDBOX_STAGING_DIRECTORY}" -mindepth 1 -maxdepth 1 -type f \
  -name 'pr-review-checkout-*.lock' -exec rm -f -- {} +

exec "$@"
