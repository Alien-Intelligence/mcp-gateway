#!/usr/bin/env bash
# Create the secrets directory with fresh random values. Refuses to touch an existing one.
#
#   ./init-secrets.sh [directory]        (default: ./secrets next to this script)
#
# Layout:
#   <directory>/                     0700: closed to every other account of the host
#     redis.conf                     0644  "requirepass <password>", given to Redis as a secret
#     gateway/                       0755  mounted read-only into the gateway at /run/mcp-gateway/secrets
#       state-redis-url              0644  redis://:<password>@redis:6379/0 (state.url_file)
#       state-encryption-key         0644  base64 of 32 random bytes (state.encryption_key_file)
#       keys/mapping.json            0644  the static mapping; you add one file per API key next to it
#
# The containers run as uid 1000 (gateway) and 999 (Redis), not as your account, so they need
# read access to the files and the gateway/ directory. A bind mount does not pass through the
# parent directory, so the 0700 parent still keeps the other accounts of the host out.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$here/secrets}"
if [[ -e "$target" ]]; then
  echo "init-secrets.sh: $target exists; remove it first to rotate everything at once" >&2
  exit 1
fi

umask 077
mkdir -p "$target/gateway/keys"
chmod 0700 "$target"

password="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
if [[ ${#password} -ne 64 ]]; then
  echo "init-secrets.sh: could not read 32 random bytes from /dev/urandom" >&2
  exit 1
fi

printf 'requirepass %s\n' "$password" >"$target/redis.conf"
printf 'redis://:%s@redis:6379/0\n' "$password" >"$target/gateway/state-redis-url"
head -c 32 /dev/urandom | base64 >"$target/gateway/state-encryption-key"
cp "$here/mapping.example.json" "$target/gateway/keys/mapping.json"

chmod 0644 "$target/redis.conf" "$target/gateway/state-redis-url" \
  "$target/gateway/state-encryption-key" "$target/gateway/keys/mapping.json"
chmod 0755 "$target/gateway" "$target/gateway/keys"
echo "created $target"
echo "next: edit $target/gateway/keys/mapping.json and write each API key it names into"
echo "      $target/gateway/keys/ (one key per file, mode 0644)"
