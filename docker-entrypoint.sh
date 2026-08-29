#!/bin/sh
set -eu

chown appuser:appuser /data
exec gosu appuser "$@"
