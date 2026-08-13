"""
Which urls count as an image CDN, and how hard they are paced.

WHY THIS EXISTS
    make_request gives every third-party host a flat 1s pause so a cold scan
    cannot hammer or get throttled by one. Measured 2026-08-12 against the live
    library, that flat rate was costing ~50-63 minutes of a cold scan of 1650
    albums: 83% of books carry alternate covers averaging 1.88 urls apiece, the
    primary cover lands on the same hosts, and 81% of the elapsed cost was the
    sleep rather than the 229ms download.

    Image CDNs are unauthenticated and built to serve pictures, so they earn a
    lighter pace than an API that meters us. Two things must not rot:

      - the HOST TEST. It is a dot-boundary suffix match, exactly like
        is_api_host's separator guard. These urls come straight out of a JSON
        response body (`imageAlternates`), so an attacker-shaped host is
        reachable in principle -- see test_api_host.py, which documents the same
        reachability for the same class of check.
      - the GAP. It is a MINIMUM GAP, not a blind sleep: a flat sleep would also
        be paid on plugin HTTP-cache hits and on urls that already arrived
        slowly, and the cover path asks for the same thumb more than once per
        album, so a blind sleep would hand back much of what this saves.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']


class ImageCdnHost(unittest.TestCase):
    def test_every_host_the_api_actually_served_is_recognised(self):
        # Not invented: these are the six hosts every alternate-cover url in a
        # 120-book sample of the live library resolved to, plus the primary
        # cover host. A url shape nobody serves is not evidence.
        for url in (
            'https://m.media-amazon.com/images/I/51abc.jpg',
            'https://assets.hardcover.app/edition/123/cover.jpg',
            'https://img1.od-cdn.com/ImageType-100/0111-1/x.jpg',
            'https://img2.od-cdn.com/ImageType-100/0111-1/x.jpg',
            'https://img3.od-cdn.com/ImageType-100/0111-1/x.jpg',
            'https://is1-ssl.mzstatic.com/image/thumb/x/1400x1400bb.jpg',
        ):
            self.assertTrue(AG.is_image_cdn_host(url), url)

    def test_the_sharded_hosts_match_by_SUFFIX_not_by_listing_each(self):
        # od-cdn fronts img1/2/3 and mzstatic fronts is1-ssl/is2-ssl/...; pinning
        # exact hostnames would pace most of OverDrive at 1s for no reason.
        self.assertTrue(AG.is_image_cdn_host('https://img9.od-cdn.com/x.jpg'))
        self.assertTrue(AG.is_image_cdn_host('https://is5-ssl.mzstatic.com/x.jpg'))

    def test_a_metered_API_host_is_NOT_an_image_cdn(self):
        # The mistake this list must never make: an endpoint that answers
        # queries, rather than one that returns bytes for a picture.
        for url in (
            'https://api.audible.com/1.0/catalog/products/B01',
            'https://api.audnex.us/books/B01',
            'https://api.hardcover.app/v1/graphql',
            'https://api.bookinfo.pro/work/1',
        ):
            self.assertFalse(AG.is_image_cdn_host(url), url)

    def test_the_DOT_BOUNDARY_is_the_guard(self):
        # Each of these contains a listed suffix and is a different host.
        for url in (
            'https://evil-od-cdn.com/x.jpg',            # no separator before it
            'https://od-cdn.com.attacker.example/x.jpg',  # suffix as a PREFIX
            'https://notmzstatic.com/x.jpg',
            'https://media-amazon.com.evil.example/x.jpg',
        ):
            self.assertFalse(AG.is_image_cdn_host(url), url)

    def test_USERINFO_cannot_forge_the_host(self):
        # "https://<listed host>@evil.example/x.jpg" is a request to evil.example.
        # Reading left to right hands back the decoy instead.
        self.assertFalse(
            AG.is_image_cdn_host('https://m.media-amazon.com@evil.example/x.jpg'))
        self.assertFalse(
            AG.is_image_cdn_host('https://a@b@assets.hardcover.app@evil.example/x.jpg'))

    def test_a_listed_host_still_matches_with_userinfo_port_or_trailing_dot(self):
        self.assertTrue(AG.is_image_cdn_host('https://user:pw@m.media-amazon.com/x.jpg'))
        self.assertTrue(AG.is_image_cdn_host('https://m.media-amazon.com:443/x.jpg'))
        self.assertTrue(AG.is_image_cdn_host('https://m.media-amazon.com./x.jpg'))
        self.assertTrue(AG.is_image_cdn_host('HTTPS://M.MEDIA-AMAZON.COM/x.jpg'))

    def test_junk_is_not_a_cdn_and_does_not_raise(self):
        for url in ('', None, 'not a url', '/relative/path.jpg', 'https://', 42):
            self.assertFalse(AG.is_image_cdn_host(url), repr(url))


class ImageCdnPacing(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.clock = [1000.0]
        self.real_sleep, self.real_time = AG.sleep, AG.time

        def fake_sleep(seconds):
            self.slept.append(seconds)
            self.clock[0] += seconds

        AG.sleep = fake_sleep
        AG.time = lambda: self.clock[0]
        AG.image_cdn_pace_state['last'] = 0.0
        AG.image_cdn_pace_state['announced'] = False

    def tearDown(self):
        AG.sleep, AG.time = self.real_sleep, self.real_time
        AG.image_cdn_pace_state['last'] = 0.0
        AG.image_cdn_pace_state['announced'] = False

    def test_the_first_fetch_is_not_delayed(self):
        AG.pace_image_cdn()
        self.assertEqual(self.slept, [])

    def test_a_burst_is_held_to_the_gap(self):
        AG.pace_image_cdn()
        AG.pace_image_cdn()
        AG.pace_image_cdn()
        self.assertEqual(self.slept, [AG.IMAGE_CDN_MIN_GAP, AG.IMAGE_CDN_MIN_GAP])

    def test_time_ALREADY_elapsed_counts_toward_the_gap(self):
        # The whole reason this is a gap and not a sleep: a fetch that took
        # longer than the gap has already paced itself, and a cache hit
        # arriving late must not be charged twice.
        AG.pace_image_cdn()
        self.clock[0] += AG.IMAGE_CDN_MIN_GAP
        AG.pace_image_cdn()
        self.assertEqual(self.slept, [])

    def test_a_PARTIAL_wait_is_credited_not_restarted(self):
        AG.pace_image_cdn()
        self.clock[0] += AG.IMAGE_CDN_MIN_GAP / 2.0
        AG.pace_image_cdn()
        self.assertAlmostEqual(self.slept[0], AG.IMAGE_CDN_MIN_GAP / 2.0, places=6)

    def test_the_engaged_line_is_logged_ONCE_not_per_fetch(self):
        # It is the load-proof string for this version, and it runs on a path
        # that fires thousands of times in a scan -- logging per fetch would
        # bury the log it is meant to be found in.
        seen = []
        real_info = AG.log.info
        AG.log.info = lambda msg, *a: seen.append(msg % a if a else msg)
        try:
            for _ in range(5):
                AG.pace_image_cdn()
        finally:
            AG.log.info = real_info
        engaged = [m for m in seen if 'image-CDN gap' in m]
        self.assertEqual(len(engaged), 1, seen)

    def test_the_gap_is_a_REAL_saving_over_the_flat_third_party_pace(self):
        # Guards the point of the change rather than its mechanics: if someone
        # retunes this to 1s, it has stopped being worth its own code path.
        self.assertGreater(AG.IMAGE_CDN_MIN_GAP, 0)
        self.assertLess(AG.IMAGE_CDN_MIN_GAP, 1)


class MakeRequestPacing(unittest.TestCase):
    """The WIRING. Recognising a host is worth nothing if make_request ignores it."""

    def setUp(self):
        self.real_request = AG.HTTP.Request
        self.real_sleep = AG.sleep
        self.real_pace = AG.pace_image_cdn
        self.sleeps = []
        self.paced = []
        AG.sleep = lambda n: None
        AG.pace_image_cdn = lambda: self.paced.append(1)
        # RESTORED in tearDown. Prefs.DEFAULTS is process-global, so leaving an
        # api_base_url behind here made test_posters fail seven tests in a suite
        # run while passing on its own -- the order-dependent shape, caused by
        # the test rather than the code.
        self.original_base = AG.Prefs['api_base_url']
        AG.Prefs.DEFAULTS['api_base_url'] = 'http://10.0.1.99:3737'

        class OkResponse(object):
            content = '{"ok":true}'

            def __str__(self):
                return self.content

        sleeps = self.sleeps

        def request(url, **kwargs):
            sleeps.append(kwargs.get('sleep'))
            return OkResponse()

        AG.HTTP.Request = request

    def tearDown(self):
        AG.HTTP.Request = self.real_request
        AG.sleep = self.real_sleep
        AG.pace_image_cdn = self.real_pace
        AG.Prefs.DEFAULTS['api_base_url'] = self.original_base

    def test_our_own_api_is_unpaced_and_does_not_take_the_cdn_path(self):
        AG.make_request('http://10.0.1.99:3737/books/B01')
        self.assertEqual(self.sleeps, [0])
        self.assertEqual(self.paced, [])

    def test_an_image_cdn_hands_the_framework_ZERO_and_paces_itself(self):
        AG.make_request('https://m.media-amazon.com/images/I/51abc.jpg')
        # The framework argument stays the integer it has always been given --
        # a fraction there is a framework detail this bundle cannot test, and if
        # it floored to an int the pacing would vanish silently.
        self.assertEqual(self.sleeps, [0])
        self.assertEqual(self.paced, [1])

    def test_A_METERED_THIRD_PARTY_STILL_PAYS_THE_FULL_SECOND(self):
        # THE regression guard. The 1s pace exists so a cold scan cannot get
        # throttled off Audible/audnexus; nothing in the image-CDN change may
        # loosen it, and a too-broad suffix list is exactly how it would.
        for url in ('https://api.audible.com/1.0/catalog/products/B01',
                    'https://api.audnex.us/books/B01'):
            self.sleeps[:] = []
            self.paced[:] = []
            AG.make_request(url)
            self.assertEqual(self.sleeps, [1], url)
            self.assertEqual(self.paced, [], url)


if __name__ == '__main__':
    unittest.main()
