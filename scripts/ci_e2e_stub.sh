#!/bin/sh
# The stub-target end-to-end run, exactly as the CI job `e2e-stub` runs it. Needs a Docker
# daemon and the compose plugin, nothing else (no Python on the host: busybox sh and the docker
# CLI only), so the same script runs on a laptop, inside a docker:dind container and in CI.
#
#   1. throwaway secrets into .env.stub
#   2. docker compose up --build --wait   (stub target: IdPs, stub upstream, stores, collector, gateways)
#   3. the harness (e2e_stub + integration tests) in a container on the host network of the
#      daemon, so the localhost issuers resolve as on a laptop
#   4. evidence, junit and `docker compose logs` copied to ARTIFACTS_DIR (default: artifacts/),
#      the stack and its volumes removed
#
# Exit status: the harness's, or non-zero when the stack did not come up.
set -eu

ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"
UP_TIMEOUT_SECONDS="${UP_TIMEOUT_SECONDS:-420}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-900}"
ENV_FILE=.env.stub
COMPOSE="docker compose -f compose.yaml -f compose.stub.yaml --env-file $ENV_FILE --profile stub"

. "$(dirname "$0")/ci_common.sh"

mkdir -p "$ARTIFACTS_DIR"
umask 077
{
    throwaway_secrets
    echo "UPSTREAM_STATEFUL_URL=http://upstream-stub:4000/stateful/mcp"
    echo "UPSTREAM_STATELESS_URL=http://upstream-stub:4000/stateless/mcp"
    # policies.rate_limits of the replica pair: a burst small enough for a test to exhaust, a
    # refill slow enough that nothing refills while it runs.
    echo "HA_RATE_LIMITS='{\"per_subject\": {\"burst\": 8, \"per_minute\": 1}, \"per_organization\": {\"burst\": 12, \"per_minute\": 1}}'"
} >"$ENV_FILE"
umask 022

ci_run
