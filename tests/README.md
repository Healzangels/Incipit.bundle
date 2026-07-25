# Tests

```bash
python3 -m unittest discover -s tests
```

No dependencies, no Plex, no network — plain stdlib `unittest` on Python 3.
Runs in well under a second, so there is no excuse for skipping it before a
commit.

## What this covers, and what it can't

`Contents/Code` is a Plex plugin: Python 2.7 inside a RestrictedPython sandbox,
with a dozen framework globals that exist nowhere else. `plexenv.py` bridges
that — inert fakes for the globals, `unicode`/`reduce`, shims for the py2
`StringIO` and `urllib` — so the modules import in ordinary Python 3 and their
pure functions can be called directly.

In scope: name folding, the folder/series anchor, volume and title
normalisation, sort-title construction, poster byte identity, the cache and
memo decisions. **That is where every regression in this repo has actually
lived** — silent comparisons that returned the wrong answer, not crashes.

Out of scope: anything that calls `HTTP.Request`, `Core.storage` or
`Proxy.Media`. Those fakes raise on use, deliberately, so a test that wanders
into a live Plex call fails loudly instead of quietly testing a stub. Those
paths are still verified on the test box against the agent log.

## Adding a test

Prefer a case that failed in production, and say so in the test. Nearly every
test here names the book that exposed the bug — Mattimeo, Defiance of the Fall
book 10, "James S.A. Corey" vs "James S. A. Corey" — because the shape of a
real failure is more instructive than an invented one.

## When a test fails

Find out why it fails. Do not adjust the assertion so it passes.

A red test is the only automated signal this repo has: the sandbox fails
silently, `py_compile` passes on code that will not load, and the plugin's
errors surface as missing metadata rather than exceptions. A test weakened to
go green removes that signal and ships the bug — and the review that prompted
this suite found exactly that: a regression test that still passed with the fix
it guarded deleted in full, because the fixture had been built to match the
outcome rather than the scenario.

If a test is genuinely wrong, fix it and explain in the commit why the old
assertion was incorrect. That is a different act from making red go away.
