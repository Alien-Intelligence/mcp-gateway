#!/usr/bin/env bash
# Render the realm template: substitute the placeholders, refuse to print anything if one is
# unset or invalid, or if a placeholder is left in the output.
#
#   REALM_NAME=example-publisher GATEWAY_PUBLIC_URL=https://mcp.example.com/mcp \
#     ./render.sh > realm.json
#
# The template path defaults to realm-template.json next to this script. Only the variables in
# PLACEHOLDERS are substituted. Keycloak's own ${...} keys stay as they are: they are lower or
# camel case (${client_id}, ${rolesScopeConsentText}), a placeholder is upper case.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${1:-$here/realm-template.json}"
PLACEHOLDERS=(REALM_NAME GATEWAY_PUBLIC_URL)

fail() {
  echo "render.sh: $*" >&2
  exit 1
}

[[ -f "$template" ]] || fail "template not found: $template"

for name in "${PLACEHOLDERS[@]}"; do
  [[ -n "${!name:-}" ]] || fail "$name is not set"
done

# Both values end up inside JSON strings, so the patterns also exclude quotes, backslashes and
# whitespace.
[[ "$REALM_NAME" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] \
  || fail "REALM_NAME must be 1 to 64 characters of a-z 0-9 _ - starting with a letter or digit"
[[ "$GATEWAY_PUBLIC_URL" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._~/%-]*[A-Za-z0-9._~%-]$ ]] \
  || fail "GATEWAY_PUBLIC_URL must be the gateway's public_url: http(s), a host, a path that is not /, no query or fragment"

rendered="$(<"$template")"
for name in "${PLACEHOLDERS[@]}"; do
  rendered="${rendered//"\${$name}"/${!name}}"
done

left_pattern='\$\{[A-Z][A-Z0-9_]*\}'
if [[ "$rendered" =~ $left_pattern ]]; then
  fail "placeholder left unsubstituted: ${BASH_REMATCH[0]} (add it to PLACEHOLDERS or remove it from the template)"
fi

printf '%s\n' "$rendered"
