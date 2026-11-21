#!/bin/sh
# Checks of charts/mcp-gateway, the same locally and in CI (ci/chart.gitlab-ci.yml):
#   1. helm lint of every values file that must render
#   2. helm template of each of them, rendered to a file (helm's exit status is checked before
#      anything reads the render: a pipe would hide it), then kubeconform -strict against the
#      Kubernetes JSON schemas (and the datree CRDs catalog for the Gateway API HTTPRoute)
#   3. the gateway configuration each render carries, taken from its Secret and run through the
#      gateway's own loader (scripts/chart_config_check.py): what the loader refuses there, the pod
#      would refuse at start. The Deployment must read that Secret. A render that carries no
#      configuration (existingSecret) must say so and its Deployment must read the existing Secret.
#   4. every values file that must NOT render has to make helm template fail with the documented
#      message (a render that succeeds, or fails for another reason, is a failure)
#
# Needs helm, kubeconform and uv on PATH (the project's dev dependencies, pyyaml among them, are
# installed by uv run). KUBERNETES_VERSION is the schema version checked against.
set -eu

CHART_DIR="${CHART_DIR:-charts/mcp-gateway}"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-1.31.0}"
# Pinned to a commit of the catalog: a moving branch would change what CI validates against.
CRDS_CATALOG_COMMIT='784ec895547a36e548e6810b41f27baade3a0890'
CRD_CATALOG="https://raw.githubusercontent.com/datreeio/CRDs-catalog/${CRDS_CATALOG_COMMIT}/"'{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

# values file | text the failing render must contain
EXPECTED_FAILURES='
ha-no-redis-values.yaml|needs the shared state
hpa-no-redis-values.yaml|needs the shared state
grace-too-short-values.yaml|must be greater than the largest upstream read_timeout_seconds
existing-secret-no-shared-state-values.yaml|existingSecret.sharedState=true
no-version-values.yaml|'version'
'

# values file | text of the refusal: these render, and the gateway's loader must refuse them
REFUSED_CONFIGS='
refused-audience-values.yaml|public_url must be listed in auth.audiences
'

# values file | name and key of the existing Secret the Deployment must read (no inline configuration)
NO_INLINE_CONFIG='
existing-secret-values.yaml|mcp-gateway-config|MCP_GATEWAY_CONFIG
'

status=0
failed() {
    echo "FAIL: $*" >&2
    status=1
}

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM

# Render one values file to $WORK_DIR/render.yaml. The exit status of helm is the function's: the
# render is only ever read after helm succeeded.
render() {
    helm template check "$CHART_DIR" -f "$1" --namespace check >"$WORK_DIR/render.yaml"
}

render_ok() {
    for values in "$CHART_DIR"/ci/*-values.yaml; do
        name=$(basename "$values")
        case "$EXPECTED_FAILURES" in
            *"$name|"*) continue ;;
        esac
        echo "== $name: helm lint"
        helm lint "$CHART_DIR" -f "$values" || failed "helm lint $name"
        echo "== $name: helm template, then kubeconform"
        if ! render "$values"; then
            failed "helm template $name"
            continue
        fi
        kubeconform -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
            -schema-location default -schema-location "$CRD_CATALOG" \
            "$WORK_DIR/render.yaml" || failed "kubeconform $name"
    done
}

config_check() {
    for values in "$CHART_DIR"/ci/*-values.yaml; do
        name=$(basename "$values")
        case "$EXPECTED_FAILURES" in
            *"$name|"*) continue ;;
        esac
        echo "== $name: gateway configuration"
        if ! render "$values"; then
            failed "helm template $name"
            continue
        fi
        if refusal=$(echo "$REFUSED_CONFIGS" | grep -F "$name|" | cut -d'|' -f2-) && [ -n "$refusal" ]; then
            set -- --expect-refused "$refusal"
        elif existing=$(echo "$NO_INLINE_CONFIG" | grep -F "$name|" | cut -d'|' -f2-) && [ -n "$existing" ]; then
            set -- --expect-none --secret-name "${existing%%|*}" --secret-key "${existing#*|}"
        else
            set --
        fi
        uv run --frozen --extra dev python -I scripts/chart_config_check.py "$@" \
            <"$WORK_DIR/render.yaml" || failed "gateway configuration $name"
    done
}

render_fails() {
    echo "$EXPECTED_FAILURES" | while IFS='|' read -r name message; do
        [ -n "$name" ] || continue
        echo "== $name: helm template must fail"
        if output=$(helm template check "$CHART_DIR" -f "$CHART_DIR/ci/$name" 2>&1); then
            echo "FAIL: $name rendered, it must not" >&2
            exit 1
        fi
        case "$output" in
            *"$message"*) echo "failed as expected: $message" ;;
            *)
                echo "FAIL: $name failed without \"$message\":" >&2
                echo "$output" >&2
                exit 1
                ;;
        esac
    done || status=1
}

render_ok
config_check
render_fails
exit "$status"
