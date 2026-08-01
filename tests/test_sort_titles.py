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


class ClosedUpDashesStillStrip(unittest.TestCase):
    """
        v1.3.146 stopped a mid-word hyphen being read as a series delimiter
        ("Half-Blood Prince") by requiring whitespace before the dash -- but
        that also stopped CLOSED-UP em/en dashes stripping, which is a normal
        publisher convention, so the series name stayed in the title
        ('Wintersteel—Cradle, Book 8' -> 'Wintersteel—Cradle').

        Em and en dashes never appear mid-word, so only the ASCII hyphen
        needs the whitespace requirement.
    """

    def test_closed_up_em_dash_strips(self):
        self.assertEqual(
            UT.strip_trailing_series(u'Wintersteel—Cradle, Book 8'),
            u'Wintersteel')

    def test_closed_up_en_dash_strips(self):
        self.assertEqual(
            UT.strip_trailing_series(u'Reaper–Cradle, Book 10'),
            u'Reaper')

    def test_spaced_dash_still_strips(self):
        self.assertEqual(
            UT.strip_trailing_series('Wintersteel - Cradle, Book 8'),
            'Wintersteel')

    def test_mid_word_hyphen_is_still_not_a_delimiter(self):
        self.assertEqual(
            UT.strip_trailing_series(
                'Harry Potter and the Half-Blood Prince, Book 6'),
            'Harry Potter and the Half-Blood Prince')

    def test_closed_up_ascii_hyphen_is_still_not_a_delimiter(self):
        # "The Blade Itself-The First Law" is indistinguishable from a
        # hyphenated word without the space, so it keeps the conservative
        # behaviour; only the ", Book N" tail comes off.
        self.assertEqual(
            UT.strip_trailing_series(
                'The Blade Itself-The First Law, Book 1'),
            'The Blade Itself-The First Law')


class TypographicApostropheSplitsAShelf(unittest.TestCase):
    """
    A curly apostrophe in a series name is the "Six of Crows " bug in disguise.

    Measured live 2026-07-29 on Margaret Atwood: the API returns the series as
    u'The Handmaid’s Tale' (RIGHT SINGLE QUOTATION MARK) while both book
    titles spell the possessive with an ASCII apostrophe, so the saved sort
    title read "Handmaid’s Tale, Book 1 - The Handmaid's Tale" -- the same
    string, punctuated two ways, inside one field.

    Cosmetic in that one case only because BOTH Atwood books happened to carry
    the curly form. The moment one book of a series arrives with the straight
    form -- a different provider, a re-scrape, a librarian edit -- the two sort
    prefixes differ and the shelf splits, unfixably from the UI, exactly as the
    trailing space did. Normalising at the composer (the last thing between a
    dirty source and a saved sort title) makes the prefix depend on the words
    rather than on which quote character the source happened to use.

    ASCII is the target: it sorts predictably and matches how the titles spell
    it.
    """

    CURLY = u'The Handmaid’s Tale'
    STRAIGHT = u"The Handmaid's Tale"

    def test_curly_series_composes_with_a_straight_apostrophe(self):
        self.assertEqual(
            sort_title_for(self.CURLY, 'Book 1', title=u"The Handmaid's Tale"),
            u"Handmaid's Tale, Book 1 - The Handmaid's Tale"
        )

    def test_curly_and_straight_sources_compose_IDENTICALLY(self):
        # THE point: whichever form the provider sends, one shelf.
        curly = sort_title_for(self.CURLY, 'Book 2', title=u'The Testaments')
        straight = sort_title_for(self.STRAIGHT, 'Book 2', title=u'The Testaments')
        self.assertEqual(curly, straight)
        self.assertEqual(straight, u"Handmaid's Tale, Book 2 - The Testaments")

    def test_the_title_half_is_normalised_too(self):
        # Otherwise one field still holds the same word punctuated two ways.
        self.assertEqual(
            sort_title_for(u'Sundering', 'Book 3', title=u'The Adversary’s Tale'),
            u"Sundering, Book 3 - The Adversary's Tale"
        )

    def test_other_typographic_apostrophes_fold_as_well(self):
        for mark in (u'‘', u'ʼ', u'′'):
            self.assertEqual(
                sort_title_for(u'Handmaid' + mark + u's Tale', 'Book 1',
                               title=u'The Testaments'),
                u"Handmaid's Tale, Book 1 - The Testaments"
            )

    def test_plain_ascii_is_untouched(self):
        self.assertEqual(
            sort_title_for('Six of Crows', 'Book 2'),
            'Six of Crows, Book 2 - Crooked Kingdom'
        )

    def test_a_real_quotation_mark_is_not_an_apostrophe(self):
        # Double quotes are a different character class; leave them alone.
        self.assertEqual(
            sort_title_for(u'Sundering', 'Book 3', title=u'The “Adversary”'),
            u'Sundering, Book 3 - The “Adversary”'
        )

# LAST STATEMENT IN THE FILE, always. This guard used to sit mid-file, and
# `python3 tests/<file>.py` then ran only the classes DEFINED ABOVE IT and
# printed OK -- measured: test_scoring ran 8 of 16, test_cache_times 4 of 20,
# test_sort_titles 3 of 18. Discovery was unaffected, so the suite stayed
# honest while a direct run (how a single fix gets checked) silently skipped
# the new tests. tests/test_deploy_gate.py pins the position for every file.
if __name__ == '__main__':
    unittest.main()
