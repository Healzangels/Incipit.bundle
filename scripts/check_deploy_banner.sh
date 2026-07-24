#!/usr/bin/env bash
# Post-deploy gate for the Incipit Plex agent.
#
# The bundle runs in Plex's RestrictedPython sandbox, which fails SILENTLY: a
# leading-underscore identifier, a blocked builtin (getattr/any/sum/...), or any
# compile-time sandbox violation kills the whole plugin with NO error in the UI --
# the agent simply stops matching. py_compile / ast.parse cannot catch these. The
# ONLY proof a deploy loaded cleanly is the version banner the agent logs from
# Start():
#     Incipit Audiobooks Agent v<version>
# This turns the manual "eyeball the log for the banner" gate into a check that
# fails loudly when the banner for the freshly-deployed version is absent.
#
# Run it ON THE PLEX HOST after deploying the bundle and triggering a refresh
# (which makes Plex load the plugin). Exit 0 = the expected version loaded clean.
#
# Usage:
#   scripts/check_deploy_banner.sh [PLUGIN_LOG_PATH]
# The log path may also come from INCIPIT_PLEX_LOG; the default is the canonical
# server location.

set -euo pipefail

# Resolve through a symlink so VERSION_FILE points at the real bundle tree even
# when the script is invoked via a PATH symlink (readlink -f is GNU/unRAID; fall
# back to the raw path on a platform without it).
SCRIPT_SRC="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SRC")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/../Contents/Code/_version.py"

DEFAULT_LOG="/mnt/user/appdata/plex/Logs/PMS Plugin Logs/com.plexapp.agents.incipit.log"
LOG="${1:-${INCIPIT_PLEX_LOG:-$DEFAULT_LOG}}"

# Read the expected version from the bundle being deployed, so the check tracks
# the code rather than a hardcoded number. Guard existence FIRST: without it, a
# missing file makes `sed` fail and `set -e` aborts before the friendly message.
if [ ! -f "$VERSION_FILE" ]; then
	echo "FAIL: version file not found: $VERSION_FILE" >&2
	exit 2
fi
# Anchor to the exact `version = "..."` line (so a future schema_version/comment
# can't match) and take the first hit only.
EXPECTED="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$VERSION_FILE" | head -1)"
if [ -z "$EXPECTED" ]; then
	echo "FAIL: could not parse version from $VERSION_FILE" >&2
	exit 2
fi

if [ ! -f "$LOG" ]; then
	echo "FAIL: plugin log not found: $LOG" >&2
	echo "  (wrong host or path view? pass the real path as an argument or via INCIPIT_PLEX_LOG.)" >&2
	exit 2
fi

# The most recent banner line the agent logged, and the version it names. Compare
# the parsed version by EXACT equality -- a substring/prefix match would let
# v1.3.9 pass against a stale v1.3.95 banner, the very silent-death this catches.
# The grep requires a digit after "v" so it matches only real version banners (not
# an "Agent version ..." line) and LAST_BANNER stays meaningful for the diagnostic;
# the capture spans version chars (digits, dots, and a semver-style -suffix) so a
# tagged build like 1.3.95-rc1 still compares equal instead of a spurious FAIL.
LAST_BANNER="$(grep -aE 'Incipit Audiobooks Agent v[0-9]' "$LOG" | tail -1 || true)"
LOGGED="$(printf '%s' "$LAST_BANNER" | sed -n 's/.*Incipit Audiobooks Agent v\([0-9][0-9A-Za-z.-]*\).*/\1/p')"

if [ "$LOGGED" = "$EXPECTED" ]; then
	echo "OK: agent loaded cleanly -- Incipit Audiobooks Agent v$EXPECTED"
	echo "  $LAST_BANNER"
	exit 0
fi

echo "FAIL: expected banner not found -- 'Incipit Audiobooks Agent v$EXPECTED'" >&2
if [ -n "$LOGGED" ]; then
	echo "  last banner names a DIFFERENT version: v$LOGGED (stale load / silent sandbox death?)" >&2
	echo "  $LAST_BANNER" >&2
else
	echo "  no Incipit banner in the log at all -- plugin never loaded, or wrong log file." >&2
fi
echo "  Trigger a refresh on any Incipit book and re-run; if it stays red, the new" >&2
echo "  code hit a silent sandbox error (leading-underscore name, blocked builtin)." >&2
exit 1
