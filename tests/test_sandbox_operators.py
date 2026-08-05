"""
Only `+=` is safe in sandboxed code. Every other augmented assignment is a
production crash that no local test would catch.

WHAT HAPPENED
    `sleep_time *= 2` sat in make_request's retry ladder. RestrictedPython
    rejects that operator -- "Operator '*=' is not supported" -- and the line
    is INSIDE the `except` handler, so the error it raised was not caught by
    that same handler. It escaped make_request, turning every retryable failure
    into an instant hard failure, and surfaced to the operator as

        incipit book fetch failed for <url>; keeping existing metadata:
        Operator '*=' is not supported

    which blames the fetch rather than the backoff.

WHY IT SURVIVED SO LONG
    It was unreachable. Before v1.3.186 HTTP.Request was lazy, so HTTP errors
    surfaced at the CALLER's str() and the ladder never ran -- "Failed http
    request attempt" appears ZERO times across four production log files.
    Forcing .content inside the try made the ladder live for the first time and
    woke the landmine. Measured on the 2026-08-05 rebuild: the ladder fired 56
    times and 13 of those -- every single invocation that reached the backoff
    line -- died there.

WHY A UNIT TEST CANNOT FIND THIS
    The test harness runs plain CPython, where `*=` is perfectly legal. The
    sandbox is the only place it fails, and the sandbox is only on the server.
    So this guard is a STATIC one: it reads the source rather than running it.

WHY `+=` IS THE EXCEPTION AND NOT A GUESS
    25 uses across search_tools.py and __init__.py, all in paths that run on
    every single search and update. If `+=` were rejected the plugin could not
    build a query string, so its safety is established by the plugin working at
    all. No other operator has that evidence, so no other operator is allowed.
"""

import ast
import os
import unittest

CODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'Contents', 'Code')

# The one operator with positive production evidence.
ALLOWED = (ast.Add,)

SYMBOL = {
    ast.Add: '+=', ast.Sub: '-=', ast.Mult: '*=', ast.Div: '/=',
    ast.FloorDiv: '//=', ast.Mod: '%=', ast.Pow: '**=', ast.LShift: '<<=',
    ast.RShift: '>>=', ast.BitOr: '|=', ast.BitAnd: '&=', ast.BitXor: '^=',
}


def sandboxed_sources():
    out = []
    for name in sorted(os.listdir(CODE_DIR)):
        if name.endswith('.py'):
            out.append(os.path.join(CODE_DIR, name))
    return out


class NoUnsupportedAugmentedAssignment(unittest.TestCase):
    def test_only_plus_equals_is_used(self):
        offenders = []
        for path in sandboxed_sources():
            handle = open(path)
            try:
                tree = ast.parse(handle.read())
            finally:
                handle.close()
            for node in ast.walk(tree):
                if not isinstance(node, ast.AugAssign):
                    continue
                if isinstance(node.op, ALLOWED):
                    continue
                offenders.append('%s:%d  %s' % (
                    os.path.basename(path), node.lineno,
                    SYMBOL.get(type(node.op), type(node.op).__name__)))
        self.assertEqual(
            offenders, [],
            'RestrictedPython rejects these operators at RUNTIME, on the '
            'server, with no local symptom -- rewrite as `x = x <op> y`:\n  '
            + '\n  '.join(offenders))

    def test_the_guard_can_actually_see_an_offender(self):
        """A static guard that matches nothing is worse than none at all.

        Proves the walk reaches AugAssign nodes and that the operator filter
        discriminates, rather than passing because it found nothing to look at.
        """
        tree = ast.parse('a = 1\na *= 2\nb = 0\nb += 1\n')
        found = [type(n.op) for n in ast.walk(tree) if isinstance(n, ast.AugAssign)]
        self.assertEqual(found, [ast.Mult, ast.Add])
        rejected = [op for op in found if not issubclass(op, ALLOWED)]
        self.assertEqual(rejected, [ast.Mult])

    def test_the_real_source_contains_augmented_assignments_at_all(self):
        """If this ever hits zero, the main test has gone vacuous."""
        total = 0
        for path in sandboxed_sources():
            handle = open(path)
            try:
                tree = ast.parse(handle.read())
            finally:
                handle.close()
            total += len([n for n in ast.walk(tree) if isinstance(n, ast.AugAssign)])
        self.assertGreater(
            total, 0,
            'no augmented assignments found anywhere -- the guard is asserting '
            'nothing and should be removed rather than left as decoration')


if __name__ == '__main__':
    unittest.main()
