#!/usr/bin/env bash
# For cron / a host's scheduled job when the web service runs with ENABLE_SCHEDULER=0.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m offmarket run
