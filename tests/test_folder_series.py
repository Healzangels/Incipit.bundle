"""
Whose series wins: the provider's, or the library's folder tree.

The provider is not consistent across a series. Katherine Addison's Chronicles
of Osreth came back under TWO names from the same source -- "Chronicles of
Osreth" for books 1 and 3, "Chronicles of Osreth: The Cemeteries of Amalo
Trilogy" for the two in between -- with the sub-series restarting at Book 1, so
the shelf had two "Book 1"s and no coherent order. The folder tree had them
right: 1, 1.1, 2, 3, 4.

The blanket `series_from_folder_wins` pref cannot be the answer, because folder
quality varies WITHIN one library: Glen Cook's twelve books sort correctly today
precisely BECAUSE the provider wins -- his folders carry three different series
names and reuse numbers (two "1 -", two "2 -"). Turning it on globally would fix
Addison by fragmenting Cook.

Hence a per-author list, the same shape as the existing
`authors_prefer_hardcover` pref.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']

ROOT = '/data/media/audiobooks-updated'


class FakeMedia(object):
    def __init__(self, path):
        self.filename = None          # UPDATE media has no filename
        self.tracks = None
        self.children = None
        self._path = path


def tool_for(path, author, series, volume, title, prefs=None):
    """An AlbumUpdateTool wired to one file path, with no Plex behind it."""
    tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
    tool.prefs = dict(plexenv.FakePrefs.DEFAULTS)
    tool.prefs.update(prefs or {})
    tool.media = FakeMedia(path)
    tool.author = [{'name': author}]
    tool.series, tool.volume, tool.title = series, volume, title
    # __init__ sets this on every real tool; __new__ skips it.
    tool.series_span = False
    tool.album_file_path = lambda: path
    return tool


def derive(**kw):
    tool = tool_for(**kw)
    tool.derive_series_from_path()
    return (tool.series, tool.volume)


def derive_tool(**kw):
    """The tool itself, for assertions beyond the (series, volume) pair."""
    tool = tool_for(**kw)
    tool.derive_series_from_path()
    return tool


OSRETH = ROOT + '/Katherine Addison/The Chronicles of Osreth'


class FolderWinsPerAuthor(unittest.TestCase):
    """The listed author's folder overrides a provider that supplied both halves."""

    LISTED = {'series_from_folder_authors': 'Katherine Addison'}

    def test_a_sub_series_name_collapses_to_the_folder(self):
        # "The Witness for the Dead" is Book 1 of the Cemeteries of Amalo
        # sub-series and book 2 of the shelf. Unlisted, the provider wins and it
        # collides with The Goblin Emperor's Book 1.
        self.assertEqual(
            derive(path=OSRETH + '/2 - The Witness for the Dead/w.m4b',
                   author='Katherine Addison',
                   series='Chronicles of Osreth: The Cemeteries of Amalo Trilogy',
                   volume='Book 1', title='The Witness for the Dead',
                   prefs=self.LISTED),
            ('The Chronicles of Osreth', 'Book 2'))

    def test_a_wrong_number_under_the_right_name_is_corrected(self):
        # Names AGREE here; only the position is wrong (provider counts the
        # sub-series, the folder counts the shelf).
        self.assertEqual(
            derive(path=OSRETH + '/4 - The Tomb of Dragons/t.m4b',
                   author='Katherine Addison', series='Chronicles of Osreth',
                   volume='Book 3', title='The Tomb of Dragons',
                   prefs=self.LISTED),
            ('The Chronicles of Osreth', 'Book 4'))

    def test_an_already_correct_book_is_unchanged(self):
        self.assertEqual(
            derive(path=OSRETH + '/1 - The Goblin Emperor/g.m4b',
                   author='Katherine Addison', series='Chronicles of Osreth',
                   volume='Book 1', title='The Goblin Emperor',
                   prefs=self.LISTED),
            ('The Chronicles of Osreth', 'Book 1'))

    def test_a_decimal_folder_number_survives(self):
        self.assertEqual(
            derive(path=OSRETH + '/1.1 - The Orb of Cairado/o.m4b',
                   author='Katherine Addison', series='Chronicles of Osreth',
                   volume='Book 1.1', title='The Orb of Cairado',
                   prefs=self.LISTED)[1],
            'Book 1.1')

    def test_matching_is_spacing_and_accent_insensitive(self):
        for spelling in ('katherine addison', 'Katherine  Addison'):
            self.assertEqual(
                derive(path=OSRETH + '/4 - The Tomb of Dragons/t.m4b',
                       author='Katherine Addison', series='Chronicles of Osreth',
                       volume='Book 3', title='The Tomb of Dragons',
                       prefs={'series_from_folder_authors': spelling})[1],
                'Book 4', spelling)

    def test_one_listed_author_among_several(self):
        self.assertEqual(
            derive(path=OSRETH + '/4 - The Tomb of Dragons/t.m4b',
                   author='Katherine Addison', series='Chronicles of Osreth',
                   volume='Book 3', title='The Tomb of Dragons',
                   prefs={'series_from_folder_authors':
                          'Craig Alanson, Katherine Addison , Robert Jordan'})[1],
            'Book 4')


class UnlistedAuthorsAreUntouched(unittest.TestCase):
    """The whole point of scoping it: Glen Cook must not change."""

    COOK = ROOT + '/Glen Cook/The Black Company'

    def test_provider_still_wins_for_an_unlisted_author(self):
        # His folders reuse numbers ("1 - Shadow Games" AND "1 - The Black
        # Company"), so folder-wins would put two books at Book 1. The provider's
        # coherent 1-11 must survive.
        self.assertEqual(
            derive(path=self.COOK + '/1 - Shadow Games/s.m4b', author='Glen Cook',
                   series='Chronicles of the Black Company', volume='Book 4',
                   title='Shadow Games',
                   prefs={'series_from_folder_authors': 'Katherine Addison'}),
            ('Chronicles of the Black Company', 'Book 4'))

    def test_an_empty_list_changes_nothing(self):
        self.assertEqual(
            derive(path=self.COOK + '/1 - Shadow Games/s.m4b', author='Glen Cook',
                   series='Chronicles of the Black Company', volume='Book 4',
                   title='Shadow Games', prefs={'series_from_folder_authors': ''}),
            ('Chronicles of the Black Company', 'Book 4'))

    def test_a_near_miss_name_does_not_match(self):
        self.assertEqual(
            derive(path=self.COOK + '/1 - Shadow Games/s.m4b', author='Glen Cook',
                   series='Chronicles of the Black Company', volume='Book 4',
                   title='Shadow Games',
                   prefs={'series_from_folder_authors': 'Glenda Cook'})[1],
            'Book 4')


class ExistingBehaviourStillHolds(unittest.TestCase):
    """The paths that already worked must not regress."""

    def test_global_pref_still_works(self):
        self.assertEqual(
            derive(path=OSRETH + '/4 - The Tomb of Dragons/t.m4b',
                   author='Katherine Addison', series='Chronicles of Osreth',
                   volume='Book 3', title='The Tomb of Dragons',
                   prefs={'series_from_folder_wins': True})[1],
            'Book 4')

    def test_provider_series_with_no_position_takes_the_folder_pair(self):
        # v1.3.106: "Arcanum Unbounded" is credited to The Mistborn Saga with no
        # position while the library files it under The Cosmere.
        self.assertEqual(
            derive(path=ROOT + '/Brandon Sanderson/The Cosmere/18 - Arcanum Unbounded/a.m4b',
                   author='Brandon Sanderson', series='The Mistborn Saga', volume='',
                   title='Arcanum Unbounded'),
            ('The Cosmere', 'Book 18'))

    def test_no_provider_series_takes_the_folder(self):
        self.assertEqual(
            derive(path=ROOT + '/TheFirstDefier/Defiance of the Fall/10 - Book Ten/d.m4b',
                   author='TheFirstDefier', series='', volume='',
                   title='Defiance of the Fall, Book 10'),
            ('Defiance of the Fall', 'Book 10'))

    def test_a_standalone_gets_nothing(self):
        self.assertEqual(
            derive(path=ROOT + '/Andy Weir/Artemis/a.m4b', author='Andy Weir',
                   series='', volume='', title='Artemis'),
            ('', ''))


class RangeFolderSpan(unittest.TestCase):
    """
    WIRING: a range folder must SET the span flag, not merely withhold a number.

    The composer's half and the parser's half were each tested on their own, and
    a mutation that stopped derive_series_from_path setting the flag left the
    whole suite green -- the two halves are one feature, and only an end-to-end
    assertion holds them together.
    """

    def test_a_range_folder_sets_the_span_flag(self):
        tool = derive_tool(
            path=ROOT + '/Michael Scott/Secrets of the Immortal Nicholas Flamel' + '/1-9 - The Lost Stories Collection/x.m4b',
            author='Michael Scott', series='', volume='',
            title='The Lost Stories Collection')
        self.assertEqual(tool.series, 'Secrets of the Immortal Nicholas Flamel')
        self.assertEqual(tool.volume, '')
        self.assertTrue(tool.series_span,
                        'a range folder must mark the book as a SPAN')

    def test_a_numbered_folder_does_NOT_set_it(self):
        tool = derive_tool(
            path=ROOT + '/Michael Scott/Secrets of the Immortal Nicholas Flamel' + '/1 - The Alchemyst/x.m4b',
            author='Michael Scott', series='', volume='', title='The Alchemyst')
        self.assertEqual(tool.volume, 'Book 1')
        self.assertFalse(tool.series_span,
                         'a normal numbered book is not a span')

    def test_the_span_composes_a_grouped_sort_title(self):
        # End to end: folder -> flag -> sort title, the whole point of the pair.
        tool = derive_tool(
            path=ROOT + '/Michael Scott/Secrets of the Immortal Nicholas Flamel' + '/1-9 - The Lost Stories Collection/x.m4b',
            author='Michael Scott', series='', volume='',
            title='The Lost Stories Collection')
        tool.force = True
        tool.metadata = type('M', (), {'title': 'The Lost Stories Collection',
                                       'title_sort': ''})()
        tool.set_metadata_sort_title()
        self.assertEqual(
            tool.metadata.title_sort,
            'Secrets of the Immortal Nicholas Flamel - The Lost Stories Collection')

if __name__ == '__main__':
    unittest.main()
