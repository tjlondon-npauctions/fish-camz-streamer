#!/usr/bin/env bash
# Manual-update fallback for Pis that we can't (or don't want to) drive
# remotely. Pulls the latest streamer image from GHCR and recreates the
# containers. Idempotent and safe to run repeatedly.
#
# Use cases:
#  - Onboarding a new Pi while you're physically there
#  - Pi has been offline for a while and you want to catch it up before
#    handing control over to the cloud-driven updater
#  - Cloud-driven update failed and you want to force a known-good image
#
# Usage:
#   bash manual-update.sh                 # pulls :latest
#   bash manual-update.sh 1.6.0           # pulls a specific tag
#   GHCR_PAT=ghp_xxx bash manual-update.sh 1.6.0    # if repo is private
#
# Run from inside the rpie-streamer project directory (where docker-compose.yml lives).

set -euo pipefail

TAG="${1:-latest}"
IMAGE="ghcr.io/tjlondon-npauctions/fish-camz-streamer"
COMPOSE_FILE="docker-compose.yml"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "error: $COMPOSE_FILE not found in $(pwd). cd into the rpie-streamer dir first." >&2
  exit 1
fi

echo "── manual update → ${IMAGE}:${TAG} ──"

# 1. Authenticate if a PAT is provided (private-repo mode).
if [ -n "${GHCR_PAT:-}" ]; then
  echo "Logging in to GHCR..."
  echo "$GHCR_PAT" | docker login ghcr.io -u tjlondon-npauctions --password-stdin
fi

# 2. Pull the requested tag, then re-tag as :latest so docker-compose.yml
#    (which pins :latest) picks it up on `up -d`.
echo "Pulling ${IMAGE}:${TAG}..."
docker pull "${IMAGE}:${TAG}"
if [ "$TAG" != "latest" ]; then
  docker tag "${IMAGE}:${TAG}" "${IMAGE}:latest"
fi

# 3. Show what we have before vs. after so the operator can confirm.
echo
echo "── before (currently running) ──"
docker ps --filter "name=rpie-" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

# 4. Recreate. `up -d` will detect the new :latest digest and recreate any
#    containers using it. We force-recreate to be safe.
echo
echo "Recreating containers..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

# 5. Settle and report.
sleep 5
echo
echo "── after ──"
docker ps --filter "name=rpie-" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

echo
echo "── version reported by web container ──"
docker exec rpie-web cat /app/VERSION 2>/dev/null || echo "(VERSION unavailable yet — give it ~30s to start up)"

echo
echo "Done. Heartbeats will report the new version on the next tick (≤60s)."
