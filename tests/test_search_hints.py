"""
The extra-args hint pipeline: what an automatic scan sends the incipit-api.

WHY THIS EXISTS
    "The Lost Stories Collection" carried its own fix and still went unmatched
    through a full rebuild: its sidecar's `asin` (B08WF9JR2P) is DEAD -- it
    resolves to nothing anywhere -- while the sidecar's `isbn` (9780593399439)
    is, in ISBN-10 form, the very id in the book's own audible.com URL. The API
    now falls back to the ISBN when the pinned ASIN is dead, but only if the
    bundle SENDS it. These tests pin that contract.

    Scope note: incipit_extra_args also reads media.tracks for the duration sum
    and media.children for the track title; those stay None here (a fresh-scan
    shape -- unanalyzed files send no duration), keeping the tests on the hint
    fields themselves.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']


class FakeMedia(object):
    def __init__(self, filename=None, album=None, artist=None, title=None):
        self.filename = filename
        self.album = album
        self.artist = artist
        self.title = title
        self.name = None
        self.tracks = None
        self.children = None


def tool_for(sidecar=None, filename=None, album=None, artist=None):
    """An AlbumSearchTool wired to one fake book, with no Plex behind it."""
    tool = ST.AlbumSearchTool.__new__(ST.AlbumSearchTool)
    tool.prefs = dict(plexenv.FakePrefs.DEFAULTS)
    tool.media = FakeMedia(filename=filename, album=album, artist=artist)
    tool.manual = False
    tool.normalizedName = album or ''
    # Pre-seed the memo so sidecar() never touches Core.storage.
    tool.sidecar_cache = sidecar
    return tool


SIDECAR = {
    'title': 'The Lost Stories Collection',
    'authors': ['Michael Scott'],
    'narrators': ['Alan Kelly'],
    'series': ['Secrets of the Immortal Nicholas Flamel #1-9'],
    'isbn': '9780593399439',
    'asin': 'B08WF9JR2P',
}


class TestIsbnHint(unittest.TestCase):
    def test_sidecar_isbn_is_sent(self):
        extra = tool_for(sidecar=dict(SIDECAR)).incipit_extra_args()
        self.assertIn('&isbn=9780593399439', extra)

    def test_isbn_rides_alongside_the_asin_not_instead(self):
        # The API decides which identity to use; the bundle sends both. The
        # sidecar ASIN here is B0-shaped, so it passes the existing guard.
        extra = tool_for(sidecar=dict(SIDECAR)).incipit_extra_args()
        self.assertIn('&asin=B08WF9JR2P', extra)
        self.assertIn('&isbn=9780593399439', extra)

    def test_isbn10_form_is_sent_too(self):
        sc = dict(SIDECAR)
        sc['isbn'] = '0-8044-2957-X'
        extra = tool_for(sidecar=sc).incipit_extra_args()
        self.assertIn('&isbn=080442957X'.replace('080442957X', '080442957X'), extra)

    def test_punctuation_is_stripped(self):
        sc = dict(SIDECAR)
        sc['isbn'] = '978-0-593-39943-9'
        extra = tool_for(sidecar=sc).incipit_extra_args()
        self.assertIn('&isbn=9780593399439', extra)

    def test_junk_isbn_is_not_sent(self):
        for junk in ('', '12345', 'not-an-isbn', None, 12345):
            sc = dict(SIDECAR)
            sc['isbn'] = junk
            extra = tool_for(sidecar=sc).incipit_extra_args()
            self.assertNotIn('&isbn=', extra)

    def test_no_sidecar_no_isbn(self):
        extra = tool_for(sidecar=None).incipit_extra_args()
        self.assertNotIn('&isbn=', extra)

    def test_typed_search_sends_nothing(self):
        # Typed Fix Match queries are deliberately context-free; the ISBN is
        # automatic-scan context like everything else here.
        tool = tool_for(sidecar=dict(SIDECAR))
        tool.manual = True
        tool.media.name = 'Some Typed Query'
        self.assertEqual(tool.incipit_extra_args(), '')


class TestExistingHintsStillRide(unittest.TestCase):
    def test_narrator_and_series_survive_the_addition(self):
        extra = tool_for(sidecar=dict(SIDECAR)).incipit_extra_args()
        self.assertIn('&narrator=', extra)
        self.assertIn('&series=', extra)


if __name__ == '__main__':
    unittest.main()
