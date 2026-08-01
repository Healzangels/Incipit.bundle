"""
The authors/narrators swap-correction arbiter (v1.3.92).

WHY THIS EXISTS
    Seven UK Harry Potter sidecars arrived with the fields SWAPPED --
    authors=["Stephen Fry"], narrators=["J.K. Rowling"] -- and the agent
    faithfully searched author=Stephen Fry. The folder author arbitrates,
    root-free (get_library_roots is blocked at search time): media.artist
    counts as the folder author only when it names a segment of this file's
    own path. The correction fires ONLY on strict disagreement: narrators
    name the folder author AND authors do not.

    The holistic review's mutation sweep found the whole arbiter unpinned:
    inverting the disagreement condition, or dropping the path confirmation
    that keeps a narrator-tagged ALBUMARTIST from posing as the author,
    left the suite green. These tests are the pins.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']


class FakeMedia(object):
    def __init__(self, filename=None, album=None, artist=None):
        self.filename = filename
        self.album = album
        self.artist = artist
        self.title = None
        self.name = None
        self.tracks = None
        self.children = None


def tool_for(sidecar=None, filename=None, artist=None):
    tool = ST.AlbumSearchTool.__new__(ST.AlbumSearchTool)
    tool.prefs = dict(plexenv.FakePrefs.DEFAULTS)
    tool.content_type = 'books'
    tool.media = FakeMedia(filename=filename, artist=artist)
    tool.manual = False
    tool.normalizedName = ''
    tool.sidecar_cache = sidecar
    return tool


ROWLING_PATH = '/data/media/audiobooks/J.K. Rowling/HP3/Book 3.mp3'


class TestFolderAuthorConfirmed(unittest.TestCase):
    """The root-free arbiter: artist counts only when the path agrees."""

    def test_artist_matching_a_path_segment_is_confirmed(self):
        tool = tool_for(filename=ROWLING_PATH, artist='J.K. Rowling')
        self.assertEqual(tool.folder_author_confirmed(), 'J.K. Rowling')

    def test_match_is_key_folded_not_literal(self):
        # Case and diacritics must not defeat the confirmation.
        tool = tool_for(filename='/data/x/j.k. rowling/HP3/f.mp3',
                        artist='J.K. Rowling')
        self.assertEqual(tool.folder_author_confirmed(), 'J.K. Rowling')

    def test_narrator_tagged_albumartist_fails_confirmation(self):
        # THE guard: a narrator in the artist tag never appears as an author
        # folder, so it must come back None -- otherwise the swap correction
        # below could "confirm" the wrong person and flip clean fields.
        tool = tool_for(filename=ROWLING_PATH, artist='Stephen Fry')
        self.assertIsNone(tool.folder_author_confirmed())

    def test_no_artist_or_no_filename_is_none(self):
        self.assertIsNone(tool_for(filename=ROWLING_PATH, artist=None)
                          .folder_author_confirmed())
        self.assertIsNone(tool_for(filename=None, artist='J.K. Rowling')
                          .folder_author_confirmed())

    def test_percent_encoded_path_still_confirms(self):
        tool = tool_for(filename='/data/x/J.K.%20Rowling/HP3/f.mp3',
                        artist='J.K. Rowling')
        self.assertEqual(tool.folder_author_confirmed(), 'J.K. Rowling')


DOYLE_PATH = '/data/media/audiobooks/Arthur Conan Doyle/Sherlock/s.mp3'


class TestAnHonorificDoesNotDisableTheArbiter(unittest.TestCase):
    """
        A COURTESY TITLE IN THE ALBUMARTIST MUST NOT KILL THE CONFIRMATION.

        The comparison was strict `name_key(seg) == akey`, and name_key does
        not strip honorifics. Measured: artist='Sir Arthur Conan Doyle' with
        the folder "Arthur Conan Doyle" returned None, while the plain name
        confirmed. folder_author_confirmed IS the arbiter for the sidecar
        author/narrator swap correction, so an honorific-bearing ALBUMARTIST
        silently disabled that whole correction -- for every such author.

        v1.3.175 relaxed the same thing in ScoreTool.score_author (min of the
        plain and the title-stripped comparison) and landed on one of three
        surfaces. This is the second.
    """

    def test_the_plain_name_still_confirms(self):
        # Unchanged, and it is the case the strict comparison already served:
        # this must stay byte-identical.
        tool = tool_for(filename=DOYLE_PATH, artist='Arthur Conan Doyle')
        self.assertEqual(tool.folder_author_confirmed(), 'Arthur Conan Doyle')

    def test_an_honorific_in_the_tag_still_confirms(self):
        tool = tool_for(filename=DOYLE_PATH, artist='Sir Arthur Conan Doyle')
        self.assertEqual(
            tool.folder_author_confirmed(), 'Sir Arthur Conan Doyle',
            'the swap-correction arbiter is disabled for every author whose '
            'ALBUMARTIST carries a courtesy title')

    def test_an_honorific_in_the_FOLDER_confirms_too(self):
        # The provider is equally free to carry it, and so is the operator's
        # folder tree.
        tool = tool_for(filename='/data/x/Sir Arthur Conan Doyle/S/s.mp3',
                        artist='Arthur Conan Doyle')
        self.assertEqual(tool.folder_author_confirmed(), 'Arthur Conan Doyle')

    def test_the_returned_value_is_the_TAG_not_the_stripped_form(self):
        # Callers compare this against the sidecar's names; handing back a
        # value the tag never had would move the arbitration itself.
        tool = tool_for(filename=DOYLE_PATH, artist='Sir Arthur Conan Doyle')
        self.assertEqual(tool.folder_author_confirmed(), 'Sir Arthur Conan Doyle')

    def test_a_DIFFERENT_author_is_still_refused(self):
        # The relaxation must not turn into "any name confirms". A
        # narrator-tagged ALBUMARTIST still has to fail, honorific or not.
        for artist in ('Stephen Fry', 'Sir Stephen Fry', 'Sir Agatha Christie'):
            self.assertIsNone(
                tool_for(filename=DOYLE_PATH, artist=artist)
                .folder_author_confirmed(), artist)

    def test_the_swap_correction_now_fires_for_an_honorific_author(self):
        # The end of the chain, and the reason any of this matters: the
        # swapped-fields sidecar is only correctable when the arbiter answers.
        tool = tool_for(
            sidecar={'authors': ['Stephen Fry'],
                     'narrators': ['Sir Arthur Conan Doyle']},
            filename=DOYLE_PATH, artist='Sir Arthur Conan Doyle')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['Sir Arthur Conan Doyle'])
        self.assertEqual(narrators, ['Stephen Fry'])


class TestSwapCorrection(unittest.TestCase):
    """sidecar_people: flip ONLY on strict disagreement."""

    def swapped_sidecar(self):
        return {'authors': ['Stephen Fry'], 'narrators': ['J.K. Rowling']}

    def test_swapped_fields_are_flipped(self):
        # The live UK Harry Potter shape: recovered, not discarded -- the
        # narrator is the signal that separates same-title editions.
        tool = tool_for(sidecar=self.swapped_sidecar(),
                        filename=ROWLING_PATH, artist='J.K. Rowling')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['J.K. Rowling'])
        self.assertEqual(narrators, ['Stephen Fry'])

    def test_self_narrated_book_is_untouched(self):
        # 69 sidecars in the operator's library have the author narrating
        # their own book: both fields name the folder author, so authors_ok
        # holds and NOTHING moves. Inverting the condition flips them all.
        tool = tool_for(
            sidecar={'authors': ['Brian Jacques'],
                     'narrators': ['Brian Jacques', 'A Full Cast']},
            filename='/data/x/Brian Jacques/Redwall/r.mp3',
            artist='Brian Jacques')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['Brian Jacques'])
        self.assertEqual(narrators, ['Brian Jacques', 'A Full Cast'])

    def test_clean_sidecar_is_untouched(self):
        tool = tool_for(
            sidecar={'authors': ['J.K. Rowling'], 'narrators': ['Stephen Fry']},
            filename=ROWLING_PATH, artist='J.K. Rowling')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['J.K. Rowling'])
        self.assertEqual(narrators, ['Stephen Fry'])

    def test_no_swap_without_a_confirmed_folder_author(self):
        # Same swapped-looking fields, but the artist tag carries the narrator
        # so the path refuses to confirm it: without an arbiter the fields
        # must stay exactly as written.
        tool = tool_for(sidecar=self.swapped_sidecar(),
                        filename=ROWLING_PATH, artist='Stephen Fry')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['Stephen Fry'])
        self.assertEqual(narrators, ['J.K. Rowling'])

    def test_no_swap_when_either_field_is_empty(self):
        # The strict condition needs BOTH fields: an authors-only sidecar has
        # nothing to arbitrate, whatever the folder says.
        tool = tool_for(sidecar={'authors': ['Stephen Fry']},
                        filename=ROWLING_PATH, artist='J.K. Rowling')
        authors, narrators = tool.sidecar_people()
        self.assertEqual(authors, ['Stephen Fry'])
        self.assertEqual(narrators, [])

    def test_result_is_memoized(self):
        tool = tool_for(sidecar=self.swapped_sidecar(),
                        filename=ROWLING_PATH, artist='J.K. Rowling')
        first = tool.sidecar_people()
        # A second call must serve the memo, not re-derive (and certainly not
        # re-swap the already-corrected fields back).
        tool.sidecar_cache = {'authors': ['Nobody'], 'narrators': ['No One']}
        self.assertEqual(tool.sidecar_people(), first)


if __name__ == '__main__':
    unittest.main()
