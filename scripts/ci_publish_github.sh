#!/bin/sh
# Publish `main` and the release tags reachable from it to the public GitHub repository, as the CI
# job `publish-github` runs it. Nothing else is ever published: not dev, not staging, not a tag whose
# commit is not on main.
#
# Steps:
#   1. fetch main and the tags from the canonical repository (this checkout's origin);
#   2. keep the v* tags whose commit is an ancestor of main;
#   3. copy exactly those refs into a scratch bare repository and run the history deny-list scan
#      (scripts/ci_history_denylist.sh) over it: the scan covers exactly what is about to be published
#      and any hit stops the publication;
#   4. push them over SSH with GitHub's published host keys pinned (no trust on first use), as a plain
#      push: GitHub's main must be an ancestor of ours, a rewritten or diverged history is refused.
#
# Required environment (no defaults on purpose):
#   GITHUB_PUBLISH_DEPLOY_KEY   path of a file holding the base64 of the deploy key's private key
#                               (File-type CI variable, protected and masked, so main pipelines only)
#   HISTORY_DENYLIST_FILE       the deny-list (File-type CI variable, see ci_history_denylist.sh)
#   GITHUB_PUBLISH_REPO         the target, e.g. git@github.com:Alien-Intelligence/mcp-gateway.git
# Optional:
#   CI_COMMIT_TAG               set by GitLab on a tag pipeline: a tag that is not on main publishes
#                               nothing and succeeds; a tag on main is published with main.
#   PUBLISH_DRY_RUN             "true": do everything except the push
set -eu

here=$(cd "$(dirname "$0")" && pwd)

require() {
    for name in "$@"; do
        eval "value=\${$name:-}"
        if [ -z "$value" ]; then
            echo "ERROR: $name is not set." >&2
            exit 2
        fi
    done
}

require GITHUB_PUBLISH_REPO
for tool in git ssh perl base64; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed." >&2; exit 2; }
done

git fetch --quiet --no-tags origin '+refs/heads/main:refs/remotes/origin/main'
git fetch --quiet --force origin '+refs/tags/*:refs/tags/*'
main=$(git rev-parse --verify 'refs/remotes/origin/main^{commit}')

if [ -n "${CI_COMMIT_TAG:-}" ] && ! git merge-base --is-ancestor "refs/tags/$CI_COMMIT_TAG" "$main"; then
    echo "Tag $CI_COMMIT_TAG is not on main: nothing to publish."
    exit 0
fi

# Both are protected variables: a main pipeline has them, an unprotected tag pipeline does not (it
# stopped above unless its tag is on main, and then the missing key fails it loudly).
require GITHUB_PUBLISH_DEPLOY_KEY HISTORY_DENYLIST_FILE

tags=""
for tag in $(git for-each-ref --format='%(refname:strip=2)' 'refs/tags/v*'); do
    if git merge-base --is-ancestor "refs/tags/$tag" "$main"; then
        tags="$tags $tag"
    fi
done

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM

# Exactly what will be published, nothing else, for the scan.
git init --quiet --bare "$work/publish.git"
refspecs="$main:refs/heads/main"
for tag in $tags; do
    refspecs="$refspecs refs/tags/$tag:refs/tags/$tag"
done
git push --quiet "$work/publish.git" $refspecs
echo "To publish: main at $main, tags:${tags:- none}"
(cd "$work/publish.git" && sh "$here/ci_history_denylist.sh")

if [ "${PUBLISH_DRY_RUN:-false}" = "true" ]; then
    echo "Dry run: not pushed."
    exit 0
fi

# GitHub's SSH host keys, from https://api.github.com/meta (ssh_keys), whose fingerprints GitHub
# publishes at https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
# (ED25519 SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU). A changed host key fails the push.
cat >"$work/known_hosts" <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF

(umask 077 && base64 -d "$GITHUB_PUBLISH_DEPLOY_KEY" >"$work/deploy_key")
export GIT_SSH_COMMAND="ssh -i $work/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$work/known_hosts -o BatchMode=yes"

# Plain push: never --force. Refused if GitHub holds anything that is not an ancestor of ours.
(cd "$work/publish.git" && git push "$GITHUB_PUBLISH_REPO" refs/heads/main:refs/heads/main $(for tag in $tags; do printf 'refs/tags/%s:refs/tags/%s ' "$tag" "$tag"; done))
echo "Published main at $main and tags:${tags:- none}"
