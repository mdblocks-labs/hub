#!/usr/bin/env bash
# Download the pinned tools (checks/tools.toml) and check each file's SHA-256
# (hub-repo.md §4.1). Installs into DEST/bin; semgrep is a pinned container image.
#
#   install-tools.sh TOOLS_TOML DEST [TOOL...]     all six when none are named
set -euo pipefail

toml=$1 dest=$2
shift 2
mkdir -p "$dest/bin" "$dest/downloads"

pin() {
  python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["tools"][sys.argv[2]][sys.argv[3]])' "$toml" "$1" "$2"
}

# fetch URL SHA256 FILE: download, then refuse a file whose hash differs.
fetch() {
  curl -fsSL --retry 3 --proto '=https' -o "$3" "$1"
  echo "$2  $3" | sha256sum --check --quiet -
}

tools=("$@")
[ ${#tools[@]} -gt 0 ] || tools=(mdb semgrep osv-scanner gitleaks codeql licensee)
for tool in "${tools[@]}"; do
  version=$(pin "$tool" version)
  sha=$(pin "$tool" sha256)
  file="$dest/downloads/$tool"
  case $tool in
    mdb)
      target=mdb-v$version-x86_64-unknown-linux-musl
      fetch "https://downloads.mdblocks.com/releases/v$version/$target.tar.gz" "$sha" "$file"
      tar -xzf "$file" -C "$dest/downloads"
      install -m 0755 "$dest/downloads/$target/mdb" "$dest/bin/mdb"
      ;;
    semgrep)
      docker pull --quiet "semgrep/semgrep@sha256:$sha" >/dev/null
      ;;
    osv-scanner)
      fetch "https://github.com/google/osv-scanner/releases/download/v$version/osv-scanner_linux_amd64" "$sha" "$file"
      install -m 0755 "$file" "$dest/bin/osv-scanner"
      ;;
    gitleaks)
      fetch "https://github.com/gitleaks/gitleaks/releases/download/v$version/gitleaks_${version}_linux_x64.tar.gz" "$sha" "$file"
      tar -xzf "$file" -C "$dest/bin" gitleaks
      ;;
    codeql)
      fetch "https://github.com/github/codeql-action/releases/download/codeql-bundle-v$version/codeql-bundle-linux64.tar.gz" "$sha" "$file"
      tar -xzf "$file" -C "$dest"
      ln -sf "$dest/codeql/codeql" "$dest/bin/codeql"
      ;;
    licensee)
      fetch "https://rubygems.org/downloads/licensee-$version.gem" "$sha" "$file.gem"
      gem install --no-document --install-dir "$dest/gems" "$file.gem" >/dev/null
      printf '#!/usr/bin/env bash\nGEM_HOME=%q GEM_PATH=%q exec %q "$@"\n' \
        "$dest/gems" "$dest/gems" "$dest/gems/bin/licensee" >"$dest/bin/licensee"
      chmod 0755 "$dest/bin/licensee"
      ;;
    *)
      echo "unknown tool: $tool" >&2
      exit 2
      ;;
  esac
done
