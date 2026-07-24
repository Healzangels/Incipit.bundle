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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/../Contents/Code/_version.py"

DEFAULT_LOG="/mnt/user/appdata/plex/Logs/PMS Plugin Logs/com.plexapp.agents.incipit.log"
LOG="${1:-${INCIPIT_PLEX_LOG:-$DEFAULT_LOG}}"

# Expected version, read straight from the bundle being deployed -- so the check
# tracks the code, never a hardcoded number that would drift.
EXPECTED="$(sed -n 's/.*version *= *"\([^"]*\)".*/\1/p' "$VERSION_FILE")"
if [ -z "$EXPECTED" ]; then
	echo "FAIL: could not read version from $VERSION_FILE" >&2
	exit 2
fi

BANNER="Incipit Audiobooks Agent v$EXPECTED"

if [ ! -f "$LOG" ]; then
	echo "FAIL: plugin log not found: $LOG" >&2
	echo "  (wrong host or path view? pass the real path as an argument or via INCIPIT_PLEX_LOG.)" >&2
	exit 2
fi

# The most recent banner the agent logged, whatever version it names.
LAST_BANNER="$(grep -a 'Incipit Audiobooks Agent v' "$LOG" | tail -1 || true)"

if printf '%s' "$LAST_BANNER" | grep -qF "$BANNER"; then
	echo "OK: agent loaded cleanly -- $BANNER"
	echo "  $LAST_BANNER"
	exit 0
fi

echo "FAIL: expected banner not found -- '$BANNER'" >&2
if [ -n "$LAST_BANNER" ]; then
	echo "  last banner names a DIFFERENT version (stale load / silent sandbox death?):" >&2
	echo "  $LAST_BANNER" >&2
else
	echo "  no Incipit banner in the log at all -- plugin never loaded, or wrong log file." >&2
fi
echo "  Trigger a refresh on any Incipit book and re-run; if it stays red, the new" >&2
echo "  code hit a silent sandbox error (leading-underscore name, blocked builtin)." >&2
exit 1
