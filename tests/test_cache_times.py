"""
Plugin HTTP cache TTLs: what may replay stale, and for how long.

WHY THIS EXISTS
    The AUTHOR record is the one the API actively HEALS after first write --
    the monthly scheduler sweep and the operator's force=1 both fill in a
    portrait/bio that arrived late (Goodreads answered Roger Zelazny's very
    first lookup cache-cold and empty; the record healed minutes later). The
    agent then hid the healed record behind the default WEEK-long HTTP cache:
    every Refresh Metadata replayed the empty pre-heal body -- measured live
    2026-07-26, "Fetching '.../authors/B000APXZHK...' from the HTTP cache" --
    and no operator action short of deleting Plex's cache dir could surface
    the bio. Author lookups now cache for an HOUR, like searches: still free
    within a scan's fan-out, but a healed record lands the same day.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']


class TestAuthorUpdateCacheTime(unittest.TestCase):
    def tearDown(self):
        AG.Prefs.pop('dev_disable_http_cache', None)

    def test_author_updates_cache_for_an_hour_not_a_week(self):
        AG.Prefs['dev_disable_http_cache'] = False
        self.assertEqual(AG.author_update_cache_time(), AG.CACHE_1HOUR)

    def test_dev_toggle_disables_it(self):
        AG.Prefs['dev_disable_http_cache'] = True
        self.assertEqual(AG.author_update_cache_time(), 0)

    def test_the_author_call_site_passes_it(self):
        # The TTL only matters if the author call_item_api actually passes it;
        # a bare make_request(update_url) silently inherits the week default.
        code_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code'
        )
        with open(os.path.join(code_dir, '__init__.py')) as f:
            src = f.read()
        start = src.index('Calls the metadata API to get author details')
        window = src[start:start + 800]
        self.assertIn('cache_time=author_update_cache_time()', window)


if __name__ == '__main__':
    unittest.main()
