#!/bin/bash
# Bring up the Zuul + git-stream demo environment in podman.
#
#   ./deploy.sh          build what's missing, start everything, wait until ready
#   ./deploy.sh --open   also open the demo UI and Zuul in the browser
#
# The first run fetches Zuul's source and builds its images (about 25
# minutes); later runs start in under a minute.  ./clean.sh removes it all.
# The first run also creates .env with random secrets (see .env.example).
set -eu
source "$(dirname "$0")/common.sh"

ensure_podman
ensure_zuul_images
prepare_secrets
# Read by every job (zuul.conf [executor] variables); post-logs uses it to
# build log links that work from other computers.
printf 'demo_host: %s\n' "$DEMO_HOST" > etc_zuul/site-variables.yaml
say "Building git-stream..."
podman build -q -t localhost/zuul-git-stream:latest -f Containerfile .. >/dev/null
say "Starting the containers..."
compose up -d >/dev/null
# "compose up" keeps running containers on their old image; restart the
# two that use the image just built (it's quick).
compose up -d --force-recreate --no-deps git-stream demo-ui >/dev/null
wait_ready

printf '\n'
say "${B}Demo UI${R}     $UI      announce bundles, watch jobs"
say "${B}git-stream${R}  $FORGE      the fake GitLab: projects, merge requests, Zuul's comments"
say "${B}Zuul${R}        $ZUUL/status"
say "${B}Job logs${R}    $LOGS"
say "${B}Events${R}      POST $FORGE/api/stream"
if [ "${DEMO_BIND:-127.0.0.1}" != 127.0.0.1 ] && [ "$DEMO_HOST" != localhost ]; then
    printf '\n'
    say "From other computers on the network:"
    say "  Demo UI     http://$DEMO_HOST:8080"
    say "  git-stream  http://$DEMO_HOST:8090"
    say "  Zuul        http://$DEMO_HOST:9000/t/example-tenant/status"
    say "  Job logs    http://$DEMO_HOST:8000"
fi
printf '\n'
say "Guided walkthrough: ./demo.sh    Pause: ./stop.sh    Remove everything: ./clean.sh"

if [ "${1:-}" = "--open" ]; then
    open "$UI" 2>/dev/null || xdg-open "$UI" 2>/dev/null || true
    open "$ZUUL/status" 2>/dev/null || xdg-open "$ZUUL/status" 2>/dev/null || true
fi
