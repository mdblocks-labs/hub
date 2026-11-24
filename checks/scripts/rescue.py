#!/usr/bin/env python3
"""Rescue forks (hub-repo.md §9): after a merge, fork each newly listed
library into `mdblocks-hub` as `<owner>--<name>` and push the tag
`hub/<commit>` for its listed commit, and delete every fork no entry lists. Fetches with Git and never checks a
tree out. RESCUE_TOKEN is an installation token of the rescue app on
`mdblocks-hub`; failures are printed, one per line, for an issue.

    rescue.py      (run in a checkout of this repository)
"""
import json
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

ORG = "mdblocks-hub"


def git(*args, **kw):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kw).stdout


def listed(revision):
    """`{"owner/name": (source, commit)}` at a revision of this repository."""
    out = {}
    names = git("ls-tree", "-r", "--name-only", revision, "--", "entries/").split()
    for path in names:
        entry = tomllib.loads(git("show", f"{revision}:{path}"))
        coordinate = path.removeprefix("entries/").removesuffix(".toml")
        out[coordinate] = (entry["source"].removeprefix("github:"), entry["reviewed"][-1]["commit"])
    return out


def api(token, method, path, body=None):
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def rescue(token, coordinate, upstream, commit):
    owner, name = coordinate.split("/")
    fork = f"{owner}--{name}"
    if not upstream.startswith(f"{ORG}/") and api(token, "GET", f"repos/{ORG}/{fork}") is None:
        api(token, "POST", f"repos/{upstream}/forks",
            {"organization": ORG, "name": fork, "default_branch_only": True})
        for _ in range(30):
            if api(token, "GET", f"repos/{ORG}/{fork}"):
                break
            time.sleep(5)
        else:
            raise RuntimeError("the fork did not appear")
        api(token, "PATCH", f"repos/{ORG}/{fork}", {
            "has_issues": False,
            "description": f"Rescue copy of github.com/{upstream} for the mdBlocks Hub. Not maintained here.",
        })
    with tempfile.TemporaryDirectory() as tmp:
        git("init", "--bare", "--quiet", tmp)
        git("-C", tmp, "fetch", "--quiet", "--no-tags", f"https://github.com/{upstream}.git", commit)
        git("-C", tmp, "push", "--quiet", f"https://x-access-token:{token}@github.com/{ORG}/{fork}.git",
            f"{commit}:refs/tags/hub/{commit}")


def forks(token):
    """The names of every repository in the organisation."""
    names, page = [], 1
    while True:
        batch = api(token, "GET", f"orgs/{ORG}/repos?per_page=100&page={page}") or []
        names += [r["name"] for r in batch]
        if len(batch) < 100:
            return names
        page += 1


def main():
    import os
    token = os.environ["RESCUE_TOKEN"]
    # Every run reconciles with the latest main, not with the push that
    # started it, so a skipped, late or re-run job leaves nothing behind.
    git("fetch", "--quiet", "origin", "main")
    latest = listed("FETCH_HEAD")
    if not latest:
        # An empty read is far likelier a fault than a Hub with nothing listed.
        print("FAILED the latest main lists no entry; nothing was deleted", file=sys.stderr)
        return 1
    wanted = {coordinate.replace("/", "--"): coordinate for coordinate in latest}
    failures = []
    for fork in sorted(set(forks(token)) - set(wanted)):
        try:
            api(token, "DELETE", f"repos/{ORG}/{fork}")
            print(f"deleted {ORG}/{fork}")
        except Exception as e:  # the removal stands; the issue asks a maintainer to delete it
            failures.append(f"{fork}: deleting it: {type(e).__name__}: {str(e).replace(token, '***')}")
    for fork, coordinate in sorted(wanted.items()):
        upstream, commit = latest[coordinate]
        if api(token, "GET", f"repos/{ORG}/{fork}/git/ref/tags/hub/{commit}") is not None:
            continue
        try:
            rescue(token, coordinate, upstream, commit)
            print(f"rescued {coordinate} at {commit}")
        except Exception as e:  # each entry stands alone; the listing never waits
            failures.append(f"{coordinate} at {commit}: {type(e).__name__}: {str(e).replace(token, '***')}")
    for failure in failures:
        print(f"FAILED {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
