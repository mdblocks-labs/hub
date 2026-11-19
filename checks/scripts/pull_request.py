#!/usr/bin/env python3
"""Print GitHub's record of a pull request as `mdb library hub-check
--pull-request` reads it (hub-repo.md §11), from the API and never from the
pull request's content. GH_TOKEN must be set.

    pull_request.py OWNER/REPO NUMBER [REPOSITORY_ID...]
"""
import json
import subprocess
import sys


def api(path, pages=False):
    args = ["gh", "api", path] + (["--paginate", "--slurp"] if pages else [])
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode:
        if '"status":"404"' in done.stdout.replace(" ", "") or "HTTP 404" in done.stderr:
            return None
        raise RuntimeError(f"gh api {path}: {done.stderr}")
    data = json.loads(done.stdout)
    return [item for page in data for item in page] if pages else data


def main():
    repo, number, *ids = sys.argv[1:]
    pr = api(f"repos/{repo}/pulls/{number}")
    commits = api(f"repos/{repo}/pulls/{number}/commits?per_page=100", pages=True)
    reviews = api(f"repos/{repo}/pulls/{number}/reviews?per_page=100", pages=True)
    # The reviewers team is the only grant of write access (hub-repo-ops.md);
    # apps never count as a person.
    reviewers = []
    for login in sorted({r["user"]["login"] for r in reviews if r.get("user")}):
        if login.endswith("[bot]"):
            continue
        permission = api(f"repos/{repo}/collaborators/{login}/permission") or {}
        if permission.get("role_name") in ("admin", "maintain", "write"):
            reviewers.append(login)
    owners = {}
    for repository_id in ids:
        repository = api(f"repositories/{repository_id}")
        owners[repository_id] = repository["owner"]["login"] if repository else None
    head = pr["head"]
    record = {
        "author": pr["user"]["login"],
        "head_branch": head["ref"],
        "head_is_fork": (head.get("repo") or {}).get("full_name") != repo,
        "commits": [
            {
                "author": (c.get("author") or {}).get("login", ""),
                "verified": bool(c["commit"].get("verification", {}).get("verified")),
            }
            for c in commits
        ],
        "head_sha": head["sha"],
        "reviews": [
            {"login": r["user"]["login"], "state": r["state"], "commit": r["commit_id"]}
            for r in reviews
            if r.get("user")
        ],
        "reviewers": reviewers,
        "repository_owners": owners,
    }
    json.dump(record, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
