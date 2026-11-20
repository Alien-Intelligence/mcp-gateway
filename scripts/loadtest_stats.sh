#!/bin/sh
# Sample the CPU and memory of named containers with `docker stats` into a JSONL file, until a
# stop file appears or a deadline passes. POSIX sh and the docker CLI only.
#
#   sh scripts/loadtest_stats.sh <out.jsonl> <stop-file> <interval-seconds> <deadline-seconds> \
#       <container> [<container>...]
#
# Each output line is {"ts": "<UTC time>", "stats": <one docker stats line>}; summarise it with
# `python -m load.stats_summary` (tests/load/stats_summary.py). A container that goes away is
# not an error: it just stops appearing.
set -eu

if [ "$#" -lt 5 ]; then
    echo "usage: $0 <out.jsonl> <stop-file> <interval-seconds> <deadline-seconds> <container>..." >&2
    exit 2
fi
out=$1
stop=$2
interval=$3
deadline_seconds=$4
shift 4

end=$(($(date +%s) + deadline_seconds))
: >"$out"
while [ ! -e "$stop" ] && [ "$(date +%s)" -lt "$end" ]; do
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    docker stats --no-stream --format '{{json .}}' "$@" |
        while IFS= read -r line; do
            printf '{"ts":"%s","stats":%s}\n' "$now" "$line"
        done >>"$out" || echo "docker stats failed" >&2
    sleep "$interval"
done
