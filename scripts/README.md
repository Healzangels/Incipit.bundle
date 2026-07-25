# Operator scripts

Read-only tools that live beside the bundle for convenience. **Plex only loads
`Contents/Code`**, so nothing here affects the agent at runtime.

None of them write, delete, or change metadata. Each prints a worklist and
stops — deciding which copy of a book to keep, or which cover is the real one,
is a judgement call, and a wrong bulk "fix" has destroyed curated art here
before.

## Which box?

Everything runs on **CMacServer-2** (the Plex host). That is the only machine
with both `Preferences.xml` — where the token is read from, so it never has to
be typed onto a command line — and a mount of the media share. The media files
themselves live on **CMacServer**; see the homelab runbook for the split.

## The scripts

| Script | Answers | Runtime |
|---|---|---|
| `library_audit.py` | Duplicate books, mixed `.mp3`/`.m4b`, junk series folders | seconds (2 API calls) |
| `poison_sweep.py` | Which books wear their AUTHOR's photo as a cover | minutes (per-album fetches) |
| `check_deploy_banner.sh` | Did the version I just deployed actually load? | seconds |
| `plexlib.py` | *(shared plumbing — not run directly)* | — |

### library_audit.py

```bash
python3 scripts/library_audit.py
```

Duplicate verdicts are ranked by how much they prove:

1. **same guid** — one edition held twice, definitive
2. **identical byte size** — one file in two places. A guid only records what
   Plex *matched*, and the same file under a different folder name matches
   differently (Oathbringer: two guids, one 807,500,000-byte file)
3. **differing guid and size** — genuinely different books sharing a title.
   This is the common case; expect several and act on none of them

### poison_sweep.py

```bash
python3 scripts/poison_sweep.py --path-to /mnt/remotes/10.0.1.98_data
```

`--path-to` is what enables the check that matters. Without it only the
*selected* poster is examined, and the poison lives in `cover.jpg` — a book with
a clean selection and a poisoned `cover.jpg` re-poisons itself on its next
refresh. Read `cover.jpg CHECKED: N of M` in the summary before trusting the
count; a large `folder NOT found` means the path rewrite is wrong for your box
and those albums were skipped, not cleared.

### check_deploy_banner.sh

Confirms the agent log shows the version you expect after a deploy. The Plex
sandbox fails silently on some errors, so a clean `git pull` is not proof the
new code loaded.
