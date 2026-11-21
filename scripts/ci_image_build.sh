#!/bin/sh
# Build the multi-arch image (linux/amd64 + linux/arm64) and push it, as the CI job `docker-build`
# runs it. Needs a Docker daemon (the dind service in CI) and the docker CLI with buildx; busybox sh
# only, so the same script runs on a laptop against a local registry (README, "Container image").
#
# Tags follow the other Alien images: main -> <version> and latest, staging -> <version>-rc and
# latest-staging, dev -> <version>-dev and latest-dev. The version is the one in pyproject.toml, which
# on the release branch is the release bot's commit.
#
# Required environment (no defaults on purpose):
#   REGISTRY            registry path, e.g. registry.example.com/team
#   REGISTRY_HOST       registry host to log in to
#   IMAGE_NAME          image name under REGISTRY
#   CI_COMMIT_BRANCH    main, staging or dev
#   CI_COMMIT_SHA       revision recorded in the image labels
#   SCW_ACCESS_KEY, SCW_SECRET_KEY   registry credentials (masked and protected CI variables)
# Optional:
#   BUILDX_BUILDER      reuse this builder (it must reach the registry and run arm64) instead of
#                       creating one
#   BUILD_ENV_FILE      where to write IMAGE_REF and IMAGE_TAG for the jobs after this one
#                       (default: build.env)
#   REGISTRY_INSECURE   "true" for a plain-HTTP local registry
set -eu

# The binfmt installer that registers the arm64 emulator, pinned by digest.
BINFMT_IMAGE="tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
PLATFORMS="linux/amd64,linux/arm64"

require() {
    for name in "$@"; do
        eval "value=\${$name:-}"
        if [ -z "$value" ]; then
            echo "ERROR: $name is not set." >&2
            exit 1
        fi
    done
}

require REGISTRY REGISTRY_HOST IMAGE_NAME CI_COMMIT_BRANCH CI_COMMIT_SHA SCW_ACCESS_KEY SCW_SECRET_KEY

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
if [ -z "$version" ]; then
    echo "ERROR: no version in pyproject.toml." >&2
    exit 1
fi

case "$CI_COMMIT_BRANCH" in
    main) tag="$version"; moving_tag="latest" ;;
    staging) tag="$version-rc"; moving_tag="latest-staging" ;;
    dev) tag="$version-dev"; moving_tag="latest-dev" ;;
    *)
        echo "ERROR: branch '$CI_COMMIT_BRANCH' publishes no image (main, staging and dev do)." >&2
        exit 1
        ;;
esac

image="$REGISTRY/$IMAGE_NAME"
printf '%s' "$SCW_SECRET_KEY" | docker login "$REGISTRY_HOST" --username "$SCW_ACCESS_KEY" --password-stdin

docker run --privileged --rm "$BINFMT_IMAGE" --install arm64 >/dev/null

if [ -z "${BUILDX_BUILDER:-}" ]; then
    BUILDX_BUILDER=ci-builder
    docker buildx create --name "$BUILDX_BUILDER" --driver docker-container --bootstrap >/dev/null
fi
export BUILDX_BUILDER

output="type=image,name=$image:$tag,push=true"
metadata_file="${TMPDIR:-/tmp}/buildx-metadata.$$.json"
if [ "${REGISTRY_INSECURE:-false}" = "true" ]; then
    output="$output,registry.insecure=true"
fi

# The SBOM and the provenance are attestations stored next to the image in the registry; the scan job
# publishes a CycloneDX SBOM per architecture as a job artifact.
echo "Building $image:$tag ($PLATFORMS), moving tag $moving_tag"
docker buildx build \
    --platform "$PLATFORMS" \
    --build-arg "VERSION=$version" \
    --build-arg "VCS_REF=$CI_COMMIT_SHA" \
    --build-arg "BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --sbom=true \
    --provenance=mode=max \
    --output "$output" \
    --metadata-file "$metadata_file" \
    .

docker buildx imagetools create --tag "$image:$moving_tag" "$image:$tag"

# The digest of what was pushed (the multi-arch index), as the build itself recorded it. Not
# `imagetools inspect --format '{{.Manifest.Digest}}'`: buildx v0.27.0 (docker:28.3.3-cli, the CI image)
# prints its human listing for this index (two platforms plus attestations) instead.
digest=$(sed -n 's/.*"containerimage\.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$metadata_file" | head -n 1)
case "$digest" in
    sha256:*) ;;
    *)
        echo "ERROR: could not read the pushed digest of $image:$tag (got '$digest')." >&2
        exit 1
        ;;
esac

# Later jobs scan and promote exactly this digest, never a tag that could move under them.
{
    echo "IMAGE_REF=$image@$digest"
    echo "IMAGE_TAG=$tag"
} >"${BUILD_ENV_FILE:-build.env}"
echo "Pushed $image:$tag and $image:$moving_tag at $digest"
