#!/usr/bin/env bash
# Release-readiness gate for dsh-subtitle-studio.
#
# Every check is a machine-verifiable pre-condition for publishing: no host
# identity in the shipped tree, no dead configuration, honest manifest, clean
# syntax, both test suites green. Run from anywhere:
#
#   test/release-gate.sh
#
# Exit 0 = publishable. Anything else prints the offending lines.
set -uo pipefail

# A pyenv / conda machine may have no bare python3 on PATH: resolve one runner and
# use it for every python call below.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3 python python3.13 python3.12 python3.11 python3.9; do
    command -v "$candidate" >/dev/null 2>&1 && { PYTHON="$candidate"; break; }
  done
fi
if [ -z "$PYTHON" ]; then echo "FAIL  no python interpreter on PATH" >&2; exit 1; fi
echo "python runner: $PYTHON"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(dirname "$HERE")"
cd "$PKG" || exit 2

fail=0
pass() { printf 'PASS  %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# Live-data guard. Every suite is supposed to run against a sandbox HOME, but a
# stray script that resolves the *default* data dir would rewrite the operator's
# real config mid-gate (one did, during a release check). Snapshot the hash of
# the live config up front and fail the gate if anything moved it.
# ---------------------------------------------------------------------------
hash_file() { (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1" 2>/dev/null) | cut -c1-64; }
LIVE_CONFIG="${HOME}/.dsh/data/subtitle-studio/config.json"
LIVE_HASH=""
if [ -f "$LIVE_CONFIG" ]; then LIVE_HASH="$(hash_file "$LIVE_CONFIG")"; fi

# Paths excluded from the scan: the gate itself contains the patterns it looks
# for, and sandboxes hold whatever the last test run left behind.
scan_files() {
  find . \
    -path ./node_modules -prune -o \
    -path ./test/sandbox -prune -o \
    -path ./test/sandbox-ui -prune -o \
    -path ./test/sandbox-real -prune -o \
    -name '__pycache__' -prune -o \
    -path ./test/release-gate.sh -prune -o \
    -path ./test/privacy-patterns.local -prune -o \
    -type f -print
}

BLOBS=$(scan_files)

# ---------------------------------------------------------------------------
# 1. Privacy sweep.
#
# Every pattern here is a *shape*, never a name. A gate that hard-codes the
# author's own hostname, keychain entry, private project directory or favourite
# show would publish, in the released tree, exactly the thing it exists to
# catch. Machine-specific words belong in test/privacy-patterns.local: one
# extended regex per line, blank lines and `#` comments allowed, git-ignored and
# never packed — so each maintainer keeps their own fingerprints out of sight
# while still being stopped by them.
#
# format of each entry: "<human label>|<grep -E regex>"
# ---------------------------------------------------------------------------
LOCAL_PATTERNS="$HERE/privacy-patterns.local"
PATTERNS=(
  'absolute home path|/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+'
  'private network address|192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|\.[a-z0-9-]+\.local\b|\.lan\b'
  'personal script directory|\.dsh/scripts'
  'secret-shaped literal|(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{20,}|[A-Za-z0-9_-]{40}\.[A-Za-z0-9_-]{20,})'
  'personal mail address|[A-Za-z0-9._%+-]+@(icloud|gmail|outlook|hotmail|qq|163|126)\.com'
  'package-manager-specific default binary|ffmpeg[^\n]*.(\./)?/opt/homebrew'
  'long opaque literal|[0-9a-f]{48,}'
)

scan_pattern() {
  # $1 label, $2 regex
  if hits=$(grep -rnE "$2" $BLOBS 2>/dev/null); then
    bad "$1 present in the shipped tree"
    printf '%s\n' "$hits" | head -8
  else
    pass "no $1"
  fi
}

for entry in "${PATTERNS[@]}"; do
  scan_pattern "${entry%%|*}" "${entry#*|}"
done

# Keychain reads are fine; naming somebody else's item is not. Anything that
# touches `security find-*` must ask for a service under this plugin's prefix.
# A docstring that merely mentions `security find-generic-password` is not a
# leak; a service name typed in as a literal is. Match only argv lists that
# actually call the keychain, so `curl -s` does not trip it.
stray_keychain=$(grep -rn 'find-generic-password' $BLOBS 2>/dev/null \
  | grep -E -- "-s[\"'], [\"']" \
  | grep -v 'subtitle-studio' || true)
if [ -n "$stray_keychain" ]; then
  bad "hard-coded keychain service outside the plugin prefix"
  printf '%s\n' "$stray_keychain" | head -5
else
  pass "no hard-coded keychain service name"
fi

# Maintainer-specific patterns, kept out of the release.
if [ -f "$LOCAL_PATTERNS" ]; then
  n_local=0
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    n_local=$((n_local + 1))
    scan_pattern "author-local fingerprint #$n_local" "$line"
  done < "$LOCAL_PATTERNS"
  pass "$n_local author-local patterns applied (not shipped)"
fi

# 10. Manifest honesty.
node -e '
const p = JSON.parse(require("fs").readFileSync("package.json", "utf8"));
const bad = [];
if (p.private === true) bad.push("private:true");
if (!p.license) bad.push("license missing");
if (!p.repository) bad.push("repository missing");
if (!p.engines || !p.engines.node) bad.push("engines.node missing");
if (!p.os) bad.push("os missing");
for (const f of p.files || []) {
  const fse = require("fs");
  if (!fse.existsSync(f) && !fse.existsSync(f.replace(/\/$/, ""))) bad.push("files entry missing on disk: " + f);
}
if (bad.length) { console.error(bad.join("\n")); process.exit(1); }
' >/dev/null 2>&1 \
  && pass "package.json is publish-ready" \
  || { bad "package.json not publish-ready"; node -e '
const p = JSON.parse(require("fs").readFileSync("package.json", "utf8"));
if (p.private === true) console.log("  private:true");
if (!p.license) console.log("  license missing");
if (!p.repository) console.log("  repository missing");
if (!p.engines || !p.engines.node) console.log("  engines.node missing");
if (!p.os) console.log("  os missing");
for (const f of p.files || []) if (!require("fs").existsSync(f)) console.log("  files entry missing on disk:", f);
'; }

# 11. Version agreement between the manifest and the engine.
PV=$(node -e 'process.stdout.write(JSON.parse(require("fs").readFileSync("package.json","utf8")).version)')
EV=$(grep -m1 -E '^ENGINE_VERSION' engine/subtitle-run | sed -E 's/.*"([^"]+)".*/\1/')
if [ "$PV" = "$EV" ]; then pass "version matches engine ($PV)"; else bad "version mismatch: package $PV vs engine $EV"; fi

# 12. Syntax.
node --check index.js >/dev/null 2>&1 && pass "index.js parses" || bad "index.js does not parse"
node --check client.js >/dev/null 2>&1 && pass "client.js parses" || bad "client.js does not parse"
"$PYTHON" -m compileall -q engine >/dev/null 2>&1 && pass "engine compiles" || bad "engine does not compile"

# 13. Engine kind `dsh` documented everywhere it is offered.
for doc in README.md docs/DETAIL.md docs/DETAIL.zh.md engine/README.md; do
  if grep -q 'dsh' "$doc" 2>/dev/null; then pass "$doc mentions the dsh engine"
  else bad "$doc does not mention the dsh engine"; fi
done

# 14. Test suites.
if node test/e2e.mjs >/tmp/gate-e2e.log 2>&1; then
  pass "e2e suite ($(grep -c '^PASS' /tmp/gate-e2e.log) checks)"
else
  bad "e2e suite failed"; tail -20 /tmp/gate-e2e.log
fi
if "$PYTHON" test/multi_dir_test.py >/tmp/gate-md.log 2>&1; then
  pass "multi-directory suite ($(grep -c '^PASS' /tmp/gate-md.log) checks)"
else
  bad "multi-directory suite failed"; tail -20 /tmp/gate-md.log
fi
# 校验消息只允许一种语言：面板是双语的，服务端消息混着写最容易出现半中半英。
if grep -qE "(error|warn)\('[^']+', '[^']*[一-鿿]" index.js; then
  bad "validation messages mix Chinese into the English set"
else
  pass "validation messages are single-language"
fi

if "$PYTHON" test/quality_test.py >/tmp/gate-q.log 2>&1; then
  pass "quality suite ($(grep -c '^PASS' /tmp/gate-q.log) checks)"
else
  bad "quality suite failed"; tail -20 /tmp/gate-q.log
fi
if "$PYTHON" test/v14_test.py >/tmp/gate-v14.log 2>&1; then
  pass "v1.4 feature suite ($(grep -c '^PASS' /tmp/gate-v14.log) checks)"
else
  bad "v1.4 feature suite failed"; tail -20 /tmp/gate-v14.log
fi
if "$PYTHON" test/v15_test.py >/tmp/gate-v15.log 2>&1; then
  pass "v1.5 feature suite ($(grep -c '^PASS' /tmp/gate-v15.log) checks)"
else
  bad "v1.5 feature suite failed"; tail -20 /tmp/gate-v15.log
fi
if "$PYTHON" test/v16_test.py >/tmp/gate-v16.log 2>&1; then
  pass "v1.6 feature suite ($(grep -c '^PASS' /tmp/gate-v16.log) checks)"
else
  bad "v1.6 feature suite failed"; tail -20 /tmp/gate-v16.log
fi
if node test/ui-smoke.mjs >/tmp/gate-ui.log 2>&1; then
  pass "ui smoke suite ($(grep -c '^PASS' /tmp/gate-ui.log) checks)"
else
  bad "ui smoke suite failed"; tail -20 /tmp/gate-ui.log
fi
if node test/real-smoke.mjs >/tmp/gate-real.log 2>&1; then
  pass "real-engine smoke ($(grep -c '^PASS' /tmp/gate-real.log) checks)"
else
  bad "real-engine smoke failed"; tail -20 /tmp/gate-real.log
fi

# 15. npm pack dry run.
if npm pack --dry-run >/tmp/gate-pack.log 2>&1; then
  pass "npm pack --dry-run"
  node -e '
const log = require("fs").readFileSync("/tmp/gate-pack.log","utf8");
const junk = log.split(/\n/).filter((l) => /\.pyc|__pycache__|sandbox|node_modules|\.DS_Store/.test(l));
if (junk.length) { console.error(junk.slice(0,10).join("\n")); process.exit(1); }
' && pass "tarball carries no build or test droppings" \
    || bad "tarball carries build or test droppings"
else
  bad "npm pack --dry-run failed"; tail -20 /tmp/gate-pack.log
fi

# 16. Leave no test residue behind: the suites run against real sandboxes that
# carry absolute paths and throwaway bridge tokens. Dropping them here means a
# whole-directory hand-off can never ship one by accident.
rm -rf test/sandbox test/sandbox-ui test/sandbox-real engine/vendor/__pycache__ engine/lib/__pycache__
if [ -e test/sandbox ] || [ -e test/sandbox-ui ] || [ -e test/sandbox-real ]; then
  bad "test sandboxes survived the cleanup"
else
  pass "test sandboxes removed"
fi

if [ -n "$LIVE_HASH" ]; then
  if [ "$(hash_file "$LIVE_CONFIG")" = "$LIVE_HASH" ]; then
    pass "live config untouched by the gate"
  else
    bad "a suite rewrote ${LIVE_CONFIG} (restored from state/engine-config.json, then re-run)"
  fi
fi

printf '\n=== release gate: %s failures ===\n' "$fail"
exit $((fail > 0 ? 1 : 0))
