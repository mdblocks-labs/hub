#!/usr/bin/env python3
"""The build's check records (hub-repo.md §6, "Where the records come from").

    published.py fetch HUB BASE_URL OUT
        Write each listed commit's records from its published detail file to
        OUT/<owner>/<name>/<sha>.json, and print `missing=<json>` (for
        $GITHUB_OUTPUT): the listed commits the build checks itself, those with
        no records or with records from another tool version than
        checks/tools.toml.

    published.py combine HUB PUBLISHED FRESH OUT
        Take each fresh result, except one that is action_required for a check
        the published one is not: keep the published one, since the reviewer
        approved only the flags they saw, and print a note for the maintainers.
        A new flag delists nothing.
"""
import json
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECTS = urllib.request.build_opener(_Refuse)
# Cloudflare refuses urllib's default agent with 403.
NO_REDIRECTS.addheaders = [("User-Agent", "mdblocks-hub-build")]


def entries(hub):
    for path in sorted(Path(hub, "entries").glob("*/*.toml")):
        entry = tomllib.loads(path.read_text())
        yield path.parent.name, path.stem, entry


def fetch(hub, base_url, out):
    tools = tomllib.loads(Path(hub, "checks/tools.toml").read_text())["tools"]
    versions = {key: tool["version"] for key, tool in tools.items()}
    missing = []
    for owner, name, entry in entries(hub):
        reviewed = entry["reviewed"]
        sha = reviewed[-1]["commit"]
        url = f"{base_url.rstrip('/')}/{owner}/{name}/{sha}.json"
        try:
            with NO_REDIRECTS.open(url, timeout=30) as response:
                checks = json.load(response)["checks"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                # A redirect or a refusal is a login page or a moved bucket, never a record.
                sys.exit(f"{url} answers {e.code}; the build reads the published index, served publicly without redirects")
            checks = None
        if checks:
            # Saved even when rechecked: `combine` keeps it if the recheck flags something new.
            target = Path(out, owner, name, f"{sha}.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"checks": checks}, indent=2) + "\n")
        stale = checks and any(
            r["tool"] != "hub" and r["version"] != versions.get(r["tool"]) for r in checks
        )
        if checks and not stale:
            continue
        missing.append({
            "entry": f"{owner}/{name}",
            "source": entry["source"],
            "commit": sha,
            "previous": reviewed[-2]["commit"] if len(reviewed) > 1 else "",
        })
    print(f"missing={json.dumps(missing)}")


def combine(hub, published, fresh, out):
    shutil.copytree(published, out, dirs_exist_ok=True)
    for path in sorted(Path(fresh).glob("*/*/*.json")):
        if path.name.count(".") > 1:  # .compare.json, .refusals.json
            continue
        key = str(path.relative_to(fresh)).removesuffix(".json")
        checks = json.loads(path.read_text())["checks"]
        target = Path(out, f"{key}.json")
        if target.exists():
            approved = {r["check"] for r in json.loads(target.read_text())["checks"] if r["result"] == "action_required"}
            new = [r["check"] for r in checks if r["result"] == "action_required" and r["check"] not in approved]
            if new:
                print(f"{key}: a recheck flags {', '.join(new)}, which the reviewer did not approve; the earlier records stay")
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, target)


def main():
    command, *args = sys.argv[1:]
    if command == "fetch":
        fetch(*args)
    elif command == "combine":
        combine(*args)
    else:
        sys.exit(f"unknown command {command}")


if __name__ == "__main__":
    main()
