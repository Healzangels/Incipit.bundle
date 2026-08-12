"""
Which urls count as "our own API", and why the separator is load-bearing.

WHY THIS EXISTS
    is_api_host gates two things that must never reach a third party:

      - incipit_headers attaches the operator's Hardcover TOKEN
      - make_request drops the framework's 1s per-fetch pacing

    It was a bare `url.startswith(base.rstrip('/'))`, with no boundary after the
    base. A base of "http://incipit-api" -- a plausible container/service-name
    setting, and the pref is free text with a blank default -- therefore also
    matched "http://incipit-api.attacker.example/x.jpg", handing that host the
    Hardcover token and fetching it unpaced. The port form is the same shape:
    base "http://host:3737" matched "http://host:37370/...".

    Not every url reaching here is one we built: the alternate-cover path
    fetches urls straight out of a JSON response body (update_tools reads
    `imageAlternates`), so an attacker-shaped url is reachable in principle.

    Requiring the "/" costs nothing, because every api url in the plugin is
    built as `base.rstrip('/') + '/' + ...`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']


class ApiHostBase(unittest.TestCase):
    def set_base(self, value):
        AG.Prefs.DEFAULTS['api_base_url'] = value


class TestIsApiHost(ApiHostBase):
    def setUp(self):
        self.original = AG.Prefs['api_base_url']

    def tearDown(self):
        self.set_base(self.original)

    def test_our_own_api_urls_still_match(self):
        # The whole point: nothing legitimate may be lost.
        self.set_base('http://10.0.1.99:3737')
        for url in ['http://10.0.1.99:3737/books?title=x',
                    'http://10.0.1.99:3737/authors/B08WF9JR2P',
                    'http://10.0.1.99:3737/']:
            self.assertTrue(AG.is_api_host(url), url)

    def test_a_trailing_slash_on_the_pref_makes_no_difference(self):
        for base in ['http://10.0.1.99:3737', 'http://10.0.1.99:3737/',
                     'http://10.0.1.99:3737///']:
            self.set_base(base)
            self.assertTrue(AG.is_api_host('http://10.0.1.99:3737/books?title=x'), base)

    def test_the_bare_base_itself_matches(self):
        self.set_base('http://10.0.1.99:3737')
        self.assertTrue(AG.is_api_host('http://10.0.1.99:3737'))

    def test_a_LOOKALIKE_HOST_does_not_match(self):
        # THE BUG. A service-name base has no port or path to disambiguate it,
        # so the missing boundary is the only thing standing between the
        # operator's Hardcover token and a third party.
        self.set_base('http://incipit-api')
        self.assertFalse(AG.is_api_host('http://incipit-api.attacker.example/x.jpg'))
        self.assertFalse(AG.is_api_host('http://incipit-apifoo/books'))

    def test_a_LONGER_PORT_does_not_match(self):
        # Same gap with a port: 3737 is a prefix of 37370.
        self.set_base('http://10.0.1.99:3737')
        self.assertFalse(AG.is_api_host('http://10.0.1.99:37370/books'))

    def test_a_third_party_host_never_matches(self):
        self.set_base('http://10.0.1.99:3737')
        for url in ['https://api.audible.com/1.0/catalog/products?x=1',
                    'https://m.media-amazon.com/images/I/abc.jpg',
                    'https://api.audnex.us/books/B08WF9JR2P']:
            self.assertFalse(AG.is_api_host(url), url)

    def test_an_unset_pref_matches_nothing(self):
        # Stock mode talks only to Audible/audnexus, so no url is "ours" and
        # every fetch must keep its pacing.
        for base in ['', None]:
            self.set_base(base)
            self.assertFalse(AG.is_api_host('https://api.audnex.us/books/B0'))
            self.assertFalse(AG.is_api_host('http://anything/at/all'))

    def test_an_empty_url_is_not_ours(self):
        self.set_base('http://10.0.1.99:3737')
        for url in ['', None]:
            self.assertFalse(AG.is_api_host(url))


class TestTheTokenPathIsGone(ApiHostBase):
    """
        The Hardcover token USED to ride on every request to the API host, and
        is_api_host existed partly to keep it off third parties. Removed in
        1.3.206: every deployment self-hosts incipit-api with its own
        HARDCOVER_TOKEN, so the per-request forward bought nothing and cost a
        personal API key sitting in Plex's plaintext prefs on every server.

        This pins the REMOVAL. Deleting a secret-handling path is only worth
        anything if it stays deleted, and the obvious way it comes back is
        someone re-adding the pref for a "shared instance" that does not exist.
    """

    def test_incipit_headers_is_gone(self):
        self.assertFalse(
            hasattr(AG, 'incipit_headers'),
            'incipit_headers is back: the bundle is forwarding a secret again'
        )

    def test_no_hardcover_token_pref(self):
        self.assertNotIn(
            'hardcover_token', AG.Prefs.DEFAULTS,
            'the hardcover_token pref is back; the API reads HARDCOVER_TOKEN from its own env'
        )

    def test_no_request_sends_a_hardcover_header(self):
        # Belt and braces: nothing anywhere in the agent may name the header.
        import os
        code_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'Contents', 'Code')
        offenders = []
        for name in os.listdir(code_dir):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(code_dir, name)) as handle:
                body = handle.read()
            # The docstring in __init__.py explains the removal, so allow a
            # mention in a comment while refusing a live header dict.
            for line in body.splitlines():
                stripped = line.strip()
                if 'x-hardcover-token' in stripped and not stripped.startswith('#'):
                    offenders.append('%s: %s' % (name, stripped[:70]))
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
