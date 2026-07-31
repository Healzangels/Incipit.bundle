#!/usr/bin/env bash
# Back up the operator's hand-curated cover.jpg files.
#
# WHY THIS EXISTS
#   Curated album art is the only IRREPLACEABLE asset in this system. Everything
#   else — the Mongo record store, the Redis caches, Plex's own metadata — is
#   re-fetchable. Cover art is human labour, and 92 of them were destroyed on
#   2026-07-26 by a guard that failed open. The agent's own source records the
#   aftermath: "There were no backups of that share."
#
#   This script is the missing backup. It copies ONLY cover.jpg files, preserving
#   the album directory layout, and it NEVER deletes from the destination.
#
# WHY --backup-dir MATTERS
#   A plain mirror is not enough. The failure mode this system actually has is a
#   bad pass OVERWRITING good covers with wrong images — and a mirror run after
#   that faithfully copies the damage over the good backup. --backup-dir keeps
#   the previous version of any file that changed, filed under the run date, so
#   an overwrite-during-scan incident stays recoverable.
#
# USAGE (run on the media host)
#   scripts/backup_covers.sh <library-root> <backup-root>
#   e.g. scripts/backup_covers.sh /data/media/audiobooks-updated /mnt/backup/incipit-covers
#
#   Add --dry-run as a third argument to see what it would do first. Do that once
#   before the first real run: confirm the library root is the one Plex actually
#   serves. ("-updated" vs "-upgraded" is a known ambiguity in this setup.)
#
# SCHEDULING
#   Weekly is plenty; covers change when the operator curates, not continuously.
#     0 4 * * 0  /path/to/scripts/backup_covers.sh <library-root> <backup-root>

set -euo pipefail

SRC="${1:-}"
DEST="${2:-}"
DRY="${3:-}"

if [ -z "$SRC" ] || [ -z "$DEST" ]; then
	echo "usage: $0 <library-root> <backup-root> [--dry-run]" >&2
	exit 2
fi
if [ ! -d "$SRC" ]; then
	echo "FAIL: library root is not a directory: $SRC" >&2
	echo "  (wrong host, or the share is not mounted here?)" >&2
	exit 2
fi

STAMP="$(date +%F)"
HIST="$DEST-history/$STAMP"
mkdir -p "$DEST" "$HIST"

RSYNC_OPTS=(-a --prune-empty-dirs --include='*/' --include='cover.jpg' --exclude='*'
	--backup --backup-dir="$HIST" --itemize-changes --stats)
if [ "$DRY" = "--dry-run" ]; then
	RSYNC_OPTS+=(--dry-run)
	echo "DRY RUN — nothing will be written."
fi

# Count first so the summary can state coverage rather than implying it.
FOUND="$(find "$SRC" -name cover.jpg -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "library root : $SRC"
echo "backup root  : $DEST"
echo "changed-file history: $HIST"
echo "cover.jpg files found: $FOUND"
if [ "$FOUND" = "0" ]; then
	echo "FAIL: no cover.jpg found under $SRC — refusing to run." >&2
	echo "  A backup that silently copies nothing is worse than no backup: it" >&2
	echo "  reports success and you stop worrying. Check the library root." >&2
	exit 2
fi

rsync "${RSYNC_OPTS[@]}" "$SRC/" "$DEST/"

if [ "$DRY" != "--dry-run" ]; then
	KEPT="$(find "$DEST" -name cover.jpg -type f | wc -l | tr -d ' ')"
	REPLACED="$(find "$HIST" -type f 2>/dev/null | wc -l | tr -d ' ')"
	echo
	echo "backed up: $KEPT cover.jpg files"
	echo "superseded versions kept in $HIST: $REPLACED"
	# A large replaced count on a routine run means covers CHANGED since the last
	# backup. During a scan the correct number is zero — the same red flag the
	# runbook records for "poster-backup: saved".
	if [ "$REPLACED" -gt 0 ]; then
		echo "NOTE: $REPLACED cover(s) differed from the last backup. If you did not"
		echo "      curate since the previous run, inspect $HIST before trusting this."
	fi
fi
