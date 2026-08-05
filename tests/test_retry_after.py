"""
A 429 states how long to wait, and the ladder has to obey it.

WHY THIS EXISTS
    Measured 2026-08-05 on a from-scratch rebuild of 1,591 albums. Plex
    re-matches an album once per track, so the scan sustained ~90 albums/min
    against the API's 100/min bucket and started taking 429s. The retry ladder
    backs off 1/2/4s -- it spends all four attempts inside ~7s, which cannot
    outlast a per-MINUTE window. So make_request returned None, update() logged
    "keeping existing metadata", and Plex kept whatever the file tags said:
    34 albums ended up titled '"The Way of Kings" by B.Sanderson w/ K.Reading'
    and three more went unmatched.

    That damage does not heal by itself. Plex does not re-run update() for an
    album it considers done, so every one of those albums needed an operator
    (or, that day, a human running refreshes by hand through the API) -- which
    is precisely the thing the agent is supposed to make unnecessary.

    Honouring Retry-After self-throttles the scan to the rate the server will
    actually serve. Slower, and correct.

SCOPE
    The parse is deliberately delta-seconds only; an HTTP-date returns None and
    falls back to the ladder's own backoff, which is the safe direction.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']

AGENT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'Contents', 'Code', '__init__.py')


class FakeHeaders(object):
    def __init__(self, value):
        self.value = value

    def get(self, name):
        if name == 'Retry-After':
            return self.value
        return None


class FakeErr(object):
    """Stands in for the HTTPError the ladder catches."""

    def __init__(self, value):
        self.headers = FakeHeaders(value)


class NoHeaders(object):
    """A transport failure carries no headers at all."""


class RetryAfterParsing(unittest.TestCase):
    def test_reads_an_integer_delay(self):
        self.assertEqual(AG.retry_after_seconds(FakeErr('47')), 47)

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(AG.retry_after_seconds(FakeErr('  12 ')), 12)

    def test_absent_header_is_none(self):
        self.assertIsNone(AG.retry_after_seconds(FakeErr(None)))

    def test_http_date_form_is_none_not_a_crash(self):
        # Falls back to the ladder's own backoff rather than raising inside the
        # except block, which would escape as an unhandled error mid-scan.
        self.assertIsNone(
            AG.retry_after_seconds(FakeErr('Wed, 05 Aug 2026 17:00:00 GMT')))

    def test_negative_is_rejected(self):
        # A negative sleep() is either an error or an instant retry; neither is
        # what the header meant.
        self.assertIsNone(AG.retry_after_seconds(FakeErr('-5')))

    def test_an_error_with_no_headers_is_none(self):
        # Transport failures (no response at all) reach the same code path.
        self.assertIsNone(AG.retry_after_seconds(NoHeaders()))


class LadderActuallyUsesIt(unittest.TestCase):
    """A helper nothing calls is dead code that still passes its own tests.

    These read the source because the behaviour lives inside make_request's
    retry loop, which cannot be driven here without a live HTTP stack.
    """

    def setUp(self):
        handle = open(AGENT_SRC)
        try:
            self.src = handle.read()
        finally:
            handle.close()

    def test_the_cap_exists_and_is_a_minute_or_less(self):
        self.assertTrue(hasattr(AG, 'MAX_RETRY_AFTER'),
                        'MAX_RETRY_AFTER is gone -- an uncapped wait can stall '
                        'Plex\'s update window indefinitely')
        self.assertGreater(AG.MAX_RETRY_AFTER, 0)
        self.assertLessEqual(
            AG.MAX_RETRY_AFTER, 60,
            'a cap above the limiter window stalls the scan for no benefit')

    def test_the_ladder_calls_it_and_caps_it(self):
        # assertTrue, not assertIn: assertIn prints the ENTIRE haystack on
        # failure, and the haystack here is a 5,000-line source file -- one
        # broken assertion buried the whole suite's output in 224KB of source.
        self.assertTrue(
            'retry_after_seconds(err)' in self.src,
            'the retry loop no longer consults Retry-After, so a 429 is back to '
            'the 1/2/4s backoff that cannot outlast a per-minute window')
        self.assertTrue(
            'min(stated, MAX_RETRY_AFTER)' in self.src,
            'the stated delay is no longer capped')

    def test_it_is_gated_on_429_specifically(self):
        self.assertTrue(
            re.search(r'if\s+err_code\s*==\s*429', self.src),
            'the Retry-After wait must apply to 429 only -- honouring it on '
            'every status would let any server dictate the scan\'s pace')

    def test_429_still_stays_on_the_retry_ladder(self):
        # The companion invariant: 429 is a 4xx by number but transient by
        # meaning. If it ever joined the answered-4xx abort, the wait computed
        # above would be unreachable because the loop breaks first.
        self.assertTrue(
            re.search(r'err_code\s+not\s+in\s+\(408,\s*425,\s*429\)', self.src),
            '429 must be excluded from the answered-4xx abort, or the ladder '
            'gives up before it ever waits')


if __name__ == '__main__':
    unittest.main()
