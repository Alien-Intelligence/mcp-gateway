#!/bin/sh
# Steps shared by the image scripts (ci_image_build.sh, ci_image_publish.sh). Sourced, never run.

require() {
    for name in "$@"; do
        eval "value=\${$name:-}"
        if [ -z "$value" ]; then
            echo "ERROR: $name is not set." >&2
            exit 1
        fi
    done
}

# Log in to $REGISTRY_HOST with a configuration directory of its own ($LOGIN_CONFIG; the caller's
# exit trap calls remove_login): the credentials never reach ~/.docker/config.json of a laptop. The
# builders stay in the buildx configuration the caller already had (BUILDX_CONFIG), so BUILDX_BUILDER
# still resolves. Select a remote daemon with DOCKER_HOST: the docker contexts live in the
# configuration directory that is replaced here.
registry_login() {
    BUILDX_CONFIG="${BUILDX_CONFIG:-${DOCKER_CONFIG:-$HOME/.docker}/buildx}"
    export BUILDX_CONFIG
    LOGIN_CONFIG=$(mktemp -d)
    DOCKER_CONFIG="$LOGIN_CONFIG"
    export DOCKER_CONFIG
    printf '%s' "$SCW_SECRET_KEY" | docker login "$REGISTRY_HOST" \
        --username "$SCW_ACCESS_KEY" --password-stdin
}

# The digest a tag of the registry points to, on stdout: empty when the tag does not exist. Any
# other failure of the registry is an error, never "absent". The digest is that of the raw
# manifest, as the registry serves it.
tag_digest() {
    if ! raw=$(docker buildx imagetools inspect --raw "$1" 2>&1); then
        case "$raw" in
            *"not found"* | *"NAME_UNKNOWN"* | *"MANIFEST_UNKNOWN"*) return 0 ;;
        esac
        echo "ERROR: could not look up $1: $raw" >&2
        return 1
    fi
    printf '%s' "$raw" | sha256sum | sed 's/^\([0-9a-f]\{64\}\) .*/sha256:\1/'
}

# Remove the configuration directory registry_login created, and only that one.
remove_login() {
    if [ -n "${LOGIN_CONFIG:-}" ]; then
        rm -rf "$LOGIN_CONFIG"
    fi
}
