#!/usr/bin/env bash
# Fetch one commit of a GitHub repository as a plain tree without .git/
# (hub-repo.md §4), and write `git ls-tree -z` of it beside, which the shape
# check reads for links and submodules. No hooks, filters or LFS run, and links are
# written as plain files.
#
#   fetch.sh github:OWNER/REPO SHA OUT_DIR LISTING_FILE
set -euo pipefail

source=$1 commit=$2 out=$3 listing=$4
repository=${source#github:}
if [[ $source != github:* || ! $repository =~ ^[a-z0-9_.-]+/[a-z0-9_.-]+$ || ! $commit =~ ^[0-9a-f]{40}$ ]]; then
  echo "fetch.sh: not a source at a commit: $source $commit" >&2
  exit 2
fi

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0
git init --quiet "$out"
git -C "$out" fetch --quiet --depth 1 --no-tags "https://github.com/$repository.git" "$commit"
git -C "$out" ls-tree -r -t -l -z --full-tree FETCH_HEAD >"$listing"
git -C "$out" -c core.symlinks=false -c core.hooksPath=/dev/null checkout --quiet FETCH_HEAD
rm -rf "$out/.git"
