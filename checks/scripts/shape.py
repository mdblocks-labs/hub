"""The `shape` check (hub-repo.md §4): this repository's own rules over a
library tree and its `git ls-tree -r -t -l -z` listing."""
import json
import re
from pathlib import Path

MAX_FILES = 10_000
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
PICKLE_SUFFIXES = (".pkl", ".pickle", ".pt", ".pth", ".bin", ".ckpt", ".joblib")
CODE_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
# PNG, JPEG, GIF, Parquet and Arrow IPC; WebP is checked apart.
SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PAR1", b"ARROW1")
HOOKS = {"preinstall", "install", "postinstall", "prepare", "prepublish"}
LFS = b"version https://git-lfs.github.com/spec/v1"
# A multi-line literal (a one-line one is caught as minified) of base64 or hex.
LONG_LITERAL = re.compile(r'("""|\'\'\'|`)([A-Za-z0-9+/=\s]{1000,})\1')
# A computed argument to a code-running call; a literal may carry an r/b prefix.
COMPUTED_CODE = re.compile(r"\b(eval|exec|compile|Function|__import__)\s*\(\s*(?![rRbB]{0,2}[\"'`]|\))")


def listing(text):
    """`(mode, type, size, path)` for each record of `git ls-tree -r -t -l -z`,
    whose paths are unquoted."""
    for record in text.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, kind, _sha, size = meta.split()
        yield mode, kind, size, path


def check(lib: Path, tree_text: str):
    """Returns `(refusals, flags)`, each a list of one-line strings."""
    refusals, flags = [], []
    blobs = []
    for mode, kind, size, path in listing(tree_text):
        if mode == "120000":
            refusals.append(f"{path}: a symlink")
        elif kind == "commit":
            refusals.append(f"{path}: a submodule")
        elif kind == "blob":
            blobs.append((int(size), path))
    if len(blobs) > MAX_FILES:
        refusals.append(f"{len(blobs)} files, more than {MAX_FILES:,}")
    total = sum(size for size, _ in blobs)
    if total > MAX_TOTAL:
        refusals.append(f"{total:,} bytes in all, more than 256 MiB")
    for size, path in blobs:
        name = path.rsplit("/", 1)[-1].lower()
        if size > MAX_FILE:
            refusals.append(f"{path}: over 64 MiB")
            continue
        if name.endswith(PICKLE_SUFFIXES):
            refusals.append(f"{path}: a {name.rsplit('.', 1)[-1]} file")
        if name in ("setup.py", ".gitmodules"):
            refusals.append(f"{path}: an install hook")
        data = (lib / path).read_bytes()
        head = data[:8192]
        if data.startswith(LFS):
            refusals.append(f"{path}: a Git LFS pointer")
            continue
        webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if b"\0" in head and not (head.startswith(SIGNATURES) or webp):
            refusals.append(f"{path}: a binary file")
            continue
        if name == ".gitattributes" and re.search(rb"\bfilter\s*=", data):
            refusals.append(f"{path}: names a filter")
        if name == "package.json":
            try:
                scripts = json.loads(data).get("scripts") or {}
            except (ValueError, AttributeError):
                scripts = {}
            if isinstance(scripts, dict) and HOOKS & set(scripts):
                refusals.append(f"{path}: an install hook ({', '.join(sorted(HOOKS & set(scripts)))})")
        if name.endswith(CODE_SUFFIXES):
            text = data.decode("utf-8", "replace")
            if any(len(line) > 1000 for line in text.splitlines()):
                refusals.append(f"{path}: minified, a line over 1,000 characters")
                continue
            long = any(
                len(re.sub(r"\s", "", m.group(2))) >= 1000 for m in LONG_LITERAL.finditer(text)
            )
            if long or COMPUTED_CODE.search(text):
                flags.append(f"{path}: obfuscated, a long encoded literal or computed code")
    return refusals, flags
