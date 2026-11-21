#!/bin/sh
# Tag the image the job `docker-build` pushed and `image-scan` passed, as the CI job `image-publish`
# runs it. Needs the docker CLI with buildx (the dind service is not used: the tags are written in
# the registry from the digest, nothing is rebuilt or pulled), and runs only after the scan of the
# same digest succeeded (`needs`).
#
# The release tag (<version>, <version>-rc or <version>-dev) is written once: when it already points
# to another digest the script fails and writes nothing; when it already points to this digest (a
# rerun of this job) it is left as it is. The moving tag follows the digest. `imagetools create`
# copies the index as it is, so the SBOM and provenance attestations stay attached.
#
# Required environment (no defaults on purpose):
#   IMAGE_REF, IMAGE_TAG, MOVING_TAG   from the build job's build.env
#   REGISTRY_HOST                      registry host to log in to
#   SCW_ACCESS_KEY, SCW_SECRET_KEY     registry credentials (masked and protected CI variables)
set -eu

. "$(dirname "$0")/ci_image_lib.sh"

require IMAGE_REF IMAGE_TAG MOVING_TAG REGISTRY_HOST SCW_ACCESS_KEY SCW_SECRET_KEY

case "$IMAGE_REF" in
    *@sha256:*) ;;
    *)
        echo "ERROR: IMAGE_REF must name a digest (name@sha256:...), got '$IMAGE_REF'." >&2
        exit 1
        ;;
esac
image="${IMAGE_REF%@*}"
digest="${IMAGE_REF#*@}"

cleanup() {
    remove_login
}
trap cleanup EXIT INT TERM

registry_login

existing=$(tag_digest "$image:$IMAGE_TAG")
if [ -z "$existing" ]; then
    docker buildx imagetools create --tag "$image:$IMAGE_TAG" "$IMAGE_REF"
elif [ "$existing" = "$digest" ]; then
    echo "$image:$IMAGE_TAG already points to $digest."
else
    echo "ERROR: $image:$IMAGE_TAG already points to $existing, not to $digest: a release tag is never overwritten. Bump the version." >&2
    exit 1
fi

docker buildx imagetools create --tag "$image:$MOVING_TAG" "$IMAGE_REF"

for tag in "$IMAGE_TAG" "$MOVING_TAG"; do
    published=$(tag_digest "$image:$tag")
    if [ "$published" != "$digest" ]; then
        echo "ERROR: $image:$tag points to '$published' after publishing, expected $digest." >&2
        exit 1
    fi
done
echo "Published $image:$IMAGE_TAG and $image:$MOVING_TAG at $digest"
