"""
The album sort title: "{series}, {volume} - {title}".

WHY THIS EXISTS
    Goodreads series 131836 is literally titled "Six of Crows " -- a
    librarian's trailing space in the canonical name. The API adopted it
    verbatim (fixed there, af9fb5c), the composer concatenated it raw, and
    Plex saved the sort title "Six of Crows , Book 2 - Crooked Kingdom" --
    splitting the shelf from its clean-named sibling, unfixably from the UI
    since re-matching re-derived the same string. The composer is the last
    thing between ANY dirty source (a provider, a folder name) and a saved
    sort title, so it strips what it joins.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']


class FakeMetadata(object):
    def __init__(self):
        self.title = 'Crooked Kingdom'
        self.title_sort = None


def sort_title_for(series, volume, title='Crooked Kingdom'):
    tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
    tool.series = series
    tool.volume = volume
    tool.title = title
    tool.metadata = FakeMetadata()
    tool.force = True
    tool.set_metadata_sort_title()
    return tool.metadata.title_sort


class TestSortTitleHygiene(unittest.TestCase):
    def test_clean_series_composes_cleanly(self):
        self.assertEqual(
            sort_title_for('Six of Crows', 'Book 2'),
            'Six of Crows, Book 2 - Crooked Kingdom'
        )

    def test_trailing_space_in_series_is_stripped(self):
        self.assertEqual(
            sort_title_for('Six of Crows ', 'Book 2'),
            'Six of Crows, Book 2 - Crooked Kingdom'
        )

    def test_whitespace_around_volume_is_stripped(self):
        self.assertEqual(
            sort_title_for('Six of Crows', ' Book 2 '),
            'Six of Crows, Book 2 - Crooked Kingdom'
        )


if __name__ == '__main__':
    unittest.main()


class HyphenatedWordsSurviveSeriesStrip(unittest.TestCase):
    """
        Measured live on Harry Potter and the Half-Blood Prince (2026-07-27):
        the series-suffix pattern accepted a MID-WORD hyphen as its delimiter
        ('Half-Blood Prince, Book 6' -> stripped from the '-' on), displaying
        'Harry Potter and the Half' through two rebuilds. A dash only counts
        as a series delimiter when whitespace precedes it; colons stay valid
        unspaced, and the ', Book N' pattern still cleans the tail.
    """

    def test_the_half_blood_prince_case(self):
        self.assertEqual(
            UT.strip_trailing_series('Harry Potter and the Half-Blood Prince, Book 6'),
            'Harry Potter and the Half-Blood Prince')

    def test_spaced_dash_series_suffix_still_strips(self):
        self.assertEqual(
            UT.strip_trailing_series('The Blade Itself - The First Law, Book 1'),
            'The Blade Itself')

    def test_colon_series_suffix_still_strips(self):
        self.assertEqual(
            UT.strip_trailing_series('Wintersteel: Cradle, Book 8'),
            'Wintersteel')

    def test_hyphenated_title_without_suffix_untouched(self):
        self.assertEqual(
            UT.strip_trailing_series('The Well-Favored Man'),
            'The Well-Favored Man')
