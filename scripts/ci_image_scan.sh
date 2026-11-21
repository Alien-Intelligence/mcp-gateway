#!/bin/sh
# Scan the image the job `docker-build` pushed, both architectures, as the CI job `image-scan` runs
# it. Needs the trivy binary (the aquasec/trivy image in CI, a container on a laptop) and nothing else.
#
# Per architecture it writes a CycloneDX SBOM and a full JSON vulnerability report to ARTIFACTS_DIR,
# then fails on any HIGH or CRITICAL vulnerability that has a fix available. Unfixed findings stay in
# the report and do not fail the job: nothing the project can do about them but wait.
#
# Required environment (no defaults on purpose):
#   IMAGE_REF           registry/name@sha256:<digest> (from the build job's build.env)
#   SCW_ACCESS_KEY, SCW_SECRET_KEY   registry credentials
# Optional:
#   ARTIFACTS_DIR       default: artifacts
#   REGISTRY_INSECURE   "true" for a plain-HTTP local registry
set -eu

ARTIFACTS_DIR="${ARTIFACTS_DIR:-artifacts}"

for name in IMAGE_REF SCW_ACCESS_KEY SCW_SECRET_KEY; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "ERROR: $name is not set." >&2
        exit 1
    fi
done

case "$IMAGE_REF" in
    *@sha256:*) ;;
    *)
        echo "ERROR: IMAGE_REF must name a digest (name@sha256:...), got '$IMAGE_REF'." >&2
        exit 1
        ;;
esac

export TRIVY_USERNAME="$SCW_ACCESS_KEY"
export TRIVY_PASSWORD="$SCW_SECRET_KEY"
export TRIVY_NO_PROGRESS=true
insecure=""
if [ "${REGISTRY_INSECURE:-false}" = "true" ]; then
    insecure="--insecure"
fi

mkdir -p "$ARTIFACTS_DIR"
status=0
for arch in amd64 arm64; do
    platform="linux/$arch"
    trivy image $insecure --image-src remote --platform "$platform" --format cyclonedx \
        --output "$ARTIFACTS_DIR/sbom-$arch.cdx.json" "$IMAGE_REF"
    trivy image $insecure --image-src remote --platform "$platform" --scanners vuln \
        --format json --output "$ARTIFACTS_DIR/vulnerabilities-$arch.json" "$IMAGE_REF"
    echo "== $platform: HIGH and CRITICAL with a fix available"
    if ! trivy image $insecure --image-src remote --platform "$platform" --scanners vuln \
        --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMAGE_REF"; then
        status=1
    fi
done
exit "$status"
