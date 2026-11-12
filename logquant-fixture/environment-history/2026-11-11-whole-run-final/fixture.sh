#!/bin/sh
set -eu
cd "$(dirname "$0")"
compose() { docker compose -f compose.yaml -f compose.lock.yaml "$@"; }
case "${1:-help}" in
  build)
    docker compose build app harness
    python3 scripts/freeze_environment.py
    ;;
  start)
    compose up -d --no-build --pull never --wait --wait-timeout 90 db app
    compose run --rm --no-deps --pull never harness reset
    ;;
  stop) compose stop -t 10 app db ;;
  reset) compose run --rm --no-deps --pull never harness reset ;;
  run|batch|integration)
    if command -v caffeinate >/dev/null 2>&1; then
      export HOST_SLEEP_GUARD=caffeinate
      caffeinate -i -s docker compose -f compose.yaml -f compose.lock.yaml run --rm --no-deps --pull never harness "$@"
    else
      export HOST_SLEEP_GUARD=unavailable
      echo 'Host sleep guard unavailable; timing acceptance remains enforced.' >&2
      compose run --rm --no-deps --pull never harness "$@"
    fi
    ;;
  check-plan) compose run --rm --no-deps --pull never harness "$@" ;;
  test) compose run --rm --no-deps --pull never --entrypoint python harness -m pytest -q ;;
  *) echo 'Usage: ./fixture.sh build|start|stop|reset|run [options]|batch FILE|check-plan FILE|integration|test'; exit 2 ;;
esac
