#!/bin/sh
# The real-dev end-to-end run, as the CI job `e2e-dev` runs it: the stack (IdPs, credential
# stores, webhook, collector, gateways) against the real dev MCP server and the Alien dev
# platform. Needs a Docker daemon and the compose plugin, nothing else, so it runs inside a
# docker:dind container as in CI (README, "CI").
#
# Inputs, from CI variables (names only in the README): the personas' key files, the sponsor
# service account's key file and the fixture file as file paths, the upstream URL, the API base
# and an existing job id as plain values. The identity providers' and stores' secrets are
# throwaway, generated per run.
#
#   1. throwaway secrets and the upstream URL into .env.cidev
#   2. docker compose up --build --wait   (compose.ci-dev.yaml lays the state out from the files)
#   3. the real-dev suite (pytest -m e2e) in a container on the host network of the daemon
#   4. evidence, junit and `docker compose logs` copied to ARTIFACTS_DIR, stack and volumes removed
#
# Exit status: the harness's, 2 when an input is missing, otherwise non-zero when the stack did
# not come up.
set -eu

ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"
UP_TIMEOUT_SECONDS="${UP_TIMEOUT_SECONDS:-420}"
RUN_TIMEOUT_SECONDS="${RUN_TIMEOUT_SECONDS:-1500}"
ENV_FILE=.env.cidev
COMPOSE="docker compose -f compose.yaml -f compose.ci-dev.yaml --env-file $ENV_FILE"

FILE_VARIABLES="E2E_DEV_SPONSOR_KEY_FILE E2E_DEV_ALICE_KEY_FILE E2E_DEV_BOB_KEY_FILE E2E_DEV_CAROL_KEY_FILE E2E_DEV_FIXTURE_FILE"
VALUE_VARIABLES="E2E_DEV_UPSTREAM_URL E2E_DEV_API_BASE E2E_DEV_EXISTING_JOB_ID"

check_inputs() {
    problems=""
    for name in $FILE_VARIABLES; do
        eval "value=\${$name:-}"
        if [ -z "$value" ]; then
            problems="$problems $name(unset)"
        elif [ ! -s "$value" ]; then
            problems="$problems $name(not a non-empty file)"
        fi
    done
    for name in $VALUE_VARIABLES; do
        eval "value=\${$name:-}"
        [ -n "$value" ] || problems="$problems $name(unset)"
    done
    case "${E2E_DEV_EXISTING_JOB_ID:-0}" in
        '' | *[!0-9]*) problems="$problems E2E_DEV_EXISTING_JOB_ID(not a number)" ;;
    esac
    if [ -n "$problems" ]; then
        # Names only, never values. Protected variables reach protected branches and tags only.
        echo "the real-dev run needs these CI variables:$problems" >&2
        exit 2
    fi
}

. "$(dirname "$0")/ci_common.sh"

check_inputs
# compose.ci-dev.yaml turns these into secrets copied into the containers (no bind mount: the
# daemon may be remote). They stay in this process's environment, never in an argument list.
for name in $FILE_VARIABLES; do
    eval "path=\$$name"
    eval "export ${name%_FILE}_CONTENT=\"\$(cat \"\$path\")\""
done
export E2E_DEV_API_BASE E2E_DEV_UPSTREAM_URL E2E_DEV_EXISTING_JOB_ID

mkdir -p "$ARTIFACTS_DIR"
umask 077
{
    throwaway_secrets
    # The real dev MCP servers are stateless: both upstream names point at the same URL.
    echo "UPSTREAM_STATEFUL_URL=$E2E_DEV_UPSTREAM_URL"
    echo "UPSTREAM_STATELESS_URL=$E2E_DEV_UPSTREAM_URL"
} >"$ENV_FILE"
umask 022

ci_run
