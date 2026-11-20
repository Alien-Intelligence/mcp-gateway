#!/bin/sh
# The stub-target end-to-end run, exactly as the CI job `e2e-stub` runs it. Needs a Docker
# daemon and the compose plugin, nothing else (no Python on the host: busybox sh and the docker
# CLI only), so the same script runs on a laptop, inside a docker:dind container and in CI.
#
#   1. throwaway secrets into .env.stub
#   2. docker compose up --build --wait   (stub target: IdPs, stub upstream, stores, gateways)
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

random_secret() {
    # 32 alphanumeric characters
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
}

mkdir -p "$ARTIFACTS_DIR"
umask 077
cat >"$ENV_FILE" <<EOF
KC_BOOTSTRAP_ADMIN_USERNAME=admin
KC_BOOTSTRAP_ADMIN_PASSWORD=$(random_secret)
E2E_USER_PASSWORD=$(random_secret)
HYDRA_SYSTEM_SECRET=$(random_secret)
POSTGRES_PASSWORD=$(random_secret)
REDIS_PASSWORD=$(random_secret)
WEBHOOK_HMAC_SECRET=$(random_secret)
UPSTREAM_STATEFUL_URL=http://upstream-stub:4000/stateful/mcp
UPSTREAM_STATELESS_URL=http://upstream-stub:4000/stateless/mcp
EOF
umask 022

collect() {
    $COMPOSE --profile harness ps --all >"$ARTIFACTS_DIR/compose-ps.txt" 2>&1 || true
    $COMPOSE --profile harness logs --no-color --timestamps >"$ARTIFACTS_DIR/compose-logs.txt" 2>&1 || true
}

cleanup() {
    docker rm -f e2e-run >/dev/null 2>&1 || true
    $COMPOSE --profile harness down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "== bringing the stack up"
if ! timeout "$UP_TIMEOUT_SECONDS" $COMPOSE up --build --wait --wait-timeout "$UP_TIMEOUT_SECONDS"; then
    echo "the stack did not become healthy" >&2
    collect
    exit 1
fi
$COMPOSE ps

echo "== running the harness"
status=0
timeout "$RUN_TIMEOUT_SECONDS" $COMPOSE --profile harness run --name e2e-run harness || status=$?
docker cp e2e-run:/out/. "$ARTIFACTS_DIR/" || echo "no harness output to copy" >&2
collect
echo "== harness exit status: $status"
exit "$status"
