# Spec — make the alternate prune REACH a stale twin

Status: IMPLEMENTED in v1.3.212. Kept as the rationale record --
particularly why the one-line fix is wrong, which is the part a future
reader is most likely to re-derive the hard way.

Deviation from the design as written: `our_container_keys()` was NOT
needed. Our framework-side keys turned out to be a CLOSED set we can
name (`twin_prune_candidates`) -- the online cover, its secondary, the
local-mirror key and the alternates -- so membership tests enumerate
them without iterating the container at all. That removes the
sibling-library hazard the spec worried about rather than guarding it.

## The defect

v1.3.211 correctly declines a twin alternate. Proven on prod 2026-08-13:

    incipit cover: alternate is the same picture at a different size
                   -- not offering a twin tile (.../71BWD27-WyL._SL1024_.jpg)

But declining only means the url is left out of `alternate_keys`, and leaving a
key out of the keep list removes a tile **only if `validate_keys` actually
runs**. It is gated on something unrelated to alternates:

    if (not local_set and helper.thumb in helper.metadata.posters):
        ...
        helper.metadata.posters.validate_keys(keep)

On prod Lamb BOTH conditions are false — Local Media Assets already displays the
cover (so our copy is not listed, `local_set` true) and the online cover was
withheld as redundant (so `helper.thumb` is not in the container). The prune
never runs and the pre-existing twin survives every refresh.

`.99` differed for one reason only: no LMA tile, so the branch ran there.

**So: new twins are already prevented. This spec is only about clearing the ones
older versions left behind.**

## Why the obvious fix is WRONG

Widening the gate to "also run when a twin was dropped" is a one-line change and
it is dangerous. In precisely the state we care about, `cover_keep_list` returns
an EMPTY list:

    keep = [thumb_key] if thumb_present else []      # thumb withheld -> []
    if local_present and not (mirror_skipped ...)    # mirror_skipped -> not appended
    for key in alternate_keys                        # twin omitted -> nothing

`validate_keys([])` empties the whole incipit namespace. The existing code
already warns about this at the one call site that does it: on a thumb-less
(Hardcover/OpenLibrary) match our namespace can hold the ONLY copy of the
operator's curated cover.jpg, and emptying it loses that art.

Do not widen the gate.

## Design

### The rule the design rests on

Three classes of entry, three different rights:

| our entry            | may we remove it?                                        |
|----------------------|----------------------------------------------------------|
| an ALTERNATE we offered | YES on a perceptual verdict — it is third-party spare art we added, never operator art |
| our LOCAL-MIRROR copy   | ONLY on byte identity with a copy still displayed (it may be the only copy of curated art) |
| the SELECTION           | NEVER — the picked-poster-evaporates failure |

The perceptual/byte distinction already exists in this file; this spec applies
it per class rather than per call site.

### Mechanism

1. `offer_alternate_covers` gains a second return value (or an out-list):
   `stale_alternates` — urls that are IN the container and were judged a twin
   this pass. Skipping already records the decision; this just keeps it.

2. A new, narrowly scoped prune runs when `stale_alternates` is non-empty AND
   the existing membership branch did NOT run. It is a separate branch, not a
   widened condition, so the existing behaviour is untouched.

3. Its keep list is built by SUBTRACTION, never from `cover_keep_list`:

       keep = [k for k in our_container_keys() if k not in stale_alternates]

   so every entry of ours that this pass did not positively condemn survives,
   including the redundant-online and local-mirror entries the existing branch
   would have dropped.

4. FAIL CLOSED. If `our_container_keys()` cannot be enumerated confidently, do
   nothing. A missed duplicate tile is cosmetic; a wrong `validate_keys` deletes
   art.

### The enumeration hazard (the reason for step 4)

The container arrives DESERIALIZED and, with two libraries side by side, can
arrive pre-populated with a SIBLING library's entries — bundles are shared per
guid ([[plex-container-serialize-traps]]). So `our_container_keys()` must:

* include only keys in our own `com.plexapp.agents.incipit` namespace
  (`validate_keys` cannot touch upload:// or another agent's keys anyway);
* never assume the set is complete — if the read fails or returns nothing while
  we know at least one of our keys exists, abort the prune;
* never include the selection, even if it is one of ours.

## Invariants to test

1. A stale twin already in the container is REMOVED.
2. A genuine alternate already in the container SURVIVES.
3. Our redundant-online entry SURVIVES this prune (the existing branch owns that
   decision; this one must not borrow it).
4. Our local-mirror entry SURVIVES this prune — the curated-art rail.
5. The SELECTION is never in the removal set, even when it is a twin.
6. Nothing runs when `stale_alternates` is empty (no behaviour change for the
   overwhelming majority of albums).
7. Enumeration failure => no `validate_keys` call at all.
8. The existing membership branch still runs exactly when it did before, with
   the same keep list (no regression on the `.99`-shaped case).

## Mutations that must go RED

* removal set applied without subtracting from our keys (i.e. `validate_keys(stale)`)
* the fail-closed guard removed
* the selection allowed into the removal set
* the new branch running when `stale_alternates` is empty
* the new branch also firing when the existing branch already ran (double prune)

## Verification before prod

* full bundle suite green, sandbox/name/deploy gates included;
* `.99` broad pass, 30 albums, measured the way the 2026-08-12 pass was:
  tiles down, DISTINCT PICTURES compared PERCEPTUALLY (byte-hash counting
  produced a 21-album false alarm), selections unchanged, and a per-album check
  that no image present before is absent after unless a twin of it is still
  shown;
* prod: one album, log at DEBUG, confirm the prune line and the surviving set,
  then revert logging to WARN.

## Scope explicitly EXCLUDED

* `upload://` accumulation from Fix Match — the agent cannot delete uploads
  (PUT/DELETE are downgraded). Unrelated to this spec.
* Local Media Assets' own duplicate tiles — not our namespace, not ours to prune.
* Any change to what COUNTS as a twin. v1.3.211's judgement is verified working
  on prod and is out of scope here.

## Risk

Medium-low. The mechanism is destructive by nature (`validate_keys` is the only
removal primitive available), which is why the keep list is built by subtraction
and the failure mode is "do nothing". The blast radius on a mistake is poster
tiles in the picker, not files on disk — but on a thumb-less match our namespace
can hold the only copy of curated art, which is what rule 4 and the fail-closed
guard exist for.
