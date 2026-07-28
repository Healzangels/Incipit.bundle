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
    tool.content_type = 'books'
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


class TestSidecarIncipitIdQuickMatch(unittest.TestCase):
    """
        The operator's record pin for a recording no catalog knows (the
        2010: Odyssey Two class: an NLS talking book whose only honest match
        is a narrator-less OpenLibrary work row -- confidence scoring can
        NEVER safely auto-apply such a record, and shouldn't). A hand-written
        `incipit_id` in the sidecar says "this file IS this record": it
        quick-matches at 100 through the same lane as an embedded ASIN,
        entirely deterministic, and survives every rebuild. Synthetic
        incipit namespaces only -- B0 ASINs belong in the `asin` field with
        its own guards -- and like the filename ASIN it never rides a TYPED
        search, which is the user actively correcting identity.
    """

    def test_sidecar_incipit_id_quick_matches(self):
        sc = {'incipit_id': 'openlibrary-works-OL36469512W'}
        tool = tool_for(sidecar=sc, album='2010: Odyssey Two')
        self.assertEqual(tool.check_for_asin(), 'openlibrary-works-OL36469512W_us')

    def test_all_synthetic_namespaces_accepted(self):
        for rid in ('hardcover-edition-666221', 'hardcover-book-429510',
                    'overdrive-9406208'):
            tool = tool_for(sidecar={'incipit_id': rid}, album='Some Book')
            self.assertEqual(tool.check_for_asin(), rid + '_us')

    def test_typed_search_ignores_the_pin(self):
        tool = tool_for(sidecar={'incipit_id': 'openlibrary-works-OL36469512W'},
                        album='Odyssey Two')
        tool.manual = True
        tool.media.name = 'Odyssey Two'
        self.assertIsNone(tool.check_for_asin())

    def test_junk_and_foreign_values_are_ignored(self):
        for bad in ('B017V4NOZ0', 'https://openlibrary.org/OL1W', '../../etc',
                    '', 42, None):
            tool = tool_for(sidecar={'incipit_id': bad}, album='Some Book')
            self.assertIsNone(tool.check_for_asin())


class TestQuickMatchDisplay(unittest.TestCase):
    """
        The Fix Match row for a quick match. ASIN rows keep their historical
        shape (raw ASIN + upstream's dummy 1969 -- harmless there because the
        /books/{asin} record's releaseDate overwrites it on update). A sidecar
        incipit_id pin row must NOT: the raw id read like a mismatch in the
        dialog, and the pinned OL/Hardcover record can lack a releaseDate, so
        the dummy 1969 leaked onto the album card as 1969-12-31 (proven live
        on 2010: Odyssey Two).
    """

    def test_pin_row_shows_sidecar_title_and_no_year(self):
        sc = {'title': '2010: Odyssey Two',
              'incipit_id': 'openlibrary-works-OL36469512W'}
        tool = tool_for(sidecar=sc, album='Odyssey Two')
        self.assertEqual(
            tool.quick_match_display('openlibrary-works-OL36469512W_us'),
            ('2010: Odyssey Two', None))

    def test_pin_row_falls_back_to_album_then_id(self):
        tool = tool_for(sidecar={'incipit_id': 'overdrive-9406208'},
                        album='Some Book')
        self.assertEqual(tool.quick_match_display('overdrive-9406208_us'),
                         ('Some Book', None))
        bare = tool_for(sidecar={'incipit_id': 'overdrive-9406208'})
        self.assertEqual(bare.quick_match_display('overdrive-9406208_us'),
                         ('overdrive-9406208_us', None))

    def test_asin_row_keeps_historical_shape(self):
        tool = tool_for(sidecar=dict(SIDECAR), album='Some Book')
        self.assertEqual(tool.quick_match_display('B08WF9JR2P_us'),
                         ('B08WF9JR2P_us', 1969))


class TestPinRegexAndRegion(unittest.TestCase):
    """
        Two 2026-07-28 review findings on the sidecar pin.

        1. The namespace needed a SEPARATOR. `^(?:openlibrary|hardcover|
           overdrive)[A-Za-z0-9_-]+$` accepted `overdrive9406208` and
           `hardcover_12345`, and since the quick-match id is joined as
           `id + '_' + region` and later split on the FIRST '_', an
           underscored pin resolved to the bare namespace: the update
           requested /books/hardcover?region=12345 forever, at score 100,
           with nothing logged. The docstring promised malformed pins
           degrade to a normal search; now they do.

        2. The region came from the WRONG STRING. The pin branch passed the
           id to check_for_region, so a '[uk]' marker in the path was
           discarded and '_us' was baked into metadata.id permanently.
    """

    def test_separator_is_required(self):
        for bad in ('overdrive9406208', 'hardcoverX', 'openlibraryZZZ'):
            tool = tool_for(sidecar={'incipit_id': bad}, album='Some Book')
            self.assertIsNone(tool.check_for_asin(), bad)

    def test_underscores_are_rejected(self):
        for bad in ('overdrive_9406208', 'openlibrary_works_OL1W',
                    'hardcover_edition_666221'):
            tool = tool_for(sidecar={'incipit_id': bad}, album='Some Book')
            self.assertIsNone(tool.check_for_asin(), bad)

    def test_real_hyphenated_ids_still_pin(self):
        for good in ('openlibrary-works-OL36469512W', 'hardcover-edition-666221',
                     'hardcover-book-429510', 'overdrive-9406208'):
            tool = tool_for(sidecar={'incipit_id': good}, album='Some Book')
            self.assertEqual(tool.check_for_asin(), good + '_us')

    def test_region_comes_from_the_filename_not_the_id(self):
        tool = tool_for(
            sidecar={'incipit_id': 'hardcover-edition-666221'},
            album='Some Book',
            filename='/data/media/Author/Title%20%5Buk%5D/book.m4b')
        self.assertEqual(tool.check_for_asin(), 'hardcover-edition-666221_uk')


class TestRegionMarkerIsValidated(unittest.TestCase):
    """
        2026-07-28 review, five independent confirmations and verified live.

        `region_regex` matches ANY bracketed two letters, so `[CD]`, `[HQ]`,
        `[EN]` and the natural uppercase `[UK]` all became the region. The
        server's RegionSchema is a lowercase enum that hard-400s anything
        else, and `make_request` treats an answered 4xx from our own host as
        permanent -- so the search dies with only "No results found", and the
        value is joined into metadata.id, making every later lookup 400
        forever with no refresh able to heal it. On the stock Audible path
        the same value is an unguarded dict index (KeyError).

        A marker must be a region we can actually ask for.
    """

    def region_for(self, path):
        tool = tool_for(album='Some Book', filename=path)
        tool.check_for_region(path)
        return tool.region_override

    def test_a_real_marker_is_honoured_in_either_case(self):
        self.assertEqual(self.region_for('/books/Author/Title [uk]/b.m4b'), 'uk')
        self.assertEqual(self.region_for('/books/Author/Title [UK]/b.m4b'), 'uk')

    def test_a_non_region_token_falls_back_to_the_pref(self):
        for junk in ('[CD]', '[HQ]', '[EN]', '[v2]', '[V2]'):
            path = '/books/Author/Title %s/b.m4b' % junk
            self.assertEqual(self.region_for(path), 'us', junk)

    def test_every_accepted_marker_is_a_region_the_api_accepts(self):
        # The enum the server validates against, from src/config/types.ts.
        valid = {'au', 'ca', 'de', 'es', 'fr', 'in', 'it', 'jp', 'uk', 'us'}
        for code in list(valid):
            path = '/books/Author/Title [%s]/b.m4b' % code
            self.assertIn(self.region_for(path), valid, code)


class TestFilenameAsinNeedsTheB0Anchor(unittest.TestCase):
    """
        The filename probe used a shape-only regex with no B0 anchor and no
        boundary, so any 10-character run of uppercase/digits qualified --
        an embedded ISBN-13 yields the 10-digit substring `9780593399`. That
        short-circuits the ENTIRE pipeline at score 100 (no fan-out, no
        scoring, no duration veto, no telemetry), then 404s, leaving the
        album pinned to a nonexistent record that only a manual Fix Match
        clears. The sidecar path was hardened for exactly this on 2026-07-26
        ("a print ISBN-10 satisfies it"); the filename path never was.
    """

    def test_an_isbn_substring_no_longer_quick_matches(self):
        tool = tool_for(album='Some Book',
                        filename='/books/Author/Title 9780593399439/b.m4b')
        self.assertIsNone(tool.check_for_asin())

    def test_a_real_embedded_asin_still_quick_matches(self):
        tool = tool_for(album='Some Book',
                        filename='/books/Author/Title B08WF9JR2P/b.m4b')
        self.assertEqual(tool.check_for_asin(), 'B08WF9JR2P_us')

    def test_a_typed_asin_is_still_honoured(self):
        # Typing an ASIN into Search Options is the most explicit identity a
        # user can give; that branch is unchanged.
        tool = tool_for(album='B08WF9JR2P')
        self.assertEqual(tool.check_for_asin(), 'B08WF9JR2P_us')


class TestBuiltQueryCarriesTheHints(unittest.TestCase):
    """
        THE QUERY, not the helper.

        The 2026-07-28 mutation sweep turned `query += self.incipit_extra_args()`
        into a bare `self.incipit_extra_args()` -- result discarded -- and the
        whole hint suite stayed green, because every test called the helper
        directly. That mutation silently drops EVERY signal at once: the
        duration veto (the primary wrong-edition guard), the sidecar ASIN and
        ISBN pins, narrator and series. Nothing else in the system notices; the
        symptom is a slow drift into wrong editions with no error anywhere.

        These drive build_search_args and assert on the string actually sent.
    """

    def album_tool(self, sidecar=None, album='A Title', artist='An Author',
                   manual=False, typed=None):
        tool = ST.AlbumSearchTool.__new__(ST.AlbumSearchTool)
        tool.prefs = dict(plexenv.FakePrefs.DEFAULTS)
        tool.prefs['api_base_url'] = 'http://api.test:3737'
        tool.content_type = 'books'
        tool.media = FakeMedia(album=album, artist=artist)
        tool.media.name = typed
        tool.manual = manual
        tool.normalizedName = album or ''
        tool.sidecar_cache = sidecar
        tool.resolved_title = None
        return tool

    def test_the_query_carries_the_sidecar_hints(self):
        tool = self.album_tool(sidecar=dict(SIDECAR))
        query = tool.build_search_args()
        self.assertIn('title=', query)
        self.assertIn('&isbn=9780593399439', query)
        self.assertIn('&asin=B08WF9JR2P', query)
        self.assertIn('&narrator=', query)
        self.assertIn('&series=', query)

    def test_the_query_is_not_just_title_and_author(self):
        # The exact shape the discarded-result mutation produces.
        tool = self.album_tool(sidecar=dict(SIDECAR))
        query = tool.build_search_args()
        stripped = query.replace('title=', '').replace('&author=', '')
        self.assertTrue(
            len(query) > len('title=A%20Title&author=An%20Author') + 10,
            'query carries no hints beyond title/author: %r' % query)
        self.assertTrue(stripped)

    def test_a_typed_search_is_marked_and_context_free(self):
        tool = self.album_tool(sidecar=dict(SIDECAR), manual=True,
                               typed='Some Typed Query')
        query = tool.build_search_args()
        self.assertIn('&manual=1', query)
        # The typed flow is deliberately context-free: hints must NOT ride.
        self.assertNotIn('&isbn=', query)
        self.assertNotIn('&narrator=', query)

    def test_an_automatic_search_is_not_marked_manual(self):
        tool = self.album_tool(sidecar=dict(SIDECAR))
        self.assertNotIn('&manual=1', tool.build_search_args())


class TestTypedSearchStaysContextFree(unittest.TestCase):
    """
        The TYPED Fix Match flow is deliberately context-free.

        Two flows exist and must not converge: the dialog's AUTO-fired list
        re-runs the automatic match (full context: sidecar title, author,
        duration, pins -- so it scores like the scan it repeats), while a
        query the user TYPES into Search Options is them correcting a wrong
        identity. Feeding the old context back into that search re-pins
        exactly what they are trying to escape -- a leak this codebase has
        shipped before ("Project Hail Mary" typed under the Brian Jacques
        artist could not surface Andy Weir's book).

        The 2026-07-28 mutation sweep found 5 of the 7 `is_typed_search()`
        gates entirely unpinned: each could be deleted with a green suite.
        These assert the BEHAVIOUR of each gate, so a deleted one fails.
    """

    def typed(self, **kw):
        tool = tool_for(**kw)
        tool.manual = True
        tool.media.name = 'Project Hail Mary'
        tool.resolved_title = None
        return tool

    def auto(self, **kw):
        tool = tool_for(**kw)
        tool.resolved_title = None
        return tool

    def test_sidecar_title_is_ignored_when_the_user_typed_one(self):
        # Otherwise Fix Match silently searches the sidecar's title instead
        # of what was typed, whenever a metadata.json exists.
        self.assertIsNone(self.typed(sidecar=dict(SIDECAR)).sidecar_title())
        self.assertEqual(
            self.auto(sidecar=dict(SIDECAR)).sidecar_title(),
            'The Lost Stories Collection')

    def test_no_author_is_attached_to_a_typed_query(self):
        # The scanner's artist is the WRONG author by assumption here.
        typed = self.typed(sidecar=dict(SIDECAR), artist='Brian Jacques')
        self.assertIsNone(typed.resolve_author())
        self.assertTrue(self.auto(sidecar=dict(SIDECAR),
                                  artist='Brian Jacques').resolve_author())

    def test_no_hints_ride_a_typed_query(self):
        self.assertEqual(
            self.typed(sidecar=dict(SIDECAR)).incipit_extra_args(), '')
        self.assertNotEqual(
            self.auto(sidecar=dict(SIDECAR)).incipit_extra_args(), '')

    def test_a_filename_asin_never_repins_a_typed_correction(self):
        # The quick-match lane runs BEFORE the query is built, so an ungated
        # filename ASIN re-pinned the exact identity being corrected and the
        # typed query never executed at all.
        name = '/data/media/Author/Title/B0ABCDEFGH - book.m4b'
        self.assertIsNone(self.typed(filename=name).check_for_asin())

    def test_a_sidecar_pin_never_repins_a_typed_correction(self):
        self.assertIsNone(
            self.typed(sidecar={'incipit_id': 'openlibrary-works-OL1W'}
                       ).check_for_asin())
