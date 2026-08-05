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

    def test_an_operator_force_bypasses_it(self):
        # An explicit Refresh Metadata must see the healed record NOW, not
        # replay a pre-heal body for up to an hour -- the same rule the
        # search path already follows (a manual Fix Match passes cache 0).
        AG.Prefs['dev_disable_http_cache'] = False
        self.assertEqual(AG.author_update_cache_time(True), 0)
        self.assertEqual(AG.author_update_cache_time(False), AG.CACHE_1HOUR)

    def test_the_author_call_site_passes_it(self):
        # The TTL only matters if the author call_item_api actually passes it
        # (a bare make_request(update_url) silently inherits the week
        # default) -- and passes the FORCE flag, or the operator's Refresh
        # replays the cached pre-heal body.
        code_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code'
        )
        with open(os.path.join(code_dir, '__init__.py')) as f:
            src = f.read()
        start = src.index('Calls the metadata API to get author details')
        window = src[start:start + 800]
        self.assertIn('cache_time=author_update_cache_time(helper.force)', window)


class TestMakeRequest4xx(unittest.TestCase):
    """
        An answered 4xx FROM OUR OWN API is a PERMANENT no -- retrying it four
        times with exponential backoff (measured live on /authors?name=4,
        which the API answered 400) just burns ~7s per search teaching
        nothing. Everything else keeps the full retry ladder: transport
        failures and 5xx/429 are the blips the ladder exists for, 408/425 are
        transient by definition even though they are 4xx, and THIRD-PARTY 4xx
        (Audible's edge serves one-off bot-check 403s; image CDNs blip) are
        exactly what the 2s retry has been absorbing -- aborting on those
        turns a blip into an unmatched book.
    """

    def setUp(self):
        self.real_request = AG.HTTP.Request
        self.real_sleep = AG.sleep
        AG.sleep = lambda n: None
        # The abort applies only to our own configured API host.
        AG.Prefs['api_base_url'] = 'http://api.test'
        self.calls = []

    def tearDown(self):
        AG.HTTP.Request = self.real_request
        AG.sleep = self.real_sleep
        AG.Prefs.pop('api_base_url', None)

    def raiser(self, code):
        calls = self.calls
        class FakeHttpError(Exception):
            pass
        def request(url, **kwargs):
            calls.append(url)
            err = FakeHttpError('HTTP %s' % code)
            err.code = code
            raise err
        return request

    def lazy_raiser(self, code):
        """A fake that behaves like Plex's REAL HTTP.Request: LAZY.

        The class above raises at construction, which is not what the framework
        does. HTTP.Request returns a wrapper and the network happens at
        .content/str(). This repo documents that twice -- fetch_url_bytes'
        docstring, and the 2026-07-25 measurement where an un-fetched wrapper
        reached image_dimensions and raised 'HTTPRequest object has no
        attribute __getitem__'.

        The difference was not academic. With the error surfacing only at the
        CALLER's str(), it landed outside make_request's try, so the retry
        ladder and every 4xx/5xx decision below it were dead code. Proven from
        production: across four agent log files 'Failed http request attempt'
        appears ZERO times while 55 HTTP errors surfaced at call sites. The
        ladder had never once fired -- and these tests all passed throughout,
        because every one of them raises eagerly.
        """
        calls = self.calls

        class FakeHttpError(Exception):
            pass

        class LazyResponse(object):
            @property
            def content(self):
                err = FakeHttpError('HTTP %s' % code)
                err.code = code
                raise err

            def __str__(self):
                return str(self.content)

        def request(url, **kwargs):
            calls.append(url)
            return LazyResponse()
        return request

    def test_a_LAZY_transient_still_gets_the_full_ladder(self):
        # Audible's one-off bot-check 403 -- the exact transient the ladder was
        # written for, and the one it was never reaching.
        AG.HTTP.Request = self.lazy_raiser(403)
        self.assertIsNone(AG.make_request('http://images.example/blip.jpg'))
        self.assertEqual(len(self.calls), 4)

    def test_a_LAZY_permanent_code_still_aborts_after_one(self):
        # The other direction: forcing the fetch must not resurrect the ladder
        # for codes that are permanent, or a rotted image URL costs seconds per
        # album again.
        AG.HTTP.Request = self.lazy_raiser(404)
        self.assertIsNone(AG.make_request('http://images.example/dead.jpg'))
        self.assertEqual(len(self.calls), 1)

    def test_a_LAZY_success_is_returned_with_its_content_intact(self):
        # Forcing .content must not consume or break the response the callers
        # then read -- all eight call sites do str() or .content on it.
        calls = self.calls

        class OkResponse(object):
            content = '{"ok":true}'

            def __str__(self):
                return self.content

        def request(url, **kwargs):
            calls.append(url)
            return OkResponse()

        AG.HTTP.Request = request
        out = AG.make_request('http://api.test/x')
        self.assertEqual(str(out), '{"ok":true}')
        self.assertEqual(len(self.calls), 1)

    def test_a_400_stops_after_one_attempt(self):
        AG.HTTP.Request = self.raiser(400)
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 1)

    def test_a_404_stops_after_one_attempt(self):
        AG.HTTP.Request = self.raiser(404)
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 1)

    def test_a_THIRD_PARTY_404_also_stops_after_one_attempt(self):
        """
        The abort was gated on is_api_host(url), so a permanent 404 from a
        THIRD-PARTY host ran the whole ladder: 4 attempts at timeout=90 plus
        1+2+4s of backoff plus the framework's per-call pacing. A rotted
        Amazon author-photo URL is the most common third-party failure here,
        and compile_metadata calls fetch_url_bytes up to four times per artist
        update on a force -- so one dead image cost tens of seconds PER ALBUM,
        inside Plex's bounded update window.

        A 403 is transient (Audible's edge serves one-off bot-checks, which is
        why the ladder exists); a 404 or 410 never is. Narrow the abort to the
        permanent codes and it applies to any host.
        """
        AG.HTTP.Request = self.raiser(404)
        self.assertIsNone(AG.make_request('http://images.example/dead.jpg'))
        self.assertEqual(len(self.calls), 1)

    def test_a_third_party_410_stops_too(self):
        AG.HTTP.Request = self.raiser(410)
        self.assertIsNone(AG.make_request('http://images.example/gone.jpg'))
        self.assertEqual(len(self.calls), 1)

    def test_a_third_party_403_KEEPS_the_ladder(self):
        # Transient by nature -- Audible's edge bot-check is exactly this, and
        # it is the case the ladder was written for. Must not be narrowed away.
        AG.HTTP.Request = self.raiser(403)
        self.assertIsNone(AG.make_request('http://images.example/blocked.jpg'))
        self.assertEqual(len(self.calls), 4)

    def test_a_500_keeps_the_full_ladder(self):
        AG.HTTP.Request = self.raiser(500)
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 4)

    def test_a_429_keeps_the_full_ladder(self):
        # A rate-limit push-back is transient by definition.
        AG.HTTP.Request = self.raiser(429)
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 4)

    def test_no_code_keeps_the_full_ladder(self):
        # A refused connection / timeout carries no HTTP code at all.
        calls = self.calls
        def request(url, **kwargs):
            calls.append(url)
            raise IOError('connection refused')
        AG.HTTP.Request = request
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 4)

    def test_a_third_party_403_keeps_the_full_ladder(self):
        # Audible's edge serves one-off bot-check 403s that the retry has
        # been absorbing for the whole life of this agent; a stock-mode scan
        # that aborts on the first one loses the book for the pass.
        AG.HTTP.Request = self.raiser(403)
        self.assertIsNone(AG.make_request('https://api.audible.com/1.0/catalog'))
        self.assertEqual(len(self.calls), 4)

    def test_a_transient_408_keeps_the_full_ladder_even_from_the_api(self):
        # 408 Request Timeout (and 425 Too Early) are 4xx by number and
        # transient by meaning -- a proxy hiccup, not a parsed rejection.
        AG.HTTP.Request = self.raiser(408)
        self.assertIsNone(AG.make_request('http://api.test/x'))
        self.assertEqual(len(self.calls), 4)


class TestBookUpdateCacheTime(unittest.TestCase):
    """
        The BOOK item fetch had no cache-time argument at all, so it took the
        week-long default with no way for an operator to bypass it.

        Measured live 2026-07-30, and this is what made it visible: the API was
        fixed to parse a series for The Spine of the World (B00HFW9SUE ->
        "Legend of Drizzt #12") and confirmed serving it, the operator then ran
        Refresh Metadata on every R. A. Salvatore book, and the album kept its
        old folder-derived sort title "Forgotten Realms Chronological, Book 12".
        The agent replayed a week-old cached /books/{asin} body carrying no
        series -- the same trap author_update_cache_time was written for, on the
        one path that never got the fix.

        The week default STAYS for a scan (those records really are stable and a
        cold scan re-requests the same ASIN per track). What changes is that an
        operator's explicit Refresh (force) bypasses it, exactly like a manual
        Fix Match passes cache 0 and an author Refresh bypasses its hour.
    """

    def tearDown(self):
        AG.Prefs.pop('dev_disable_http_cache', None)

    def test_a_scan_still_uses_the_week_long_cache(self):
        AG.Prefs['dev_disable_http_cache'] = False
        self.assertEqual(AG.book_update_cache_time(), AG.CACHE_1WEEK)
        self.assertEqual(AG.book_update_cache_time(False), AG.CACHE_1WEEK)

    def test_an_operator_force_bypasses_it(self):
        # The whole point: a human asking NOW must not be answered from a body
        # cached before the fix they are refreshing to pick up.
        AG.Prefs['dev_disable_http_cache'] = False
        self.assertEqual(AG.book_update_cache_time(True), 0)

    def test_the_dev_toggle_disables_it(self):
        AG.Prefs['dev_disable_http_cache'] = True
        self.assertEqual(AG.book_update_cache_time(), 0)
        self.assertEqual(AG.book_update_cache_time(True), 0)

    def test_it_is_not_the_author_hour(self):
        # Books are not authors: author records self-heal server-side and were
        # dropped to an hour for that reason. Book records are stable, so the
        # scan-time cost of a week is worth keeping -- only the force bypass is
        # being added here.
        AG.Prefs['dev_disable_http_cache'] = False
        self.assertNotEqual(AG.book_update_cache_time(), AG.CACHE_1HOUR)


class TestBookUpdateCallSitePassesForce(unittest.TestCase):
    """
        The helper being right is not enough: the CALL SITE has to hand it the
        operator's force flag.

        Mutation caught this gap -- dropping `helper.force` from the
        call_item_api argument left the whole suite green, because every other
        test in this file drives book_update_cache_time() directly. That is
        exactly the shape this project keeps getting bitten by, so the wiring
        gets its own test.
    """

    class FakeHelper(object):
        content_type = 'books'

        def __init__(self, force):
            self.force = force
            self.date = None

        def build_url(self):
            return 'http://api.example/books/B00HFW9SUE?region=us'

        def parse_api_response(self, response):
            self.parsed = response

    def setUp(self):
        self.calls = []
        self.real_request = AG.make_request
        self.real_decode = AG.json_decode

        def fake_request(url, cache_time=None):
            self.calls.append(cache_time)
            return '{"asin": "B00HFW9SUE"}'

        AG.make_request = fake_request
        AG.json_decode = lambda body: {'asin': 'B00HFW9SUE'}
        AG.Prefs['dev_disable_http_cache'] = False

    def tearDown(self):
        AG.make_request = self.real_request
        AG.json_decode = self.real_decode
        AG.Prefs.pop('dev_disable_http_cache', None)

    def fetch(self, force):
        agent = AG.AudiobookAlbum()
        # The date parse at the end of call_item_api reaches a live framework
        # call, which the harness blocks on purpose. Stub only that -- the
        # cache_time argument is what this test is about.
        agent.getDateFromString = lambda value: None
        helper = self.FakeHelper(force)
        self.assertTrue(agent.call_item_api(helper))
        return self.calls[-1]

    def test_an_operator_refresh_reaches_the_network(self):
        # force=True must arrive as cache 0, not the week-long default -- this is
        # the bug that made a corrected API record invisible to Refresh Metadata.
        self.assertEqual(self.fetch(True), 0)

    def test_a_scan_still_gets_the_week_long_cache(self):
        self.assertEqual(self.fetch(False), AG.CACHE_1WEEK)

# LAST STATEMENT IN THE FILE, always. This guard used to sit mid-file, and
# `python3 tests/<file>.py` then ran only the classes DEFINED ABOVE IT and
# printed OK -- measured: test_scoring ran 8 of 16, test_cache_times 4 of 20,
# test_sort_titles 3 of 18. Discovery was unaffected, so the suite stayed
# honest while a direct run (how a single fix gets checked) silently skipped
# the new tests. tests/test_deploy_gate.py pins the position for every file.
if __name__ == '__main__':
    unittest.main()
