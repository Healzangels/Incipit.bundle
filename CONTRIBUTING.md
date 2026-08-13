<!-- omit in toc -->
# Contributing to Incipit.bundle

Thanks for taking the time. This guide is short and specific, because the things
most likely to waste your afternoon here are not general good practice — they are
consequences of running inside Plex's plugin sandbox.

<!-- omit in toc -->
## Table of Contents

- [The sandbox will break your code silently](#the-sandbox-will-break-your-code-silently)
- [Running the tests](#running-the-tests)
- [Rules the suite enforces on you](#rules-the-suite-enforces-on-you)
- [Testing against a real server](#testing-against-a-real-server)
- [What a good change looks like here](#what-a-good-change-looks-like-here)
- [Reporting bugs](#reporting-bugs)
- [Licence](#licence)

## The sandbox will break your code silently

Plex runs this bundle under **Python 2.7 inside RestrictedPython**. When you use
something it disallows, the plugin does not raise — it fails to compile and simply
never loads, while Plex carries on and the agent quietly returns nothing.

The two that catch people:

- **No leading-underscore names.** Not variables, not functions, not attributes.
  `_helper`, `self._cache`, `from _version import version` at the wrong spot — all
  fatal. `test_deploy_gate.py` scans the whole bundle for these.
- **No `getattr` / `hasattr`.** Duck-type with `try/except AttributeError` instead.
  Both were caught by the guard suite after reaching production.

Also worth knowing: augmented assignment is restricted (`test_sandbox_operators.py`
allows only `+=`), and `basestring` exists in Plex's py2.7 but NameErrors under the
py3 test harness — so do not reach for it.

## Running the tests

Standard library `unittest`, no dependencies:

```
python3 -m unittest discover -s tests
```

The harness runs on **Python 3** while Plex runs the same code on **2.7**. That gap is
real: byte/str handling differs, and a function that returns bytes in production can
return `None` under the harness. Where that matters the tests say so.

## Rules the suite enforces on you

These are meta-tests. They fail loudly, but knowing about them saves a confusing red:

- **Version bumps go in two files.** `Contents/Code/_version.py` and
  `Contents/Info.plist` must agree, or the deploy gate fails.
- **Every prune announces itself at WARN.** Anything that removes a poster must log
  at `warn`, because the shipped default level is WARN and a prune nobody can see is
  how art disappears with no way to tell what took it. `log.error` in an except
  handler is a failure report, not an announcement, and does not count.
- **`if __name__ == '__main__':` must be the LAST top-level statement** in a test
  file. Append a class after it and `python3 tests/that_file.py` silently skips it.

## Testing against a real server

**Plex never hot-reloads a bundle.** After changing files, a resident plugin keeps
serving the old code from memory indefinitely — you can watch it happily answer
refreshes with the version you just replaced. Reload just the plugin:

```
curl -s "http://<plex-host>:32400/:/plugins/com.plexapp.agents.incipit/restart?X-Plex-Token=<token>"
```

Two more traps when verifying live:

- If Plex runs in a container, the files must be readable by its user (usually
  `nobody:users`). Files left owned by root simply do not load.
- Cover decisions log at INFO, but the shipped level is WARN, so raise
  `logging_level` while diagnosing and put it back afterwards — a full library sweep
  at DEBUG writes a great deal.

## What a good change looks like here

This codebase is maintained by measurement rather than intuition, and the comments
and commit messages carry that record. Concretely:

- **Say what you measured.** "Fixes duplicate covers" is weaker than "measured on 30
  albums: 75 tiles → 42, 0 selections changed, 0 pictures lost".
- **Mutate your fix.** Break it deliberately and confirm a test goes red. A green
  suite proves nothing about a test that never constrained the code. Several bugs
  here were found exactly this way, including one that shipped.
- **Prefer a rule to a special case.** Naming one franchise umbrella removed twelve
  hand-written per-book pins.
- **Poster changes carry an extra duty.** Users hand-curate covers. A change that can
  remove a tile must never be able to remove the one they picked, and must fail
  closed when it cannot tell.

Design rationale for larger pieces lives in [`docs/`](docs/), including specs that
were deliberately **not** built and why.

## Reporting bugs

Open an issue at
[Healzangels/Incipit.bundle/issues](https://github.com/Healzangels/Incipit.bundle/issues).

Useful to include: what the agent did versus what you expected, the album/author it
happened on, your `logging_level` and the relevant lines from
`PMS Plugin Logs/com.plexapp.agents.incipit.log`, and your bundle version from
`Contents/Code/_version.py`.

Please do not file security issues publicly — raise them privately with the
maintainer through GitHub instead.

## Licence

By contributing you agree your work is provided under this project's licence
(GPL-3.0), inherited from [Audnexus.bundle](https://github.com/djdembeck/Audnexus.bundle).
