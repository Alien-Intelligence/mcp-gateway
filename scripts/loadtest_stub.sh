#!/bin/sh
# Load test of the gateway on the stub target: N concurrent long-lived SSE streams through a
# gateway and straight to the upstream stub, the same workload one after the other. Needs a Docker
# daemon and the compose plugin, nothing else (busybox sh and the docker CLI), so it runs on a
# laptop and inside a docker:dind container like scripts/ci_e2e_stub.sh.
#
#   1. throwaway secrets into .env.stub, stack up (as ci_e2e_stub.sh)
#   2. scripts/loadtest_stats.sh samples CPU and memory of the gateway containers
#   3. the load client (compose.load.yaml, host network) measures the direct stub, then the gateway
#   4. summary.json, raw-*.csv, stats.jsonl, stats-summary.json and the compose logs are copied to
#      ARTIFACTS_DIR, the stack and its volumes removed
#
# Required, no default (they define the measurement):
#   LOAD_CONCURRENCY            concurrent streams (workers, one stream each)
#   LOAD_REQUESTS_PER_WORKER    measured tools/call per worker
#   LOAD_WARMUP_REQUESTS        warmup tools/call per worker, excluded from the statistics
#   LOAD_PERSONAS               space-separated stub personas, e.g. "alice bob carol dave"
#   LOAD_TOOL                   e.g. slow_sse
#   LOAD_ARGUMENTS_JSON         e.g. '{"count": 10, "delay_seconds": 0.5}'
#   LOAD_EXPECTED_EVENTS        events of one reply stream (slow_sse: count + 1)
#   LOAD_TIMEOUT_SECONDS        per request
#   LOAD_MAX_RUN_SECONDS        wall-clock ceiling of the measurement
# Documented defaults (what is under test, not how it is loaded):
#   LOAD_GATEWAY_SERVICES       gateway-keycloak   compose services whose containers are sampled
#   LOAD_GATEWAY_URL            http://localhost:18000/mcp   the gateway the clients connect to
#   LOAD_ISSUER                 http://localhost:18080/realms/alien-gateway-dev   its OAuth issuer
#   LOAD_DIRECT_URL             http://127.0.0.1:14000/stateless/mcp   the baseline, the stub
#   LOAD_STATS_INTERVAL_SECONDS 2
#   ARTIFACTS_DIR               artifacts
#   UP_TIMEOUT_SECONDS          420
#
# Exit status: the load client's, or non-zero when the stack did not come up.
set -eu

: "${LOAD_CONCURRENCY:?set LOAD_CONCURRENCY}"
: "${LOAD_REQUESTS_PER_WORKER:?set LOAD_REQUESTS_PER_WORKER}"
: "${LOAD_WARMUP_REQUESTS:?set LOAD_WARMUP_REQUESTS}"
: "${LOAD_PERSONAS:?set LOAD_PERSONAS}"
: "${LOAD_TOOL:?set LOAD_TOOL}"
: "${LOAD_ARGUMENTS_JSON:?set LOAD_ARGUMENTS_JSON}"
: "${LOAD_EXPECTED_EVENTS:?set LOAD_EXPECTED_EVENTS}"
: "${LOAD_TIMEOUT_SECONDS:?set LOAD_TIMEOUT_SECONDS}"
: "${LOAD_MAX_RUN_SECONDS:?set LOAD_MAX_RUN_SECONDS}"
LOAD_GATEWAY_SERVICES="${LOAD_GATEWAY_SERVICES:-gateway-keycloak}"
LOAD_GATEWAY_URL="${LOAD_GATEWAY_URL:-http://localhost:18000/mcp}"
LOAD_ISSUER="${LOAD_ISSUER:-http://localhost:18080/realms/alien-gateway-dev}"
LOAD_DIRECT_URL="${LOAD_DIRECT_URL:-http://127.0.0.1:14000/stateless/mcp}"
LOAD_STATS_INTERVAL_SECONDS="${LOAD_STATS_INTERVAL_SECONDS:-2}"

ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"
UP_TIMEOUT_SECONDS="${UP_TIMEOUT_SECONDS:-420}"
ENV_FILE=.env.stub
COMPOSE="docker compose -f compose.yaml -f compose.stub.yaml -f compose.load.yaml --env-file $ENV_FILE --profile stub"
STATS_FILE="$ARTIFACTS_DIR/stats.jsonl"
STOP_FILE="$ARTIFACTS_DIR/stats.stop"
sampler_pid=

. "$(dirname "$0")/ci_common.sh"

load_cleanup() {
    touch "$STOP_FILE"
    if [ -n "$sampler_pid" ]; then
        wait "$sampler_pid" 2>/dev/null || true
    fi
    docker rm -f load-run >/dev/null 2>&1 || true
    ci_cleanup
    rm -f "$STOP_FILE"
}

mkdir -p "$ARTIFACTS_DIR"
rm -f "$STOP_FILE"
umask 077
{
    throwaway_secrets
    echo "UPSTREAM_STATEFUL_URL=http://upstream-stub:4000/stateful/mcp"
    echo "UPSTREAM_STATELESS_URL=http://upstream-stub:4000/stateless/mcp"
    echo "LOADTEST_GIT_COMMIT=${LOADTEST_GIT_COMMIT:-}"
    # The replica pair is not under load; the stack requires the variable, null means no rate limits.
    echo "HA_RATE_LIMITS=null"
} >"$ENV_FILE"
umask 022

trap load_cleanup EXIT
load_cleanup

echo "== bringing the stack up"
if ! timeout "$UP_TIMEOUT_SECONDS" $COMPOSE up --build --wait --wait-timeout "$UP_TIMEOUT_SECONDS"; then
    echo "the stack did not become healthy" >&2
    ci_collect
    exit 1
fi
$COMPOSE ps

containers=
for service in $LOAD_GATEWAY_SERVICES; do
    ids=$($COMPOSE ps -q "$service")
    if [ -z "$ids" ]; then
        echo "no running container for the service $service" >&2
        exit 1
    fi
    containers="$containers $ids"
done
rm -f "$STOP_FILE"
# The sampler outlives the run by one interval at most; its own deadline bounds a lost stop file.
sh "$(dirname "$0")/loadtest_stats.sh" "$STATS_FILE" "$STOP_FILE" "$LOAD_STATS_INTERVAL_SECONDS" \
    "$((LOAD_MAX_RUN_SECONDS + 300))" $containers &
sampler_pid=$!

echo "== running the load test"
status=0
timeout "$((LOAD_MAX_RUN_SECONDS + 300))" $COMPOSE run --name load-run load \
    python -m load.loadtest \
    --target-url "direct=$LOAD_DIRECT_URL" --target-auth direct=stub-key \
    --target-url "gateway=$LOAD_GATEWAY_URL" --target-auth "gateway=oauth:$LOAD_ISSUER" \
    --baseline-target direct \
    --state-dir /run/gateway/state \
    --personas $LOAD_PERSONAS \
    --concurrency "$LOAD_CONCURRENCY" \
    --requests-per-worker "$LOAD_REQUESTS_PER_WORKER" \
    --warmup-requests "$LOAD_WARMUP_REQUESTS" \
    --tool "$LOAD_TOOL" \
    --arguments-json "$LOAD_ARGUMENTS_JSON" \
    --expected-events "$LOAD_EXPECTED_EVENTS" \
    --timeout-seconds "$LOAD_TIMEOUT_SECONDS" \
    --max-run-seconds "$LOAD_MAX_RUN_SECONDS" \
    --out-dir /out || status=$?
docker cp load-run:/out/. "$ARTIFACTS_DIR/" || echo "no load output to copy" >&2

touch "$STOP_FILE"
wait "$sampler_pid" 2>/dev/null || true
sampler_pid=
if [ -s "$STATS_FILE" ]; then
    timeout 120 $COMPOSE run -T --rm --no-deps load python -m load.stats_summary - - \
        <"$STATS_FILE" >"$ARTIFACTS_DIR/stats-summary.json" || echo "no stats summary" >&2
fi
rm -f "$STOP_FILE"
ci_collect
echo "== load client exit status: $status"
exit "$status"
