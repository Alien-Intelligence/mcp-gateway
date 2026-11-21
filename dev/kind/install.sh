#!/bin/sh
# Install the chart on a kind cluster with everything it needs, for real: Redis, Keycloak with the
# dev realm, the secrets, the image built from this checkout. Dev only, never a shared cluster.
#
#   dev/kind/install.sh            (from the repository root; reads .env and the state directory)
#
# Inputs (environment, or the file ENV_FILE, default ./.env, for the first six):
#   KC_BOOTSTRAP_ADMIN_USERNAME KC_BOOTSTRAP_ADMIN_PASSWORD E2E_USER_PASSWORD REDIS_PASSWORD
#   UPSTREAM_STATELESS_URL   the real dev MCP the gateway forwards to
#   MCP_GATEWAY_STATE_DIR    the dev fixture's state: mapping.json and the key files it names
#   CLUSTER (default p5-gateway), IMAGE_TAG (default kind), WORK_DIR (secrets are staged here
#   with mode 600 and removed when the script ends)
#
# Secrets are staged as files and handed to kubectl by path: never in an argument list, never
# printed.
set -eu

CLUSTER="${CLUSTER:-p5-gateway}"
IMAGE_TAG="${IMAGE_TAG:-kind}"
WORK_DIR="${WORK_DIR:?set WORK_DIR to a directory that outlives a reboot}"
NAMESPACE=gateway
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
CTX="kind-$CLUSTER"

ENV_FILE="${ENV_FILE:-$ROOT/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ENV_FILE"
    set +a
fi
for name in KC_BOOTSTRAP_ADMIN_USERNAME KC_BOOTSTRAP_ADMIN_PASSWORD E2E_USER_PASSWORD REDIS_PASSWORD UPSTREAM_STATELESS_URL MCP_GATEWAY_STATE_DIR; do
    eval "value=\${$name:-}"
    [ -n "$value" ] || {
        echo "missing $name" >&2
        exit 2
    }
done

umask 077
mkdir -p "$WORK_DIR"
trap 'rm -rf "$WORK_DIR/secrets"' EXIT
mkdir -p "$WORK_DIR/secrets"
S="$WORK_DIR/secrets"

kc() { kubectl --context "$CTX" -n "$NAMESPACE" "$@"; }

if ! kind get clusters | grep -qx "$CLUSTER"; then
    kind create cluster --name "$CLUSTER" --config "$HERE/kind-config.yaml" --wait 120s
fi

docker build -t "mcp-gateway:$IMAGE_TAG" "$ROOT"
for image in "mcp-gateway:$IMAGE_TAG" quay.io/keycloak/keycloak:26.8.2 redis:7.4-alpine; do
    docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
    kind load docker-image --name "$CLUSTER" "$image"
done

kubectl --context "$CTX" create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl --context "$CTX" apply -f -

# The dev realm with two values moved to this cluster: the gateway's audience (the realm names
# the dev stack's http://localhost:18000/mcp) and the registration policy's trusted hosts (the
# anonymous client registration of the harness reaches Keycloak from the pod network's gateway
# address, where the dev stack sees its Docker bridge's).
sed -e 's#http://localhost:18000/mcp#http://localhost:38000/mcp#' \
    -e 's#"172.31.250.1"#"172.31.250.1", "10.244.0.1"#' \
    "$ROOT/dev/keycloak/alien-gateway-dev-realm.json" >"$S/realm.json"
kc create configmap keycloak-realm --from-file=alien-gateway-dev-realm.json="$S/realm.json" \
    --dry-run=client -o yaml | kc apply -f -
# The realm is imported when Keycloak starts: a changed realm needs a new pod.
if kc get deployment keycloak >/dev/null 2>&1; then
    kc rollout restart deployment/keycloak
fi

printf 'KC_BOOTSTRAP_ADMIN_USERNAME=%s\nKC_BOOTSTRAP_ADMIN_PASSWORD=%s\nE2E_USER_PASSWORD=%s\n' \
    "$KC_BOOTSTRAP_ADMIN_USERNAME" "$KC_BOOTSTRAP_ADMIN_PASSWORD" "$E2E_USER_PASSWORD" >"$S/keycloak.env"
kc create secret generic keycloak-env --from-env-file="$S/keycloak.env" --dry-run=client -o yaml | kc apply -f -

printf '%s' "$REDIS_PASSWORD" >"$S/redis-password"
kc create secret generic redis --from-file=password="$S/redis-password" --dry-run=client -o yaml | kc apply -f -

printf 'redis://:%s@redis:6379/0\n' "$REDIS_PASSWORD" >"$S/state-redis-url"
head -c 32 /dev/urandom | base64 >"$S/state-encryption-key"
# Created once: replacing the key would rename every key of the shared state.
if ! kc get secret gateway-secrets >/dev/null 2>&1; then
    kc create secret generic gateway-secrets \
        --from-file=state-redis-url="$S/state-redis-url" \
        --from-file=state-encryption-key="$S/state-encryption-key"
fi

# The static resolver's mapping and the key files it names (relative paths, one directory).
set -- --from-file=mapping.json="$MCP_GATEWAY_STATE_DIR/mapping.json"
for key in $(python3 -I -c "import json,sys; print(' '.join(sorted({e['api_key_file'] for e in json.load(open(sys.argv[1]))['entries']})))" "$MCP_GATEWAY_STATE_DIR/mapping.json"); do
    set -- "$@" --from-file="$key=$MCP_GATEWAY_STATE_DIR/$key"
done
kc create secret generic gateway-state "$@" --dry-run=client -o yaml | kc apply -f -

kubectl --context "$CTX" apply -f "$HERE/infra.yaml"
kc rollout status deployment/redis --timeout=120s
kc rollout status deployment/keycloak --timeout=300s

python3 -I "$HERE/gateway_values.py" "$IMAGE_TAG" "$UPSTREAM_STATELESS_URL" >"$S/values.json"
helm --kube-context "$CTX" upgrade --install gateway "$ROOT/charts/mcp-gateway" -n "$NAMESPACE" \
    -f "$S/values.json" --wait --timeout 180s
