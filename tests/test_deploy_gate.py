"""
The deploy gate: can you tell, from outside, which build is actually loaded?

WHY THIS EXISTS
    A RestrictedPython violation kills this plugin at COMPILE time, silently --
    py_compile passes, Fix Match just spins, and no error reaches the UI. The
    only positive proof the bundle loaded is the version banner Start() writes.

    Two independent defects made that proof unavailable, both found in the
    2026-07-31 review:

    1. The banner was written through log.separator(log_level="info"), and
       Logging.info() emits only when logging_level is DEBUG or INFO. The
       SHIPPED default (DefaultPrefs.json) is WARN. So at default prefs no
       banner was ever written, and "plugin never loaded" looked exactly like
       "plugin loaded fine" -- indistinguishable from the silent death the
       banner exists to detect.

       Note the second half of that bug: separator() only special-cased
       "debug" and fell through to info() for everything else, so passing
       log_level="warn" would NOT have helped. Both halves are pinned below.

    2. Info.plist reported 1.3.86 while _version.py said 1.3.170 -- 84 releases
       of drift. The banner carries _version.py, but Plex's own plugin manager
       reports Info.plist, so any external deploy check read a stale version and
       could not detect a failed deploy.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
LOGGING = MODULES['logging']

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    path = os.path.join(ROOT, *parts)
    with open(path) as handle:
        return handle.read()


class TestVersionsAgree(unittest.TestCase):
    """Info.plist is what Plex reports; _version.py is what the banner says."""

    def plist_versions(self):
        plist = read('Contents', 'Info.plist')
        found = {}
        for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
            match = re.search(
                r'<key>' + key + r'</key>\s*<string>([^<]+)</string>', plist)
            self.assertIsNotNone(match, '%s missing from Info.plist' % key)
            found[key] = match.group(1).strip()
        return found

    def code_version(self):
        match = re.search(r'version\s*=\s*"([^"]+)"',
                          read('Contents', 'Code', '_version.py'))
        self.assertIsNotNone(match, 'version missing from _version.py')
        return match.group(1).strip()

    def test_info_plist_matches_the_code_version(self):
        code = self.code_version()
        for key, value in self.plist_versions().items():
            self.assertEqual(
                value, code,
                '%s is %s but _version.py is %s -- Plex would report the '
                'stale one and a failed deploy would look successful'
                % (key, value, code))


class TestBannerIsVisibleAtShippedDefault(unittest.TestCase):
    """The banner must survive the log level the bundle actually ships with."""

    def shipped_default_level(self):
        prefs = read('Contents', 'DefaultPrefs.json')
        match = re.search(
            r'"id"\s*:\s*"logging_level".*?"default"\s*:\s*"([A-Z]+)"',
            prefs, re.S)
        self.assertIsNotNone(match, 'logging_level default not found')
        return match.group(1)

    def test_separator_honours_warn(self):
        # separator() used to ignore every level except "debug" and fall
        # through to info(), so a caller asking for warn was silently demoted.
        emitted = []
        log = LOGGING.Logging()
        log.warn = lambda message, *args: emitted.append(('warn', message))
        log.info = lambda message, *args: emitted.append(('info', message))
        log.debug = lambda message, *args: emitted.append(('debug', message))
        log.separator(msg='hello', log_level='warn')
        self.assertEqual([level for level, _ in emitted], ['warn'])

    def test_separator_still_routes_info_and_debug(self):
        emitted = []
        log = LOGGING.Logging()
        log.warn = lambda message, *args: emitted.append(('warn', message))
        log.info = lambda message, *args: emitted.append(('info', message))
        log.debug = lambda message, *args: emitted.append(('debug', message))
        log.separator(msg='a', log_level='info')
        log.separator(msg='b', log_level='debug')
        self.assertEqual([level for level, _ in emitted], ['info', 'debug'])

    def test_the_start_banner_is_not_written_below_the_default_level(self):
        source = read('Contents', 'Code', '__init__.py')
        banner = re.search(
            r'log\.separator\(\s*msg=\(\s*"Incipit Audiobooks Agent v"'
            r'.*?log_level="([a-z]+)"', source, re.S)
        self.assertIsNotNone(
            banner, 'the Start() version banner could not be located')
        level = banner.group(1)
        default = self.shipped_default_level()
        order = {'DEBUG': 0, 'INFO': 1, 'WARN': 2, 'ERROR': 3}
        self.assertLessEqual(
            order[default], order[level.upper()],
            'the banner logs at %s but the bundle ships logging_level=%s, so '
            'no banner is written at default prefs and a silent sandbox death '
            'is undetectable' % (level.upper(), default))


class TestMainGuardIsLastInEveryTestFile(unittest.TestCase):
    """
        `if __name__ == '__main__': unittest.main()` must be the LAST top-level
        statement of a test file.

        Placed mid-file it does not break discovery -- so the suite total stays
        honest -- but a DIRECT run (`python3 tests/test_scoring.py`, which is how
        a single fix gets checked while writing it) executes only the classes
        defined ABOVE it and prints a confident OK. Measured 2026-08-01 before
        this guard: test_scoring ran 8 of 16, test_cache_times 4 of 20,
        test_sort_titles 3 of 18, test_search_hints 12 of 50, test_posters 122
        of 231. Every one of those gaps was NEW tests, appended below an old
        guard -- i.e. the shape hides exactly the tests someone is watching.

        AST rather than a text search, so a guard inside a comment or a string
        cannot satisfy it and a trailing helper function cannot hide beneath it.
    """

    def _test_files(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return sorted(
            os.path.join(here, name) for name in os.listdir(here)
            if name.startswith('test_') and name.endswith('.py'))

    def is_main_guard(self, node):
        import ast
        if not isinstance(node, ast.If):
            return False
        test = node.test
        return (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == '__name__'
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == '__main__')

    def test_the_guard_is_the_final_top_level_statement(self):
        import ast
        offenders = []
        checked = 0
        for path in self._test_files():
            with open(path) as handle:
                tree = ast.parse(handle.read(), path)
            body = tree.body
            positions = [i for i, node in enumerate(body)
                         if self.is_main_guard(node)]
            if not positions:
                continue
            checked += 1
            if positions[-1] != len(body) - 1 or len(positions) > 1:
                after = body[positions[0] + 1]
                offenders.append(
                    (os.path.basename(path), body[positions[0]].lineno,
                     'first statement skipped by a direct run is on line %s'
                     % after.lineno))
        self.assertGreater(checked, 0, 'no __main__ guard found to check at all')
        self.assertEqual(
            offenders, [],
            'a mid-file __main__ guard makes `python3 <file>` silently skip '
            'every class defined below it and still print OK: %r' % (offenders,))


class SandboxIllegalNames(unittest.TestCase):
    """
    A LEADING-UNDERSCORE NAME KILLS THE WHOLE PLUGIN, SILENTLY.

    Plex compiles this bundle under RestrictedPython, whose name check is:

        if name.startswith('_') and name != '_':
            error('"%s" is an invalid variable name because it starts with "_"')

    That is a COMPILE error, so the agent never loads at all -- no banner, no
    handler, every search and refresh in the library falls back to nothing.
    Nothing in the normal test suite can see it: these tests run under real
    CPython, where `_unused` is perfectly ordinary.

    Shipped live twice now. v1.3.190 introduced `for fam_sha, _unused in ...`
    and the .99 deploy died with exactly the message above at line 2430 --
    caught only because a poster fix mysteriously did nothing and the log was
    read directly. A prior incident is why the rule was written down in the
    first place; a note was clearly not enough, so this is the gate.

    Bare `_` is explicitly legal (RestrictedPython exempts it) and dunders
    (`__init__`) are used throughout and demonstrably load, so both are
    allowed here -- matching the known-good baseline exactly.
    """

    CODE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'Contents', 'Code')

    @staticmethod
    def illegal(name):
        if not name or not name.startswith('_'):
            return False
        if name == '_':
            return False
        if name.startswith('__') and name.endswith('__'):
            return False
        return True

    @classmethod
    def offenders(cls, source, filename='<inline>'):
        """
            Every name in `source` RestrictedPython would reject at compile
            time. ONE implementation, shared by the whole-bundle scan below and
            the per-form tests: a second copy written for the synthetic cases
            would MIRROR this walk instead of guarding it, and a form missed
            here would then be missed there too.
        """
        import ast
        found = []
        for node in ast.walk(ast.parse(source, filename)):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.arg):
                name = node.arg
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Attribute):
                # RestrictedPython blocks `obj._attr` reads for the same
                # reason; the baseline has none, so this is free coverage.
                name = node.attr
            elif isinstance(node, ast.alias):
                # RestrictedPython's visitImport/visitFrom checkName() the
                # IMPORTED name FIRST and the asname second. Reading only
                # `asname` therefore caught the single rarest fatal form
                # (`import os as _os`) and missed `import _helpers`,
                # `from x import _thing`, `import _foo as bar`,
                # `from x import _a as b` and `from . import _mod` -- all of
                # which kill the plugin at load.
                #
                # DELIBERATELY NOT ast.ImportFrom.module: `from _version import
                # version` binds only `version`, RestrictedPython never
                # checkName()s the module string, and Contents/Code/__init__.py
                # has shipped exactly that line since 1.3.0 with v1.3.19x
                # loading in production. Checking it would fail a known-good
                # build (pinned below).
                name = node.name if cls.illegal(node.name) else node.asname
            elif isinstance(node, ast.ExceptHandler):
                # `except SomeError as _e:` is fatal too, and nothing above
                # sees it: ExceptHandler.name is a bare str under py3, not an
                # ast.Name. (Under a py2 ast it IS an ast.Name and the branch
                # above catches it, hence the type test rather than a cast.)
                if isinstance(node.name, str):
                    name = node.name
            if cls.illegal(name):
                found.append((filename, getattr(node, 'lineno', '?'), name))
        return found

    # Every form RestrictedPython kills the plugin for. One assertion each,
    # because they fail for different reasons in the visitor.
    FATAL_FORMS = (
        'import _helpers\n',
        'from x import _thing\n',
        'import _foo as bar\n',
        'from x import _a as b\n',
        'from . import _mod\n',
        'import os as _os\n',
        'try:\n    x = 1\nexcept ValueError as _e:\n    pass\n',
    )

    def test_no_leading_underscore_names_anywhere_in_the_bundle(self):
        offenders = []
        scanned = 0
        for filename in sorted(os.listdir(self.CODE_DIR)):
            if not filename.endswith('.py'):
                continue
            path = os.path.join(self.CODE_DIR, filename)
            with open(path) as handle:
                offenders.extend(self.offenders(handle.read(), filename))
            scanned += 1
        self.assertGreater(scanned, 0, 'no bundle source scanned at all')
        self.assertEqual(
            offenders, [],
            'RestrictedPython rejects these at COMPILE time and the whole '
            'plugin dies silently -- no banner, no agent: %r' % (offenders,))

    def test_every_fatal_import_and_except_form_is_caught(self):
        for source in self.FATAL_FORMS:
            self.assertNotEqual(
                self.offenders(source), [],
                'this form is fatal under RestrictedPython and the gate '
                'let it through: %r' % (source,))

    def test_from_underscore_module_import_plain_name_still_passes(self):
        # THE KNOWN-GOOD BASELINE. `from _version import version` binds only
        # `version`; RestrictedPython's visitFrom checkName()s the alias, never
        # node.module. Widening the gate to the module string would fail a
        # build that is loading in production right now.
        self.assertEqual(self.offenders('from _version import version\n'), [])

    def test_that_baseline_is_not_hypothetical(self):
        # ...and it is the line the bundle actually ships, so the exemption
        # above is load-bearing rather than a hypothetical carve-out.
        path = os.path.join(self.CODE_DIR, '__init__.py')
        with open(path) as handle:
            self.assertIn('from _version import version', handle.read())

    def test_ordinary_imports_are_not_flagged(self):
        # The gate must not be over-broad: a dotted module, a plain alias and
        # a normal except handler are all fine.
        self.assertEqual(self.offenders(
            'import os\n'
            'import os.path\n'
            'import json as j\n'
            'from x import y as z\n'
            'try:\n    x = 1\nexcept ValueError as e:\n    pass\n'), [])


if __name__ == '__main__':
    unittest.main()
