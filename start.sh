#!/bin/sh
set -e

# NOTE: migrations run via Fly's release_command (see fly.toml), NOT here.
# Running them in the boot path meant a failed migration crash-looped every
# machine into a total outage; as a release_command a failure aborts the
# deploy and the previous version keeps serving.

echo "Starting gunicorn..."
exec gunicorn \
  --bind 0.0.0.0:8080 \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  run:app
