#!/bin/sh
# Regenerate btcusdt.html with fresh broker prices.
#
# Written to a temporary file and moved into place, so a browser reloading the
# page never catches a half-written file.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

.venv/bin/python -m taskqueue.exchange > btcusdt.html.tmp
mv -f btcusdt.html.tmp btcusdt.html

printf '%s  refreshed %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$REPO/btcusdt.html"
