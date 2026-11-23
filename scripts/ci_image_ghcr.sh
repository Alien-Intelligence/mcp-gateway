#!/bin/sh
# Copy the released image to the public registry, as the CI job `ghcr-publish` runs it on `main`.
# The image copied is the digest `docker-build` pushed, `image-scan` passed and `image-publish` tagged
# in our registry: nothing is rebuilt, so the public image is byte for byte the scanned one, with its
# SBOM and provenance attestations (`imagetools create` copies the index and its blobs).
#
# Tags on the public registry, for a release X.Y.Z: X.Y.Z (written once: a tag that already points
# to another digest fails the job and nothing is written), then X.Y, X and latest, which follow.
# Only a plain X.Y.Z release is published (the `main` tag of ci_image_build.sh); -rc and -dev never.
#
# The package is linked to the public repository through the image label
# org.opencontainers.image.source. GitHub creates a new package as private and has no API to change
# a container package's visibility: after the first publication it is made public once, by hand, in
# the package settings (docs/development.md, "Public image").
#
# Required environment (no defaults on purpose):
#   IMAGE_REF, IMAGE_TAG             from the build job's build.env (IMAGE_TAG is X.Y.Z on main)
#   REGISTRY_HOST                    our registry host, to read the image
#   SCW_ACCESS_KEY, SCW_SECRET_KEY   its credentials (masked and protected CI variables)
#   GHCR_IMAGE                       the public image, e.g. ghcr.io/alien-intelligence/mcp-gateway
#   GHCR_USERNAME, GHCR_TOKEN        the GitHub account and its classic token with write:packages
#                                    (protected; the token masked)
set -eu

. "$(dirname "$0")/ci_image_lib.sh"

require IMAGE_REF IMAGE_TAG REGISTRY_HOST SCW_ACCESS_KEY SCW_SECRET_KEY GHCR_IMAGE GHCR_USERNAME GHCR_TOKEN

case "$IMAGE_REF" in
    *@sha256:*) ;;
    *)
        echo "ERROR: IMAGE_REF must name a digest (name@sha256:...), got '$IMAGE_REF'." >&2
        exit 1
        ;;
esac
if ! printf '%s' "$IMAGE_TAG" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: only a release X.Y.Z is published, got '$IMAGE_TAG'." >&2
    exit 1
fi
digest="${IMAGE_REF#*@}"
minor="${IMAGE_TAG%.*}"
major="${IMAGE_TAG%%.*}"

cleanup() {
    remove_login
}
trap cleanup EXIT INT TERM

registry_login
ghcr_host="${GHCR_IMAGE%%/*}"
printf '%s' "$GHCR_TOKEN" | docker login "$ghcr_host" --username "$GHCR_USERNAME" --password-stdin

existing=$(tag_digest "$GHCR_IMAGE:$IMAGE_TAG")
if [ -z "$existing" ]; then
    docker buildx imagetools create --tag "$GHCR_IMAGE:$IMAGE_TAG" "$IMAGE_REF"
elif [ "$existing" = "$digest" ]; then
    echo "$GHCR_IMAGE:$IMAGE_TAG already points to $digest."
else
    echo "ERROR: $GHCR_IMAGE:$IMAGE_TAG already points to $existing, not to $digest: a release tag is never overwritten." >&2
    exit 1
fi
for tag in "$minor" "$major" latest; do
    docker buildx imagetools create --tag "$GHCR_IMAGE:$tag" "$GHCR_IMAGE:$IMAGE_TAG"
done

for tag in "$IMAGE_TAG" "$minor" "$major" latest; do
    published=$(tag_digest "$GHCR_IMAGE:$tag")
    if [ "$published" != "$digest" ]; then
        echo "ERROR: $GHCR_IMAGE:$tag points to '$published' after publishing, expected $digest." >&2
        exit 1
    fi
done
echo "Published $GHCR_IMAGE:$IMAGE_TAG, :$minor, :$major and :latest at $digest"
