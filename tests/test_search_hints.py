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



class TestSandboxSafeExtraction(unittest.TestCase):
    """
    The first ISBN implementation iterated the string character by character
    (`for ch in sc_isbn`). That works in py2, py3 AND this harness -- but dies
    in the Plex sandbox: RestrictedPython guards iteration via _getiter_,
    which calls __iter__, and py2 unicode strings have no __iter__ (they
    iterate via the legacy __getitem__ protocol). Measured live 2026-07-26:
    "incipit isbn probe failed: 'unicode' object has no attribute '__iter__'"
    on every scan, so the hint silently never rode. The harness cannot
    reproduce that (py3 strings have __iter__), so this pins the SOURCE: no
    for-loop over the isbn value.
    """

    def test_isbn_extraction_does_not_iterate_the_string(self):
        code_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code'
        )
        with open(os.path.join(code_dir, 'search_tools.py')) as f:
            src = f.read()
        start = src.index('ISBN, sent ALONGSIDE')
        end = src.index('incipit isbn probe failed')
        self.assertNotIn('for ch in', src[start:end])


class TestResultRowContract(unittest.TestCase):
    """
    The album search's display loop reads r['title'] (and the author/narrator
    keys) unconditionally, so score_create_result must ALWAYS set them. The
    author and narrator keys were hardened long ago with exactly this
    reasoning written next to them; title was still conditional, and a
    title-less API row (the B08WF9JR2P husk) crashed the whole listing with
    KeyError: 'title' -- one bad row blanked every result for the book.
    """

    def row_for(self, title):
        score = ST.ScoreTool.__new__(ST.ScoreTool)
        score.asin = 'B000TEST01'
        score.author = 'Michael Scott'
        score.narrator = 'Alan Kelly'
        score.date = None
        score.region = 'us'
        score.title = title
        score.year = None
        return score.score_create_result(85)

    def test_title_key_always_present(self):
        for title in ('A Real Title', '', None):
            row = self.row_for(title)
            self.assertIn('title', row)

    def test_empty_title_degrades_to_empty_string(self):
        self.assertEqual(self.row_for(None)['title'], '')
        self.assertEqual(self.row_for('')['title'], '')

    def test_real_title_kept(self):
        self.assertEqual(self.row_for('A Real Title')['title'], 'A Real Title')

if __name__ == '__main__':
    unittest.main()


class FakeArtistMedia(object):
    def __init__(self, filename=None, album=None, artist=None, name=None):
        self.filename = filename
        self.album = album
        self.artist = artist
        self.name = name
        self.title = None
        self.tracks = None
        self.children = None


def artist_tool_for(sidecar=None, album=None, filename=None):
    tool = ST.ArtistSearchTool.__new__(ST.ArtistSearchTool)
    tool.prefs = dict(plexenv.FakePrefs.DEFAULTS)
    tool.media = FakeArtistMedia(filename=filename, album=album)
    tool.manual = False
    tool.sidecar_cache = sidecar
    return tool


class TestArtistRecoveryTitle(unittest.TestCase):
    """
        The artist-recovery book search must prefer the SIDECAR title.

        Measured live on The Hand of Oberon (2026-07-26): the file's album tag
        is rip-tool junk ('coa_04_The Hand of Oberon Unabridged'), and the
        /books recovery search on it returns ZERO rows -- while the sidecar's
        title ('The Hand of Oberon') plus the file duration answers the right
        book at confidence 1.0 with the author the recovery needs. The sidecar
        is machine-written truth and already preferred everywhere else the
        title matters; the recovery was the one consumer still reading the raw
        tag.
    """

    JUNK = 'coa_04_The Hand of Oberon Unabridged'
    FN = ('%2Fdata%2Fmedia%2Faudiobooks-updated%2FRoger%20Zelazny'
          '%2FAmber%20-%20The%20Corwin%20Cycle%2F4%20-%20The%20Hand%20of%20Oberon'
          '%2FThe%20Hand%20of%20Oberon%2Em4b')

    def test_sidecar_title_wins_over_the_album_tag(self):
        tool = artist_tool_for(
            sidecar={'title': 'The Hand of Oberon'},
            album=self.JUNK, filename=self.FN)
        self.assertEqual(tool.artist_album_title(), 'The Hand of Oberon')

    def test_album_tag_still_used_without_a_sidecar(self):
        tool = artist_tool_for(sidecar=None, album=self.JUNK, filename=self.FN)
        self.assertEqual(tool.artist_album_title(), self.JUNK)

    def test_file_basename_remains_the_last_resort(self):
        tool = artist_tool_for(sidecar=None, album=None, filename=self.FN)
        self.assertEqual(tool.artist_album_title(), 'The Hand of Oberon')
