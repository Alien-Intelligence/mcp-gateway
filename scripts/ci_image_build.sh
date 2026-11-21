#!/bin/sh
# Build the multi-arch image (linux/amd64 + linux/arm64) and push it BY DIGEST ONLY, as the CI job
# `docker-build` runs it. Needs a Docker daemon (the dind service in CI) and the docker CLI with
# buildx; busybox sh only, so the same script runs on a laptop against a local registry (README,
# "Container image").
#
# No tag is written here. The image exists in the registry as a bare digest until the job
# `image-publish` (scripts/ci_image_publish.sh) has seen the scan of that digest pass and applies
# the tags: a failed scan leaves nothing published under a release tag, and a rerun can never
# overwrite the tag a release points to.
#
# Tags follow the other Alien images (applied by ci_image_publish.sh): main -> <version> and latest,
# staging -> <version>-rc and latest-staging, dev -> <version>-dev and latest-dev. The version is the
# one in pyproject.toml, which on the release branch is the release bot's commit. A <version> tag
# that already exists is never rebuilt over: this script refuses, before building, and says to bump.
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
#   BUILDX_CREATE_ARGS  extra arguments of `docker buildx create` (a laptop with a registry on
#                       localhost: "--driver-opt network=host")
#   BUILD_ENV_FILE      where to write IMAGE_REF, IMAGE_TAG and MOVING_TAG for the jobs after this
#                       one (default: build.env)
#   REGISTRY_INSECURE   "true" for a plain-HTTP local registry
set -eu

. "$(dirname "$0")/ci_image_lib.sh"

# The toolchain is pinned by digest: the binfmt installer that registers the arm64 emulator, and
# the BuildKit that builds. Bump them on purpose.
BINFMT_IMAGE="tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
BUILDKIT_IMAGE="moby/buildkit:v0.23.2@sha256:ddd1ca44b21eda906e81ab14a3d467fa6c39cd73b9a39df1196210edcb8db59e"
PLATFORMS="linux/amd64,linux/arm64"

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
work_dir=$(mktemp -d)
created_builder=""
cleanup() {
    if [ -n "$created_builder" ]; then
        docker buildx rm --force "$created_builder" >/dev/null 2>&1 || true
    fi
    remove_login
    rm -rf "$work_dir"
}
trap cleanup EXIT INT TERM

registry_login

# Fail before a 45 minute build: a release tag is written once. A different image under the same
# version is a version that was not bumped.
existing=$(tag_digest "$image:$tag")
if [ -n "$existing" ]; then
    echo "ERROR: $image:$tag already exists ($existing) and is never overwritten: bump the version (a merge to the release branch does it)." >&2
    exit 1
fi

docker run --privileged --rm "$BINFMT_IMAGE" --install arm64 >/dev/null

if [ -z "${BUILDX_BUILDER:-}" ]; then
    BUILDX_BUILDER=ci-builder
    # BUILDX_CREATE_ARGS is a list of arguments: left unquoted on purpose
    docker buildx create --name "$BUILDX_BUILDER" --driver docker-container \
        --driver-opt "image=$BUILDKIT_IMAGE" ${BUILDX_CREATE_ARGS:-} --bootstrap >/dev/null
    created_builder="$BUILDX_BUILDER"
fi
export BUILDX_BUILDER

output="type=image,name=$image,push-by-digest=true,name-canonical=true,push=true"
metadata_file="$work_dir/buildx-metadata.json"
if [ "${REGISTRY_INSECURE:-false}" = "true" ]; then
    output="$output,registry.insecure=true"
fi

# The SBOM and the provenance are attestations stored next to the image in the registry; the scan job
# publishes a CycloneDX SBOM per architecture as a job artifact.
echo "Building $image ($PLATFORMS) for the tags $tag and $moving_tag, pushed by digest only"
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

# The digest of what was pushed (the multi-arch index), as the build itself recorded it. Not
# `imagetools inspect --format '{{.Manifest.Digest}}'`: buildx v0.27.0 (docker:28.3.3-cli, the CI image)
# prints its human listing for this index (two platforms plus attestations) instead.
digest=$(sed -n 's/.*"containerimage\.digest": *"\(sha256:[0-9a-f]\{64\}\)".*/\1/p' "$metadata_file" | head -n 1)
case "$digest" in
    sha256:*) ;;
    *)
        echo "ERROR: could not read the pushed digest of $image (got '$digest')." >&2
        exit 1
        ;;
esac

# Later jobs scan and tag exactly this digest, never a tag that could move under them.
{
    echo "IMAGE_REF=$image@$digest"
    echo "IMAGE_TAG=$tag"
    echo "MOVING_TAG=$moving_tag"
} >"${BUILD_ENV_FILE:-build.env}"
echo "Pushed $image@$digest (to be tagged $tag and $moving_tag once the scan passes)"
