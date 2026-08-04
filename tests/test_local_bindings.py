"""
`alternate_keys` must be bound BEFORE the branch that populates it.

Python binds function locals at runtime, so a name assigned only inside
`if X:` and read after the whole if/elif chain raises UnboundLocalError on
every path where X was falsy. Nothing catches that statically -- the module
imports, the plugin loads, the suite passes -- so it surfaces as a crashed
update() for one class of book and silence for the rest.

Found 2026-08-04: `alternate_keys = offer_alternate_covers(helper)` sat inside
`if helper.thumb:`, while the local-cover prune further down reads it and runs
unconditionally after the chain. A book whose art is a local cover.jpg only --
no online thumb -- reached that read with the name unbound, and the `try:` sits
AFTER the call, so it aborted compile_metadata for precisely the books the
local-cover path exists to serve. The same call already guarded the falsy case
one argument earlier (`thumb_present=bool(helper.thumb)`).

SCOPE, deliberately narrow: a general "conditionally bound local" detector was
written first and discarded. Doing it correctly needs dominance analysis --
without it, every `for` target and `try`-scoped name reads as a bug (it flagged
nine, all false). A guard that cries wolf gets deleted, so this pins the one
invariant that actually broke.
"""

import ast
import os
import unittest

AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '..', 'Contents', 'Code', '__init__.py')
NAME = 'alternate_keys'
FUNC = 'compile_metadata'


def _function(tree, name, must_mention=None):
    """The definition of `name` that actually uses `must_mention`.

    This file defines compile_metadata more than once (one per agent class), and
    taking the first match found a copy with no reference to the name at all --
    so every assertion below passed on an empty set. Selecting by content is what
    makes the guard point at the code it is guarding.
    """
    cands = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == name]
    if must_mention:
        named = [f for f in cands
                 if any(isinstance(x, ast.Name) and x.id == must_mention
                        for x in ast.walk(f))]
        if named:
            return named[0]
        return None
    return cands[0] if cands else None


class AlternateKeysBinding(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(open(AGENT).read())
        self.fn = _function(self.tree, FUNC, must_mention=NAME)
        self.assertIsNotNone(self.fn, 'no %s() referencing %r -- renamed, or the name is gone?' % (FUNC, NAME))

    def _top_level_binds(self):
        """Lines where NAME is assigned by a statement in the function BODY itself.

        A bind nested inside an If is conditional; one at this level always runs.
        """
        out = []
        for stmt in self.fn.body:
            if not isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and n.id == NAME and isinstance(n.ctx, ast.Store):
                    out.append(stmt.lineno)
        return sorted(out)

    def _reads(self):
        return sorted({n.lineno for n in ast.walk(self.fn)
                       if isinstance(n, ast.Name) and n.id == NAME
                       and isinstance(n.ctx, ast.Load)})

    def test_it_is_bound_unconditionally(self):
        binds = self._top_level_binds()
        self.assertTrue(
            binds,
            '%r is never bound at the top level of %s(), so every binding is '
            'inside a branch and any read outside that branch raises '
            'UnboundLocalError.' % (NAME, FUNC))

    def test_the_unconditional_bind_precedes_every_read(self):
        binds, reads = self._top_level_binds(), self._reads()
        self.assertTrue(reads, 'no reads of %r found -- test is now vacuous' % NAME)
        self.assertLess(
            min(binds), min(reads),
            '%r is read at line %d before its unconditional bind at %d.'
            % (NAME, min(reads), min(binds)))

    def test_a_read_survives_the_branch_being_skipped(self):
        """The reads must sit OUTSIDE the `if helper.thumb:` arm, or this whole
        guard is pointless -- that is the shape that broke.
        """
        arm = None
        for n in ast.walk(self.fn):
            if (isinstance(n, ast.If) and isinstance(n.test, ast.Attribute)
                    and n.test.attr == 'thumb'):
                arm = n
                break
        self.assertIsNotNone(arm, 'the `if helper.thumb:` arm was not found')
        arm_lines = {x.lineno for x in ast.walk(arm) if hasattr(x, 'lineno')}
        outside = [ln for ln in self._reads() if ln not in arm_lines]
        self.assertTrue(
            outside,
            'every read of %r is inside the `if helper.thumb:` arm; if that is '
            'now true the unconditional bind is dead code and this guard should '
            'be removed rather than left asserting nothing.' % NAME)


if __name__ == '__main__':
    unittest.main()
