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


if __name__ == '__main__':
    unittest.main()
