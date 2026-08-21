#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "A test command is required." >&2
  exit 2
fi

if "$@"; then
  exit 0
fi

echo "Redis test suite failed; retrying the complete suite once." >&2
"$@"
