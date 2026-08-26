"""
Import the agent's modules OUTSIDE Plex, so its logic can be unit-tested.

WHY THIS IS NEEDED
    Contents/Code is a Plex plugin: it runs under Python 2.7 inside a
    RestrictedPython sandbox where the framework injects a dozen globals
    (String, HTTP, Prefs, Core, Proxy, Locale, Log, Datetime, Agent, Util,
    MetadataSearchResult) that exist nowhere else. There is no Plex to import
    from, so for a long time the only way to check a change was to deploy it and
    read the agent log -- which is why several bugs shipped and were found live.

    None of the modules touch a framework global at IMPORT time, though (checked:
    zero module-scope references), so a small compatibility layer is enough to
    load them in ordinary Python 3 and call the pure functions directly.

WHAT IT BRIDGES
    * framework globals -> inert fakes, injected into builtins
    * `unicode` / `reduce`   -> str / functools.reduce
    * `import StringIO`      -> io shim (py2 module, gone in py3)
    * `import urllib`        -> py2-style urllib.quote/unquote/urlencode
    * `from logging import Logging` -> the BUNDLE's logging.py, not the stdlib's
      one, which shares the name. sys.modules is swapped for the duration of the
      load and restored afterwards, so nothing else in the process is affected.

WHAT IT DOES NOT DO
    It does not emulate Plex. Anything that actually calls HTTP.Request,
    Core.storage or Proxy.Media is out of scope here -- those paths are verified
    on the test box against the agent log. What IS in scope is every pure
    function: name folding, the folder/series derivation, title and volume
    normalisation, the poster byte comparisons. That is where this session's
    regressions lived.
"""

import builtins
import functools
import importlib.util
import io
import os
import sys
import types
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.join(os.path.dirname(HERE), 'Contents', 'Code')

# Order matters: each module is imported by the ones after it, under exactly the
# name they use in their import statements.
MODULE_ORDER = ('_version', 'logging', 'region_tools', 'search_tools', 'update_tools')


def _default_prefs():
    """
    The shipped defaults, read from Contents/DefaultPrefs.json.

    Loaded from the real file rather than hand-copied so the two cannot drift.
    A hand-maintained copy immediately did: a pref added to the JSON was missing
    here, and three unrelated tests started raising KeyError -- which reads as a
    product bug and is not one. It also makes the harness faithful in the other
    direction: code that reads a pref never declared in DefaultPrefs.json now
    raises here, exactly as it would in Plex.
    """
    import json
    path = os.path.join(os.path.dirname(CODE), 'DefaultPrefs.json')
    out = {}
    with open(path) as handle:
        for entry in json.load(handle):
            value = entry.get('default', '')
            if entry.get('type') == 'bool':
                value = str(value).lower() == 'true'
            out[entry['id']] = value
    return out


class FakePrefs(dict):
    """Prefs[...] backed by the shipped DefaultPrefs.json."""

    DEFAULTS = _default_prefs()

    def __getitem__(self, key):
        if key in self:
            return dict.__getitem__(self, key)
        # KeyError on an undeclared pref, deliberately: Plex would return None
        # and the bug would surface later as odd behaviour instead of here.
        return self.DEFAULTS[key]


def _strip_diacritics(value):
    """Plex's String.StripDiacritics: NFKD, then drop everything non-ASCII.

    Reproduced faithfully INCLUDING its lossiness -- a non-Latin string comes
    back empty, and a multi-word one comes back as just its spaces. That second
    behaviour is the whole reason name_key needs `folded.strip()`, so a lenient
    stub here would hide the bug this harness exists to catch.
    """
    import unicodedata
    return unicodedata.normalize('NFKD', value).encode('ASCII', 'ignore').decode('ASCII')


def _levenshtein(a, b):
    """Util.LevenshteinDistance."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[j] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _make_framework():
    """The inert framework globals, as simple namespaces."""
    String = types.SimpleNamespace(
        StripDiacritics=_strip_diacritics,
        Quote=urllib.parse.quote,
        Unquote=urllib.parse.unquote,
        # Plex's String.Base64Encode takes str bytes (py2) and returns the
        # ascii b64 str; the py3 shim accepts both bytes and str.
        Base64Encode=lambda data: __import__('base64').b64encode(
            data if isinstance(data, bytes) else data.encode('utf-8')
        ).decode('ascii'),
    )
    Util = types.SimpleNamespace(LevenshteinDistance=_levenshtein)
    Locale = types.SimpleNamespace(
        Language=types.SimpleNamespace(English='en', German='de', French='fr')
    )

    class LivePlexCall(BaseException):
        """
        Raised when a test reaches a live Plex call.

        BaseException on purpose: every HTTP.Request/Core.storage call site in
        the plugin sits inside `except Exception`, which catches AssertionError
        -- so the old guard was silently converted into the plugin's normal
        error path and a test could go green having exercised nothing but the
        error handler. BaseException escapes every one of those handlers (the
        plugin has no bare `except:`) and surfaces as a loud unittest error.
        """

    def _unavailable(*_a, **_kw):
        raise LivePlexCall(
            'this test touched a live Plex call; keep unit tests to pure functions'
        )

    HTTP = types.SimpleNamespace(Request=_unavailable, Headers={}, CacheTime=0,
                                 ClearCache=lambda: None)
    Core = types.SimpleNamespace(storage=types.SimpleNamespace(load=_unavailable,
                                                               save=_unavailable))
    Proxy = types.SimpleNamespace(Media=lambda data, **kw: ('media', len(data or b'')),
                                  Preview=lambda *a, **kw: None)
    # Plex's Log is BOTH callable and a namespace -- logging.py uses `Log(msg)`
    # for info/warn and `Log.Debug`/`Log.Error` for the rest. A plain namespace
    # raised "not callable" from inside the code under test, which reads as a
    # product bug and is not one.
    class _Log(object):
        def __call__(self, *a, **kw):
            return None
        Debug = staticmethod(lambda *a, **kw: None)
        Info = staticmethod(lambda *a, **kw: None)
        Warn = staticmethod(lambda *a, **kw: None)
        Error = staticmethod(lambda *a, **kw: None)

    Log = _Log()
    # NO Track. Plex's AgentKit has no Track attribute -- proven live on
    # 2026-08-26, v1.3.214: `class AudiobookTrack(Agent.Track)` raised
    # AttributeError: 'AgentKit' object has no attribute 'Track' and took the
    # whole plugin down with "Exception starting plug-in". Adding Track HERE
    # is what let that ship: the harness modelled a type the sandbox does not
    # have, so 743 tests passed against a bundle that could not start. Only
    # ever mirror what AgentKit actually exposes.
    Agent = types.SimpleNamespace(Artist=type('Artist', (object,), {}),
                                  Album=type('Album', (object,), {}))
    Datetime = types.SimpleNamespace(ParseDate=_unavailable)

    return {
        'String': String, 'Util': Util, 'Locale': Locale, 'HTTP': HTTP, 'Core': Core,
        'Proxy': Proxy, 'Log': Log, 'Agent': Agent, 'Datetime': Datetime,
        'Prefs': FakePrefs(),
        'MetadataSearchResult': lambda **kw: kw,
        # Framework cache-duration constants, in seconds.
        'CACHE_1HOUR': 3600,
        'CACHE_1WEEK': 604800,
    }


def _install_compat():
    """py2 builtins and modules the agent code assumes exist."""
    builtins.unicode = str
    builtins.reduce = functools.reduce

    if 'StringIO' not in sys.modules:
        shim = types.ModuleType('StringIO')
        shim.StringIO = io.StringIO
        sys.modules['StringIO'] = shim

    # py2 urllib: flat quote/unquote/urlencode. py3 moved them under .parse, so
    # `urllib.quote(...)` would be an AttributeError without this.
    py2_urllib = types.ModuleType('urllib')
    py2_urllib.quote = urllib.parse.quote
    py2_urllib.unquote = urllib.parse.unquote
    py2_urllib.urlencode = urllib.parse.urlencode
    py2_urllib.quote_plus = urllib.parse.quote_plus
    sys.modules['urllib'] = py2_urllib


def _load_one(name, path, framework):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(framework)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_LOADED = {}


def load():
    """
    Import the agent modules and return them keyed by name.

    Memoised: the modules keep import-time state (compiled regexes, the memo
    dicts) and re-executing them per test file would be both slow and a source
    of cross-test surprises.
    """
    if _LOADED:
        return _LOADED

    _install_compat()
    framework = _make_framework()

    stdlib_urllib = sys.modules.get('urllib')
    saved_logging = sys.modules.get('logging')
    try:
        for name in MODULE_ORDER:
            _LOADED[name] = _load_one(name, os.path.join(CODE, name + '.py'), framework)
        # __init__.py imports from search_tools, which is now in sys.modules.
        _LOADED['agent'] = _load_one('incipit_agent',
                                     os.path.join(CODE, '__init__.py'), framework)
    finally:
        # Hand the stdlib its module names back. The bundle's logging.py shadows
        # the stdlib one, and leaving that swapped in would break any later
        # import in the test process.
        if saved_logging is not None:
            sys.modules['logging'] = saved_logging
        if stdlib_urllib is not None:
            sys.modules['urllib'] = stdlib_urllib

    _LOADED['framework'] = framework
    return _LOADED
