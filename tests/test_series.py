"""
Series/volume derivation and the sort title it produces.

The sort title is what shelves a book next to its siblings, and it is built
from two halves that come from different places -- so it fails silently: the
book just sorts under its own name and nobody notices until a series looks
scrambled. Every case below is one of those.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']
ST = MODULES['search_tools']


class SeriesKey(unittest.TestCase):
    def test_leading_article_is_folded(self):
        # The same Spellmonger series came back as "Spellmonger" for book 13
        # and "The Spellmonger" for book 17 -- sixteen books under S, one
        # under T.
        self.assertEqual(UT.series_key('The Spellmonger'), UT.series_key('Spellmonger'))

    def test_punctuation_is_folded(self):
        self.assertEqual(UT.series_key("Gaunt's Ghosts"), UT.series_key('Gaunts Ghosts'))


class VolumePrefix(unittest.TestCase):
    def setUp(self):
        self.tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)

    def test_bare_number_gains_the_marker(self):
        self.assertEqual(self.tool.volume_prefix('10'), 'Book 10')

    def test_existing_marker_is_left_alone(self):
        self.assertEqual(self.tool.volume_prefix('Book 10'), 'Book 10')

    def test_decimal_is_preserved(self):
        # A novella folder "3.5 - ..." used to yield "Book 3", colliding with
        # the real book 3 and shelving the novella on top of it.
        self.assertEqual(self.tool.volume_prefix('3.5'), 'Book 3.5')

    def test_none_and_blank_yield_no_volume(self):
        self.assertEqual(self.tool.volume_prefix(None), '')
        self.assertEqual(self.tool.volume_prefix('  '), '')

    def test_zero_is_a_real_volume_not_a_falsy_hole(self):
        self.assertEqual(self.tool.volume_prefix(0), 'Book 0')


class FolderNumber(unittest.TestCase):
    def test_common_numbering_conventions(self):
        for folder, want in (('27 - Cube Route', '27'),
                             ('17 Harpy Thyme', '17'),
                             ('1. The Gunslinger', '1'),
                             ('03_Title', '03'),
                             ('3.5 - The Road To Sevendor', '3.5')):
            match = UT.FOLDER_NUMBER_RE.match(folder)
            self.assertIsNotNone(match, folder)
            self.assertEqual(match.group(1), want, folder)

    def test_a_year_shaped_folder_is_not_a_number(self):
        self.assertIsNone(UT.FOLDER_NUMBER_RE.match('1984'))

    def test_a_number_glued_to_a_word_is_not_a_number(self):
        self.assertIsNone(UT.FOLDER_NUMBER_RE.match('27Cube'))


class TitleCleanup(unittest.TestCase):
    def test_trailing_series_suffix_is_stripped(self):
        self.assertEqual(
            UT.strip_trailing_series('Changes: The Dresden Files, Book 12'), 'Changes')
        self.assertEqual(UT.strip_trailing_series('Steel World (Undying Mercenaries #1)'),
                         'Steel World')

    def test_a_real_subtitle_is_kept(self):
        # No number marker means it is a subtitle, not a series tail.
        self.assertEqual(UT.strip_trailing_series('Changes: A Novel'), 'Changes: A Novel')

    def test_stripping_never_empties_a_title(self):
        self.assertTrue(UT.strip_trailing_series('The Dresden Files, Book 12'))

    def test_marketing_blurb_is_recognised(self):
        self.assertTrue(UT.is_marketing_subtitle(
            "from the Bestselling Series That Inspired BBC's the Watch"))
        self.assertFalse(UT.is_marketing_subtitle('A Novel'))
        self.assertFalse(UT.is_marketing_subtitle(''))


class PartIndexAndFolderTitle(unittest.TestCase):
    def test_ripper_part_indices_are_stripped(self):
        for raw, want in (('Title (264)', 'Title'),
                          ('Title [7]', 'Title'),
                          ('Title - 12', 'Title'),
                          ('Title - pt03', 'Title'),
                          ('Title Ch01', 'Title')):
            self.assertEqual(ST.strip_part_index(raw), want, raw)

    def test_a_real_numeric_tail_is_kept(self):
        # These are titles, not part indices. Stripping them would search for
        # the wrong book entirely.
        for raw in ('Xanth 24', 'Fahrenheit 451', '1984'):
            self.assertEqual(ST.strip_part_index(raw), raw)

    def test_book_number_survives_part_stripping(self):
        # "Defiance of the Fall, Book 10" must not become "Book 1".
        self.assertEqual(ST.strip_part_index('Defiance of the Fall, Book 10'),
                         'Defiance of the Fall, Book 10')

    def test_folder_title_recovery(self):
        self.assertEqual(
            ST.folder_title_from_path('/data/x/2013 - The Pilot [Last Horizon 4]/f.m4b'),
            'The Pilot')

    def test_missing_album_placeholders(self):
        self.assertTrue(ST.is_missing_album('[Unknown Album]'))
        self.assertTrue(ST.is_missing_album(''))
        self.assertTrue(ST.is_missing_album(None))
        self.assertFalse(ST.is_missing_album('Mattimeo'))


class SortTitle(unittest.TestCase):
    """set_metadata_sort_title, driven through a minimal fake metadata object."""

    def _tool(self, series, volume, title, existing_sort='', force=False):
        tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
        tool.series, tool.volume, tool.title = series, volume, title
        tool.force = force
        tool.metadata = type('M', (), {'title': title, 'title_sort': existing_sort})()
        return tool

    def test_series_and_volume_build_the_shelf_key(self):
        tool = self._tool('Defiance of the Fall', 'Book 10', 'Defiance of the Fall')
        tool.set_metadata_sort_title()
        self.assertEqual(tool.metadata.title_sort,
                         'Defiance of the Fall, Book 10 - Defiance of the Fall')

    def test_leading_article_is_dropped_from_the_series(self):
        tool = self._tool('The Expanse', 'Book 10', "Memory's Legion")
        tool.set_metadata_sort_title()
        self.assertTrue(tool.metadata.title_sort.startswith('Expanse, Book 10'))

    def test_a_standalone_gets_no_series_prefix(self):
        # A series NAME with no position cannot build a shelf key, so the book
        # must sort under its own title rather than a half-formed one.
        tool = self._tool('Some Series', '', 'Last Man Standing')
        tool.set_metadata_sort_title()
        self.assertEqual(tool.metadata.title_sort, 'Last Man Standing')

    def test_an_existing_sort_title_is_not_overwritten_without_force(self):
        # This is why a folder move needs Refresh Metadata and not just a scan.
        tool = self._tool('Defiance of the Fall', 'Book 10', 'Defiance of the Fall',
                          existing_sort='Stale Value')
        tool.set_metadata_sort_title()
        self.assertEqual(tool.metadata.title_sort, 'Stale Value')

    def test_force_rewrites_a_stale_sort_title(self):
        tool = self._tool('Defiance of the Fall', 'Book 10', 'Defiance of the Fall',
                          existing_sort='Stale Value', force=True)
        tool.set_metadata_sort_title()
        self.assertEqual(tool.metadata.title_sort,
                         'Defiance of the Fall, Book 10 - Defiance of the Fall')


if __name__ == '__main__':
    unittest.main()
