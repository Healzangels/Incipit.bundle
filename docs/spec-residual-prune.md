# Spec — reach the residual twin (the "nothing left to keep" case)

Status: CLOSED — WON'T FIX, on this spec's own sizing gate (2026-08-13).
Kept as the rationale record and in case the numbers change.
Follows `spec-prune-reach.md` (v1.3.212), which cleared the majority.

## What v1.3.212 leaves behind

Measured on prod 2026-08-13: 6 of 8 sampled albums shed tiles, 0 selections
changed. The two that did not logged

    incipit cover: twin prune skipped -- nothing of ours left to keep,
                   refusing to empty the namespace

That guard is correct. The prune builds its keep list from OUR framework keys
that are `in helper.metadata.posters`, and in this state none are:

* the online cover was withheld as redundant (Local Media Assets shows it), so
  `helper.thumb` was never put in the dict this pass;
* the local copy was not listed for the same reason, so `incipit-local-cover`
  is absent;
* the only alternate was the condemned twin.

Keeping nothing means deleting everything of ours, so it correctly did nothing.

**The design error being corrected: a key being NAMEABLE is not the same as it
being PRESENT.** `validate_keys` works in the framework key space
(`helper.thumb`, the literal `incipit-local-cover`, alternate urls), and a key
lives there only if THIS pass put it there. The `metadata://posters/...` keys
Plex shows are a different space we cannot pass to it.

Prod Lamb also shows a second-order version: the bundle now asks for
`41XTMI50CbL.jpg` while the container tile was minted under the older
`41XTMI50CbL._SL500_.jpg`, so `helper.thumb` does not match the tile that holds
its own picture even when both exist.

## The hard precondition, and why this spec is not "just re-offer something"

To prune we must keep at least one key. The obvious move is to re-offer one of
our good tiles so there IS one. But `validate_keys` prunes every key of ours
that is not in the list, and **the selection can be one of our metadata keys**,
whose framework key we cannot name. Pruning then evicts the operator's pick --
the picked-poster-evaporates failure.

Measured before writing this: across 60 prod albums carrying a stale twin,
**60 select an `upload://`**, not one of ours. So the safe subset is the large
majority. Prod Lamb is the exception -- its selection IS our
`metadata://...incipit_b20a3838` tile -- and it stays unfixed BY DESIGN.

    PRECONDITION: proceed only when the selected container key is NOT in our
    own metadata namespace. `read_poster_state` already returns the selected
    key (dup_state[1]); no new read is needed.

Ironic but worth stating plainly: the album that started this investigation is
the one shape this cannot safely clear.

## Mechanism

Only when ALL of these hold:

1. `stale_alternates` is non-empty (something was condemned);
2. the v1.3.212 keep list came out EMPTY (this is the residual case, not a
   second bite at the case already handled);
3. the selection is not one of our metadata keys (above);
4. we hold BYTES for one of our own tiles this pass -- `thumb_data`, else
   `cover_bytes`. Never fabricate, never re-fetch just to satisfy this.

then re-offer that one tile under its current framework key, and prune with a
keep list containing exactly it.

Priority: `helper.thumb` first (it is the record's own art), `local_key` second.
Never the artist photo, never a poisoned local cover -- the existing
`poisoned_local` / `deferred_portrait_local` flags already say which.

## What it costs, stated honestly

Re-offering puts back a tile `online_redundant` deliberately withheld. That is
a real reversal of an existing decision, and the justification is narrow: the
container ALREADY displays that picture from our namespace under a stale key,
so this re-keys an existing duplicate rather than adding a new one. Net visible
change on a Lamb-shaped album is one tile fewer, not one more.

If that trade is judged wrong, the alternative is to leave the residual alone.
It is cosmetic either way.

## Invariants to test

1. The condemned twin is REMOVED in the residual case.
2. NOTHING happens when the selection is one of our metadata keys.
3. NOTHING happens when we hold no bytes for any of our own tiles.
4. NOTHING happens when the v1.3.212 branch already built a non-empty keep list
   (no double prune, no second re-offer).
5. The re-offered tile is our own key, never the twin's.
6. An upload:// selection is never affected (validate_keys cannot touch it, but
   assert it, because that is the whole safety argument).
7. The artist photo / poisoned local is never the tile re-offered.

## Mutations that must go RED

* the selection precondition removed;
* the "keep list was empty" condition removed (fires on already-handled albums);
* the re-offered key swapped for the condemned twin's key;
* bytes-in-hand check removed (offering None);
* keep list built from the twin rather than the re-offered tile.

## Verification

* full bundle suite, sandbox/name/deploy gates;
* `.99` first: albums in the residual shape specifically, not just any album --
  confirm tiles drop by exactly the condemned count and selections are byte-for
  byte unchanged;
* prod: the 2-of-8 shape from 2026-08-13, log at DEBUG, confirm the prune line
  and that the surviving tile is ours;
* re-run the census and compare against the ~660 baseline.

## SIZED — and the answer is DON'T BUILD IT

Measured 2026-08-13 on 30 prod albums carrying a stale twin, refreshed under
v1.3.212:

    cleared by v1.3.212      26  (87%)
    RESIDUAL                  4  (13%)
    residual reachable        4  of 4 (all select an upload://)
    selections changed        0

    extrapolated over ~660 affected albums:  ~572 clear, ~88 residual

The gate this spec set before measuring was ~150 reachable albums. The real
number is **~88**, comfortably under it, so this closes as WON'T FIX by its own
criterion rather than by taste.

Two things reinforce that. v1.3.212 clears 87%, better than the 75% the first
8-album sample suggested -- the residual is a smaller tail than it looked. And
the cost has not changed: reversing a deliberate `online_redundant` decision, on
the destructive `validate_keys` path, to remove a cosmetic duplicate tile.

REOPEN IF: the residual share rises materially (re-run the sizing after a full
library rebuild, when container state is fresh), or if a mechanism appears that
does not require re-offering a withheld tile.
