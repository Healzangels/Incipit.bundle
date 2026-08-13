# docs/

Design rationale for changes big enough that the reasoning is worth more than the
diff. **A spec here is not a plan of record — read its Status line first.** One of
these describes work that was deliberately not done, and that is the point: knowing
why something was rejected is as useful as knowing why something was built, and
cheaper than re-deriving it.

| spec | status |
|---|---|
| [spec-prune-reach.md](spec-prune-reach.md) | **Implemented** in v1.3.212 |
| [spec-residual-prune.md](spec-residual-prune.md) | **Closed — won't fix**, on the sizing gate the spec set for itself |

Both concern the same corner: duplicate poster tiles, and the fact that
`validate_keys` is the only primitive available for removing one. That makes every
change there destructive-adjacent, which is why they got written down at all.

## Why these two are worth reading before touching cover code

**`spec-prune-reach.md`** records why the obvious one-line fix is wrong. Widening the
existing gate looks correct and would empty the agent's whole poster namespace on
exactly the albums it was meant to help — including, on a thumb-less match, the only
copy of a user's curated `cover.jpg`. The keep list is built by subtraction for that
reason.

It also records a design error worth inheriting: **a key being nameable is not the
same as it being present.** `validate_keys` works in the framework key space, and a
key only lives there if the current pass put it there. Reasoning from "we can name
our keys" produced a fix that worked on some albums and silently did nothing on
others.

**`spec-residual-prune.md`** was sized before being built, and the number came in
under the gate it had set — ~88 reachable albums against a ~150 threshold — so it
closed. It carries a `REOPEN IF`, because the honest reason to keep it is that the
numbers might change, not that the idea was bad.

## Writing another one

Worth a spec when the change is hard to reverse, when the obvious approach is wrong
in a way the diff will not show, or when you might decide not to build it. State the
gate before measuring, so the decision is made by the criterion rather than by
whichever answer you were hoping for.
