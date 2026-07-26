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
