"""
The author-in-path confirm tolerates a generational suffix.

WHY THIS EXISTS
    Artist recovery repairs a narrator mis-tagged as the artist: it asks the
    book API who wrote this title, then confirms that name against the file
    path so a same-title book by someone else can never win.

    The confirm compared RAW strings. Measured live 2026-08-11:
    Slaughterhouse-Five sits in <Kurt Vonnegut Jr>/ while every provider credits
    "Kurt Vonnegut", so "kurt vonnegut" != "kurt vonnegut jr", the confirm
    rejected a correct answer, and the album kept "Narrator: Ethan Hawke" as its
    artist -- the exact mis-tag the recovery exists to repair.

    The suffix strip is anchored to whitespace/comma so it can only ever remove
    a SEPARATE token: the folded key of "Hawaii" ends in "ii" and "Tel Aviv"
    ends in "iv", and a bare endswith() would quietly truncate real names.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']
keys = ST.name_keys_with_suffix_dropped


def confirm(path, authors):
    """Run the real confirm against one file path."""
    tool = ST.ArtistSearchTool.__new__(ST.ArtistSearchTool)
    tool.artist_path = lambda: path
    return tool.author_confirmed_in_path([{'authors': authors}])


VONNEGUT = '/data/media/audiobooks/Kurt Vonnegut Jr/Slaughterhouse-Five/x.m4b'


class SuffixTolerantConfirm(unittest.TestCase):
    def test_the_live_case(self):
        self.assertEqual(confirm(VONNEGUT, ['Kurt Vonnegut']), 'Kurt Vonnegut')

    def test_the_suffix_may_be_on_EITHER_side(self):
        path = '/data/media/audiobooks/Kurt Vonnegut/Slaughterhouse-Five/x.m4b'
        self.assertEqual(confirm(path, ['Kurt Vonnegut Jr.']), 'Kurt Vonnegut Jr.')

    def test_punctuation_and_spacing_stop_mattering_too(self):
        path = '/data/media/audiobooks/J R R Tolkien/The Hobbit/x.m4b'
        self.assertEqual(confirm(path, ['J.R.R. Tolkien']), 'J.R.R. Tolkien')

    def test_a_DIFFERENT_author_still_cannot_confirm(self):
        # The whole point of the confirm: a same-title book by someone else
        # must never be adopted.
        self.assertIsNone(confirm(VONNEGUT, ['Neil Gaiman']))

    def test_a_shared_surname_is_not_enough(self):
        self.assertIsNone(confirm(VONNEGUT, ['Mark Vonnegut']))

    def test_no_path_confirms_nothing(self):
        tool = ST.ArtistSearchTool.__new__(ST.ArtistSearchTool)
        tool.artist_path = lambda: None
        self.assertIsNone(tool.author_confirmed_in_path([{'authors': ['Kurt Vonnegut']}]))


class SuffixStripIsAnchored(unittest.TestCase):
    def test_a_real_name_ending_in_suffix_LETTERS_is_untouched(self):
        # "Hawaii" ends in "ii" and "Tel Aviv" in "iv"; only a separate token
        # may be stripped, or these lose their tails.
        self.assertEqual(keys('Hawaii'), {'hawaii'})
        self.assertEqual(keys('Tel Aviv'), {'telaviv'})

    def test_a_separate_suffix_token_yields_both_keys(self):
        self.assertEqual(keys('Kurt Vonnegut Jr'), {'kurtvonnegutjr', 'kurtvonnegut'})
        self.assertEqual(keys('Martin Luther King, Jr.'),
                         {'martinlutherkingjr', 'martinlutherking'})

    def test_a_plain_name_yields_one_key(self):
        self.assertEqual(keys('Kurt Vonnegut'), {'kurtvonnegut'})

    def test_a_bare_suffix_never_yields_an_empty_key(self):
        # An empty key would intersect with any other empty key and confirm
        # anything at all.
        self.assertNotIn('', keys('Jr'))
        self.assertNotIn('', keys(''))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(keys(''), set())
        self.assertEqual(keys(None), set())


if __name__ == '__main__':
    unittest.main()
