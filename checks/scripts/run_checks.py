#!/usr/bin/env python3
"""Run hub-repo.md §4's checks on one listed commit and write its results (§6).

    run_checks.py --hub DIR --lib DIR --tree FILE --entry OWNER/NAME
                  --source github:OWNER/REPO --commit SHA [--prev DIR]
                  --tools DIR --out DIR

Writes OUT/<owner>/<name>/<sha>.json ({"checks": [...]}, the layout
`mdb library catalog --checks` and `mdb library hub-check --results` read),
`<sha>.refusals.json`, and with --prev `<sha>.compare.json`. Exits 1 when a
check refuses. A tool that crashes or times out raises, failing the job.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shape  # noqa: E402

CHECKS = [
    ("check", "mdb"), ("export", "mdb"), ("effects", "semgrep"),
    ("dependencies", "osv-scanner"), ("secrets", "gitleaks"), ("codeql", "codeql"),
    ("shape", "hub"), ("licence", "licensee"), ("pickle", "semgrep"), ("refs", "mdb"),
]
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
# What a leaf's code hash leaves out (mdBlocks' `keys.rs`): these folders
# anywhere, and these beside the leaf's block.md.
HASH_EXCLUDED = {".style", "__pycache__", ".git", ".mdb", ".envs", ".venv"}
BESIDE_LEAF = {".views", "site", "tests", "presets", "automations"}


class Refused(Exception):
    """The check refuses: the pull request cannot merge."""


def one_line(text):
    text = " ".join(str(text).split())
    return text if len(text) <= 200 else text[:199] + "…"


def run(args, ok=(0,), timeout=1800, **kw):
    """A tool's output; any other exit code is a crash, which fails the job."""
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
    if done.returncode not in ok:
        raise RuntimeError(f"{args[0]} exited {done.returncode}: {done.stderr[-2000:]}")
    return done


class Library:
    def __init__(self, opts):
        self.opts = opts
        self.lib = Path(opts.lib).resolve()
        self.hub = Path(opts.hub).resolve()
        self.bin = Path(opts.tools).resolve() / "bin"
        self.owner, self.name = opts.entry.split("/")
        tools = tomllib.loads((self.hub / "checks/tools.toml").read_text())["tools"]
        self.version = {key: tool["version"] for key, tool in tools.items()}
        self.semgrep_image = f"semgrep/semgrep@sha256:{tools['semgrep']['sha256']}"
        hub_commit = run(["git", "-C", str(self.hub), "rev-parse", "HEAD"]).stdout.strip()
        self.version["hub"] = hub_commit[:12]
        self.cards = json.loads(
            run([self.mdb, "library", "show", "--path", str(self.lib), "--json"]).stdout
        )
        self.members = {m["path"]: m["card"] for m in self.cards.get("members") or []}
        self.leaves = {
            path: card["executable"]
            for path, card in self.members.items()
            if (card.get("executable") or {}).get("kind") == "leaf"
        }
        self.files = [p for p in self.lib.rglob("*") if p.is_file()]

    @property
    def mdb(self):
        return str(self.bin / "mdb")

    def rel(self, path):
        return str(Path(path).relative_to(self.lib)) if Path(path).is_absolute() else path

    def leaves_of(self, rel):
        return [self.leaves[folder] for folder in self.folders_of(rel)]

    def folders_of(self, rel):
        """The leaf folders whose code a file is: its nearest leaf folder, else
        every leaf whose `code.include` holds it."""
        parts = rel.split("/")
        for i in range(len(parts) - 1, -1, -1):
            folder = "/".join(parts[:i]) or "."
            if folder in self.leaves:
                return [folder]
        found = []
        for folder, leaf in self.leaves.items():
            base = Path(folder if folder != "." else "")
            for include in (leaf.get("code") or {}).get("include") or []:
                root = (self.lib / base / include).resolve()
                if (self.lib / rel).resolve().is_relative_to(root):
                    found.append(folder)
        return found

    def hashes(self, folder, target):
        """Whether the code hash of the leaf at `folder` covers `target`, a
        library-relative path (`keys.rs` `tree_hash`)."""
        parts = target.split("/")
        if parts[0] == ".." or HASH_EXCLUDED & set(parts):
            return False
        base = [] if folder == "." else folder.split("/")
        if parts[:len(base)] == base and len(parts) > len(base):
            return parts[len(base)] not in BESIDE_LEAF
        for include in (self.leaves[folder].get("code") or {}).get("include") or []:
            root = [p for p in os.path.normpath(os.path.join(folder, include)).split("/") if p != "."]
            if parts[:len(root)] == root:
                return True
        return False

    def semgrep(self, config):
        # Semgrep reads no .mts or .cts file, so each is also scanned as a copy
        # under a .ts name; offsets are the same, so a match maps back.
        with tempfile.TemporaryDirectory() as extra:
            for path in self.files:
                if path.suffix in (".mts", ".cts"):
                    copy = Path(extra, f"{path.relative_to(self.lib).as_posix()}.ts")
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, copy)
            done = run(
                ["docker", "run", "--rm", "--network", "none",
                 "-v", f"{self.lib}:/src:ro", "-v", f"{extra}:/extra:ro", "-v", f"{self.hub}/checks/semgrep:/rules:ro",
                 self.semgrep_image, "semgrep", "scan", "--metrics=off", "--disable-version-check",
                 "--no-git-ignore", "--json", "--config", f"/rules/{config}", "/src", "/extra"],
            )
        results = json.loads(done.stdout)
        if results.get("errors"):
            raise RuntimeError(f"semgrep: {results['errors'][0]}")
        for result in results["results"]:
            path = result["path"]
            rel = path.removeprefix("/src/") if path.startswith("/src/") else path.removeprefix("/extra/").removesuffix(".ts")
            data = (self.lib / rel).read_bytes()
            text = data[result["start"]["offset"]:result["end"]["offset"]].decode("utf-8", "replace")
            yield result["check_id"].rsplit(".", 1)[-1], rel, result["start"]["line"], text

    # ── The checks, in §4's order ──────────────────────────────────────────

    def check(self):
        # `mdb check` checks a block tree, so each top-level block is one.
        paths = list(self.members)
        top = ["."] if "." in paths else [p for p in paths if not any(p.startswith(q + "/") for q in paths)]
        diagnostics = list(self.cards["card"].get("diagnostics") or [])
        for path in top:
            done = run([self.mdb, "check", str(self.lib / path), "--json"], ok=(0, 1))
            diagnostics += json.loads(done.stdout or "[]")
        errors = [d for d in diagnostics if d.get("severity") == "error"]
        warnings = [d for d in diagnostics if d.get("severity") == "warning"]
        first = lambda ds: f"{ds[0].get('code')} {ds[0].get('message')}".replace(f"{self.lib}/", "")
        if errors:
            raise Refused(f"{len(errors)} errors; first: {first(errors)}")
        if warnings:
            return "action_required", f"{len(warnings)} warnings; first: {first(warnings)}"
        return "success", f"No errors or warnings in {len(self.members)} blocks"

    def effects(self):
        found = []
        for rule, rel, line, text in self.semgrep("effects"):
            leaves = self.leaves_of(rel)
            if rule.startswith("transform-"):
                if not any(leaf.get("execution") == "transform" for leaf in leaves):
                    continue
            elif rule in ("env-python", "env-js"):
                if not leaves:
                    continue
                literal = re.search(r"""\[\s*["']([A-Z_][A-Z0-9_]*)["']|\(\s*["']([A-Z_][A-Z0-9_]*)["']|\.env\.([A-Za-z_]\w*)""", text)
                name = literal and next(g for g in literal.groups() if g)
                declared = lambda leaf: set(leaf.get("secrets") or []) | {
                    key.get("from_env") for key in leaf.get("config") or [] if isinstance(key, dict)
                }
                if name and all(name in declared(leaf) for leaf in leaves):
                    continue
            elif rule.startswith("env-") and not leaves:
                continue
            elif rule == "import-js-relative":
                spec = re.search(r"""["'](\.\.?/[^"']*)["']""", text)
                target = spec and os.path.normpath(os.path.join(os.path.dirname(rel), spec.group(1)))
                if not target or all(self.hashes(folder, target) for folder in self.folders_of(rel)):
                    continue
            found.append(f"{text.split('(')[0].strip()} in {rel} line {line}")
        if found:
            return "action_required", f"{len(found)} findings; first: {found[0]}"
        return "success", "No undeclared network, file, subprocess or environment use"

    def lockfiles(self, root):
        """`(path, format)` of each lock (lockfile.md §1) below `root`."""
        out = []
        for path in sorted(root.rglob("*.lock")):
            text = path.read_text(errors="replace")
            try:
                tomllib.loads(text)
                out.append((path, "uv.lock"))
            except tomllib.TOMLDecodeError:
                if "lockfileVersion" in text:
                    out.append((path, "pnpm-lock.yaml"))
        return out

    @staticmethod
    def packages(path, fmt):
        text = path.read_text(errors="replace")
        if fmt == "uv.lock":
            lock = tomllib.loads(text)
            return {p["name"] for p in lock.get("package", []) if "registry" in p.get("source", {})}
        names, inside = set(), False
        for line in text.splitlines():
            if re.match(r"^(packages|snapshots):\s*$", line):
                inside = True
            elif re.match(r"^\S", line):
                inside = False
            elif inside and (m := re.match(r"^  '?/?((?:@[^/@\s]+/)?[^@\s/':]+)@", line)):
                names.add(m.group(1))
        return names

    def dependencies(self):
        locks = self.lockfiles(self.lib)
        if not locks:
            return "success", "No lockfiles"
        args = [str(self.bin / "osv-scanner"), "scan", "source", "--format", "json"]
        for path, fmt in locks:
            args += ["-L", f"{fmt}:{path}"]
        done = run(args, ok=(0, 1, 128))
        report = json.loads(done.stdout or "{}")
        for result in report.get("results") or []:
            for package in result.get("packages") or []:
                for vuln in package.get("vulnerabilities") or []:
                    if vuln["id"].startswith("MAL-"):
                        raise Refused(f"Known-malicious package {package['package']['name']} ({vuln['id']})")
        names = set().union(*(self.packages(p, f) for p, f in locks))
        popular = set()
        for list_file in (self.hub / "checks/popular").glob("*.txt"):
            popular |= {l.strip().lower() for l in list_file.read_text().splitlines() if l.strip() and not l.startswith("#")}
        notes = []
        if self.opts.prev:
            before = set().union(set(), *(self.packages(p, f) for p, f in self.lockfiles(Path(self.opts.prev))))
            notes += [f"new package {n}" for n in sorted(names - before)]
        for n in sorted(names):
            key = re.sub(r"[-_.]+", "-", n.lower())
            if key not in popular and any(distance(key, p) <= 2 for p in popular):
                notes.append(f"{n} is close to a popular name")
        if notes:
            return "action_required", f"{len(notes)} to review; first: {notes[0]}"
        return "success", f"{len(locks)} lockfiles, {len(names)} packages, no malicious or new packages"

    def secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "gitleaks.json"
            run([str(self.bin / "gitleaks"), "dir", str(self.lib), "--no-banner", "--redact",
                 "--exit-code", "0", "--report-format", "json", "--report-path", str(report)])
            findings = json.loads(report.read_text() or "[]")
        if findings:
            f = findings[0]
            return "action_required", f"{len(findings)} findings; first: {f['RuleID']} in {self.rel(f['File'])} line {f['StartLine']}"
        return "success", "No secrets found"

    def codeql(self):
        names = [p.name for p in self.files]
        languages = [lang for lang, present in (
            ("python", any(n.endswith(".py") for n in names)),
            ("javascript", any(n.endswith(JS_SUFFIXES) for n in names)),
        ) if present]
        if not languages:
            return "skipped", "The tree holds no Python or JavaScript"
        found = []
        with tempfile.TemporaryDirectory() as tmp:
            for lang in languages:
                db, sarif = Path(tmp) / f"db-{lang}", Path(tmp) / f"{lang}.sarif"
                extractor = "javascript-typescript" if lang == "javascript" else lang
                run([str(self.bin / "codeql"), "database", "create", str(db), f"--language={extractor}",
                     f"--source-root={self.lib}", "--build-mode=none", "--overwrite", "--quiet"])
                run([str(self.bin / "codeql"), "database", "analyze", str(db), "--quiet",
                     "--format=sarif-latest", f"--output={sarif}", "--threads=0",
                     f"codeql/{lang}-queries:codeql-suites/{lang}-security-extended.qls"])
                for result_run in json.loads(sarif.read_text())["runs"]:
                    levels = {
                        rule["id"]: (rule.get("defaultConfiguration") or {}).get("level", "warning")
                        for rule in result_run["tool"]["driver"].get("rules", [])
                    }
                    for r in result_run.get("results", []):
                        if (r.get("level") or levels.get(r["ruleId"], "warning")) not in ("error", "warning"):
                            continue
                        where = r["locations"][0]["physicalLocation"]
                        line = where.get("region", {}).get("startLine", "?")
                        found.append(f"{r['ruleId']} in {where['artifactLocation']['uri']} line {line}")
        if found:
            return "action_required", f"{len(found)} results; first: {found[0]}"
        return "success", f"No error or warning results ({', '.join(languages)})"

    def shape(self):
        # surrogateescape gives a non-UTF-8 name the same str as the file system's.
        tree = Path(self.opts.tree).read_bytes().decode("utf-8", "surrogateescape")
        refusals, flags = shape.check(self.lib, tree)
        if refusals:
            raise Refused(f"{len(refusals)} refused; first: {refusals[0]}")
        if flags:
            return "action_required", f"{len(flags)} flagged; first: {flags[0]}"
        return "success", "No binaries, install hooks, links, or minified or obfuscated files"

    def licence(self):
        # Licence files only: no package metadata or README counts (hub-repo.md §4).
        detect = [str(self.bin / "licensee"), "detect", "--json", "--no-packages", "--no-readme", str(self.lib)]
        report = json.loads(run(detect, ok=(0, 1)).stdout)
        matched = [f for f in report.get("matched_files") or [] if "/" not in f["filename"]]
        if not matched:
            raise Refused("No licence file at the root")
        spdx = matched[0].get("matched_license") or ""
        osi = {l.strip() for l in (self.hub / "checks/osi.txt").read_text().splitlines() if l and not l.startswith("#")}
        if spdx not in osi:
            raise Refused(f"{matched[0]['filename']} is not an OSI-approved licence ({spdx or 'unknown'})")
        declared = self.cards["card"].get("license")
        if declared and declared != spdx:
            return "action_required", f"library.md says {declared}; {matched[0]['filename']} is {spdx}"
        return "success", f"{spdx} in {matched[0]['filename']}"

    def pickle(self):
        found = [
            f"{text.split('(')[0].strip() if rule == 'pickle-load' else text} in {rel} line {line}"
            for rule, rel, line, text in self.semgrep("pickle")
        ]
        if found:
            return "action_required", f"Loads pickle data: {found[0]}" + (f" (and {len(found) - 1} more)" if len(found) > 1 else "")
        return "success", "No pickle loads or pickle-family files"

    def refs(self):
        refs = []
        for preset in sorted(self.lib.glob("**/presets/*.y*ml")):
            inside = False
            for line in preset.read_text(errors="replace").splitlines():
                if re.match(r"^in:\s*$", line):
                    inside = True
                elif re.match(r"^\S", line):
                    inside = False
                elif inside and (m := re.match(r"""^\s+[\w.-]+:\s*["']?((?:hf|s3|https?)://[^\s"']+)""", line)):
                    refs.append((self.rel(preset), m.group(1)))
        if not refs:
            return "skipped", "No external data refs in presets"
        failed = [f"{url} ({status})" for _, url in refs if (status := head(url)) != "ok"]
        if failed:
            return "action_required", f"{len(failed)} of {len(refs)} refs do not resolve; first: {failed[0]}"
        return "success", f"{len(refs)} external refs resolve"

    def export(self, records, out_dir):
        """`mdb library catalog --sources` over this entry and fields.toml
        alone, with every other record in place (hub-repo.md §4)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            entry = tmp / "hub/entries" / self.owner / f"{self.name}.toml"
            entry.parent.mkdir(parents=True)
            shutil.copy(self.hub / "entries" / self.owner / f"{self.name}.toml", entry)
            # The listing's `fields` must name every line's `field` (block-cards.md §8).
            shutil.copy(self.hub / "fields.toml", tmp / "hub/fields.toml")
            sources = tmp / "sources" / self.opts.source.removeprefix("github:") / self.opts.commit
            shutil.copytree(self.lib, sources, symlinks=True)
            # Flags are the merge rule's business; this run asks only whether the cards build.
            checks = tmp / "checks" / self.owner / self.name / f"{self.opts.commit}.json"
            checks.parent.mkdir(parents=True)
            plain = [dict(r, result="success") if r["result"] == "action_required" else r for r in records]
            checks.write_text(json.dumps({"checks": plain}))
            done = run([self.mdb, "library", "catalog", str(tmp / "hub"), str(tmp / "out"),
                        "--checks", str(tmp / "checks"), "--sources", str(tmp / "sources")], ok=(0, 2))
            if done.returncode:
                raise Refused(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "the cards cannot be built")
            detail = tmp / "out/hub/v3" / self.owner / self.name / f"{self.opts.commit}.json"
            members = len(json.loads(detail.read_text())["members"])
        return "success", f"Cards built for {members} blocks"


def distance(a, b):
    """Levenshtein distance, stopping early past 2."""
    if abs(len(a) - len(b)) > 2:
        return 3
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
        if min(row) > 2:
            return 3
    return row[-1]


def head(url):
    """`ok`, or why an unauthenticated HEAD request for an external ref failed."""
    if url.startswith("s3://"):
        bucket, _, key = url[5:].partition("/")
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
    elif url.startswith("hf://"):
        rest = url[5:]
        kind = ""
        if rest.split("/", 1)[0] in ("datasets", "spaces"):
            kind, rest = rest.split("/", 1)
            kind += "/"
        org, name, path = (rest.split("/", 2) + ["", ""])[:3]
        url = f"https://huggingface.co/{kind}{org}/{name}/resolve/main/{path}"
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "mdblocks-hub-checks"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return "ok" if response.status < 400 else str(response.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return type(e).__name__


def main():
    parser = argparse.ArgumentParser()
    for flag in ("hub", "lib", "tree", "entry", "source", "commit", "tools", "out"):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--prev")
    opts = parser.parse_args()
    library = Library(opts)
    out_dir = Path(opts.out) / library.owner / library.name
    out_dir.mkdir(parents=True, exist_ok=True)
    records, refusals = {}, []
    for check, tool in CHECKS:
        if check == "export":
            continue
        try:
            result, reason = getattr(library, check)()
        except Refused as refusal:
            refusals.append({"check": check, "reason": one_line(refusal)})
            continue
        records[check] = {"check": check, "tool": tool, "version": library.version[tool],
                          "result": result, "reason": one_line(reason)}
        print(f"{check:13} {result:15} {records[check]['reason']}")
    if not refusals:
        ordered = [records[c] if c in records else {"check": c, "tool": t, "version": library.version[t],
                   "result": "success", "reason": "Cards built"} for c, t in CHECKS]
        try:
            result, reason = library.export(ordered, out_dir)
            records["export"] = {"check": "export", "tool": "mdb", "version": library.version["mdb"],
                                 "result": result, "reason": one_line(reason)}
            print(f"{'export':13} {result:15} {reason}")
        except Refused as refusal:
            refusals.append({"check": "export", "reason": one_line(refusal)})
    for refusal in refusals:
        print(f"{refusal['check']:13} failure         {refusal['reason']}")
    (out_dir / f"{opts.commit}.refusals.json").write_text(json.dumps(refusals, indent=2) + "\n")
    if refusals:
        return 1
    (out_dir / f"{opts.commit}.json").write_text(
        json.dumps({"checks": [records[c] for c, _ in CHECKS]}, indent=2) + "\n"
    )
    if opts.prev:
        done = run([library.mdb, "library", "compare", opts.prev, str(library.lib), "--json"], ok=(0, 1))
        (out_dir / f"{opts.commit}.compare.json").write_text(done.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
