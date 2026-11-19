# mdBlocks Hub

This repository is the list of libraries in the mdBlocks Hub. mdBlocks is a
tool for building data workflows from blocks described in plain text files. A
library is a Git repository of such blocks, and the Hub is where people find
and copy them.

The Hub hosts no code. Each file in `entries/` points at one reviewed commit of
an author's own repository, and everything the Hub says about that commit
comes from here: that it was reviewed, which checks ran on it, and whether it
was later removed.

## Publishing a library

Run `mdb library publish` in your library's repository. It opens a pull
request here for you. Don't open one by hand: only pull requests from the
publish service can merge without a maintainer.

Before a library is listed, CI checks the commit: `mdb check`, the cards the
Hub shows, declared effects against the code, dependencies, secrets, CodeQL,
file shapes, the licence, pickle loading and external data links. A
maintainer reviews every library's first publication and any update that
changes code, dependencies or declared effects. An update that only changes
descriptions or icons merges on its own once the checks pass.

The full rules are in the [Hub repository specification](https://mdblocks.com/docs/reference/specs/hub-repo).

## What is here

| Path | What it holds |
|---|---|
| `entries/<owner>/<name>.toml` | One listed library and its reviewed commits |
| `removed/<owner>/<name>.toml` | Its removals, kept forever |
| `fields.toml` | The Field → Domain → Application list a library chooses from |
| `checks/` | Pinned tool versions, scan rules and the check scripts |
| `.github/` | The workflows and the Report form |

## Reporting a library

Every Hub page has a Report link to this repository's
[issue form](https://github.com/mdblocks-labs/hub/issues/new?template=report.yml).
For a security problem that should not be public, see [SECURITY.md](SECURITY.md).

