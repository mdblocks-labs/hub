#!/usr/bin/env python3
"""Upload a built index to one R2 bucket and purge what changed (hub-repo.md §10).

    publish.py OUT --bucket NAME --public-base URL [--dry-run]

OUT holds `hub/v3/`, as `mdb library catalog` writes it. Every file must be
JSON or an image within `block-cards.md` §8's caps and under `hub/v3/`;
nothing in it is executed. Detail files and thumbnails go up before the
listing, so the listing never names a missing file; keys no longer built are
deleted after it. The URLs of deleted and overwritten keys are purged, and
recorded at `hub/purge.json` in the bucket first, so a purge that fails is
retried by the next run. Without
--dry-run it needs AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, R2_ENDPOINT,
CF_PURGE_TOKEN and CF_ZONE_ID.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PREFIX = "hub/v3/"
LISTING = PREFIX + "listing.json"
# Outside PREFIX, so the site never serves it and `remote` never lists it.
PENDING = "hub/purge.json"
HUB_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,38}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
THUMBNAILS = {"icon.svg", "icon.png", "icon-dark.svg", "icon-dark.png", "cover.png", "cover.jpg", "cover.webp"}
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def valid_key(key):
    """A key the catalog writes (block-cards.md §8), the site Worker's rule:
    the listing, `<owner>/<name>/<sha>.json`, or a thumbnail whose folders are
    a member's path under libraries.md's rule."""
    parts = key.removeprefix(PREFIX).split("/")
    if not key.startswith(PREFIX):
        return False
    if parts == ["listing.json"]:
        return True
    if len(parts) < 3 or not (HUB_NAME.match(parts[0]) and HUB_NAME.match(parts[1])):
        return False
    owner, name, file, *rest = parts
    if not rest:
        return file.endswith(".json") and bool(COMMIT.match(file[:-5]))
    folders = rest[:-1]
    member = all(
        p not in ("", ".", "..") and p[1:2] != ":" and "\\" not in p and not CONTROL.search(p) for p in folders
    )
    return bool(COMMIT.match(file)) and rest[-1] in THUMBNAILS and member
IMAGES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
}
MIB = 1024 * 1024


def check(key, data):
    """The content type, or an error naming why the file may not go up."""
    if not valid_key(key):
        raise ValueError(f"{key}: not a path the catalog writes under {PREFIX}")
    suffix = Path(key).suffix
    if suffix == ".json":
        cap = 64 * MIB if key == LISTING else MIB
        if len(data) > cap:
            raise ValueError(f"{key}: over its cap")
        json.loads(data)
        return "application/json"
    if suffix in IMAGES:
        if len(data) > 64 * 1024:
            raise ValueError(f"{key}: a thumbnail over 64 KiB")
        signature = {
            ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": data.startswith(b"\xff\xd8\xff"),
            ".webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
            ".svg": b"<svg" in data[:4096],
        }[suffix]
        if not signature:
            raise ValueError(f"{key}: not the image its name says")
        return IMAGES[suffix]
    raise ValueError(f"{key}: neither JSON nor an image")


def aws(*args):
    done = subprocess.run(
        ["aws", "--endpoint-url", os.environ["R2_ENDPOINT"], "--region", "auto", *args],
        capture_output=True, text=True, check=True,
    )
    return json.loads(done.stdout) if done.stdout.strip() else {}


def remote(bucket):
    """Every key under the prefix with its ETag."""
    keys, token = {}, None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", PREFIX]
        if token:
            args += ["--continuation-token", token]
        page = aws(*args)
        for item in page.get("Contents", []):
            keys[item["Key"]] = item["ETag"].strip('"')
        token = page.get("NextContinuationToken")
        if not token:
            return keys


def put(bucket, key, path, content_type, cache):
    aws("s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path),
        "--content-type", content_type, "--cache-control", cache)


def pending(bucket, tmp):
    """The URLs an earlier run recorded and could not purge."""
    if not aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", PENDING).get("Contents"):
        return []
    aws("s3api", "get-object", "--bucket", bucket, "--key", PENDING, str(tmp))
    urls = json.loads(tmp.read_text())
    tmp.unlink()
    return urls


def purge(urls):
    zone, token = os.environ["CF_ZONE_ID"], os.environ["CF_PURGE_TOKEN"]
    for i in range(0, len(urls), 30):
        request = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
            data=json.dumps({"files": urls[i:i + 30]}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if not json.load(response).get("success"):
                raise RuntimeError("the purge failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--public-base", required=True)
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args()
    root = Path(opts.out)
    built = {}
    for path in sorted((root / PREFIX).rglob("*")):
        if path.is_symlink() or not path.is_file():
            if path.is_symlink():
                sys.exit(f"{path}: a link")
            continue
        key = str(path.relative_to(root))
        data = path.read_bytes()
        built[key] = (data, check(key, data))
    if LISTING not in built:
        sys.exit("no listing was built")
    have = {} if opts.dry_run else remote(opts.bucket)
    tmp = root / ".upload"
    earlier = [] if opts.dry_run else pending(opts.bucket, tmp)
    upload = [k for k, (data, _) in built.items() if have.get(k) != hashlib.md5(data).hexdigest()]
    upload.sort(key=lambda k: k == LISTING)  # the listing last
    delete = sorted(set(have) - set(built))
    url = lambda key: opts.public_base.rstrip("/") + "/" + urllib.parse.quote(key.removeprefix(PREFIX))
    # A recheck rewrites a detail file at its commit-keyed URL. Recorded before
    # any write, so a run that stops part way leaves the full list behind.
    stale = sorted(set(earlier) | {url(k) for k in delete} | {url(k) for k in upload if k in have})
    if stale and not opts.dry_run:
        tmp.write_text(json.dumps(stale))
        put(opts.bucket, PENDING, tmp, "application/json", "no-store")
        tmp.unlink()
    for key in upload:
        print(f"upload {key}")
        if opts.dry_run:
            continue
        data, content_type = built[key]
        cache = "public, max-age=300, must-revalidate" if key == LISTING else "public, max-age=31536000, immutable"
        tmp.write_bytes(data)
        put(opts.bucket, key, tmp, content_type, cache)
        tmp.unlink()
    for key in delete:
        print(f"delete {key}")
        if not opts.dry_run:
            aws("s3api", "delete-object", "--bucket", opts.bucket, "--key", key)
    if stale and not opts.dry_run:
        purge(stale)
        aws("s3api", "delete-object", "--bucket", opts.bucket, "--key", PENDING)
    print(f"{len(upload)} uploaded, {len(delete)} deleted, {len(stale)} purged" + (" (dry run)" if opts.dry_run else ""))


if __name__ == "__main__":
    main()
