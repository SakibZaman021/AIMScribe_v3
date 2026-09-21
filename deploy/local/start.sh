#!/bin/sh
# AIMScribe - local stack
cd "$(dirname "$0")"
exec python3 bootstrap.py "$@"
