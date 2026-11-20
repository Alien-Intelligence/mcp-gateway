#!/bin/sh
# Steps shared by the CI end-to-end scripts (ci_e2e_stub.sh, ci_e2e_dev.sh). Sourced, never run.
# The caller sets COMPOSE (the docker compose command line, env file included), ARTIFACTS_DIR,
# UP_TIMEOUT_SECONDS and RUN_TIMEOUT_SECONDS before calling ci_run.

random_secret() {
    # 32 alphanumeric characters
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
}

# The throwaway secrets of the stack (identity providers, credential stores, webhook signature),
# one NAME=value per line; the caller appends its upstream URLs and redirects into its env file.
throwaway_secrets() {
    cat <<SECRETS
KC_BOOTSTRAP_ADMIN_USERNAME=admin
KC_BOOTSTRAP_ADMIN_PASSWORD=$(random_secret)
E2E_USER_PASSWORD=$(random_secret)
HYDRA_SYSTEM_SECRET=$(random_secret)
POSTGRES_PASSWORD=$(random_secret)
REDIS_PASSWORD=$(random_secret)
WEBHOOK_HMAC_SECRET=$(random_secret)
SECRETS
}

ci_collect() {
    $COMPOSE --profile harness ps --all >"$ARTIFACTS_DIR/compose-ps.txt" 2>&1 || true
    $COMPOSE --profile harness logs --no-color --timestamps >"$ARTIFACTS_DIR/compose-logs.txt" 2>&1 || true
}

ci_cleanup() {
    docker rm -f e2e-run >/dev/null 2>&1 || true
    $COMPOSE --profile harness down --volumes --remove-orphans --timeout 20 >/dev/null 2>&1 || true
}

# Bring the stack up, run the harness container, keep the evidence, tear everything down.
# Exit status: the harness's, or non-zero when the stack did not come up.
ci_run() {
    trap ci_cleanup EXIT
    ci_cleanup

    echo "== bringing the stack up"
    if ! timeout "$UP_TIMEOUT_SECONDS" $COMPOSE up --build --wait --wait-timeout "$UP_TIMEOUT_SECONDS"; then
        echo "the stack did not become healthy" >&2
        ci_collect
        exit 1
    fi
    $COMPOSE ps

    echo "== running the harness"
    status=0
    timeout "$RUN_TIMEOUT_SECONDS" $COMPOSE --profile harness run --name e2e-run harness || status=$?
    docker cp e2e-run:/out/. "$ARTIFACTS_DIR/" || echo "no harness output to copy" >&2
    ci_collect
    echo "== harness exit status: $status"
    exit "$status"
}
