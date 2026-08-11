"""
A "Series:" mood the record no longer claims must be RETIRED.

WHY THIS EXISTS
    Purely additive moods made a wrong shelf permanent. `metadata.moods` is
    restored from the AGENT'S OWN persisted model -- the agent registers
    persist_stored_files -- so removing the tag from the Plex library is undone
    the next time update() runs.

    Measured live 2026-08-11: 53 albums carried a translated shelf ("Series:
    Kolekcja Swiat Dysku", "Series: Der grosse Bruderkrieg", "Series: Les
    Annales de la Compagnie Noire"). Clearing them through Plex's bulk-edit API
    worked -- verified zero remaining -- and a single refresh brought all of
    them back, while /books was returning the correct sub-series. No external
    pass can fix this; only the writer can.

    The field is SHARED with author moods, and the retire must never cost them,
    nor may a record with no series wipe a shelf it cannot replace.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']


class FakeMoods(object):
    """The framework's mood set, as far as this code uses it: iterate, add,
       clear. Deliberately NOT a plain set -- ordering makes the assertions
       readable and iteration is the operation the real object must support."""

    def __init__(self, items=()):
        self.items = list(items)

    def __iter__(self):
        return iter(list(self.items))

    def add(self, value):
        if value not in self.items:
            self.items.append(value)

    def clear(self):
        self.items = []


class FakeMetadata(object):
    def __init__(self, moods=()):
        self.moods = FakeMoods(moods)


class Helper(object):
    def __init__(self, moods=(), series=None, series2=None):
        self.metadata = FakeMetadata(moods)
        self.series = series
        self.series2 = series2


def tagger(moods=(), series=None, series2=None):
    helper = Helper(moods, series, series2)
    tool = UT.TagTool(helper, {})
    return tool, helper


class TestStaleSeriesMoodIsRetired(unittest.TestCase):
    def test_a_series_no_longer_claimed_is_removed(self):
        # The exact live case: Discworld book that used to carry the Polish
        # custom-order listing and now resolves to its real sub-arc.
        tool, helper = tagger(
            moods=['Terry Pratchett', 'Series: Discworld', 'Series: Kolekcja Swiat Dysku'],
            series='Discworld',
            series2='Discworld - Witches',
        )
        tool.add_series_to_moods()
        self.assertEqual(
            sorted(helper.metadata.moods.items),
            ['Series: Discworld', 'Series: Discworld - Witches', 'Terry Pratchett'],
        )

    def test_author_moods_survive_the_retire(self):
        # moods is a flat field shared with authors; retiring a shelf must not
        # cost them. This is what makes clear()+re-add correct rather than lazy.
        tool, helper = tagger(
            moods=['Dan Abnett', 'Some Other Author', 'Series: Der grosse Bruderkrieg'],
            series='The Horus Heresy',
        )
        tool.add_series_to_moods()
        self.assertIn('Dan Abnett', helper.metadata.moods.items)
        self.assertIn('Some Other Author', helper.metadata.moods.items)
        self.assertNotIn('Series: Der grosse Bruderkrieg', helper.metadata.moods.items)

    def test_a_record_with_NO_series_wipes_nothing(self):
        # The sparse-record guard. A provider that simply lacks a series must
        # not be able to unshelve a book -- same rule as add_genres.
        tool, helper = tagger(
            moods=['Terry Pratchett', 'Series: Discworld'],
            series=None,
            series2=None,
        )
        tool.add_series_to_moods()
        self.assertEqual(
            sorted(helper.metadata.moods.items), ['Series: Discworld', 'Terry Pratchett']
        )

    def test_no_rewrite_when_nothing_is_stale(self):
        # A rewrite is logged by Plex as "something changed" and costs a
        # per-track tags write, so the steady state must not churn.
        tool, helper = tagger(
            moods=['Terry Pratchett', 'Series: Discworld'], series='Discworld'
        )
        moods = helper.metadata.moods
        cleared = []
        moods.clear = lambda: cleared.append(True)
        tool.add_series_to_moods()
        self.assertEqual(cleared, [])
        self.assertEqual(sorted(moods.items), ['Series: Discworld', 'Terry Pratchett'])

    def test_both_series_are_kept(self):
        tool, helper = tagger(
            moods=['Series: Stale One'], series='Primary Shelf', series2='Sub Arc'
        )
        tool.add_series_to_moods()
        self.assertEqual(
            sorted(helper.metadata.moods.items), ['Series: Primary Shelf', 'Series: Sub Arc']
        )

    def test_unreadable_moods_still_shelve_the_book(self):
        # The retire is cosmetic; the shelf is not. If the set cannot be read,
        # fall back to adding rather than raising out of update().
        tool, helper = tagger(series='Primary Shelf')

        class Unreadable(FakeMoods):
            def __iter__(self):
                raise RuntimeError('boom')

        helper.metadata.moods = Unreadable(['Series: Stale'])
        tool.add_series_to_moods()
        self.assertIn('Series: Primary Shelf', helper.metadata.moods.items)


if __name__ == '__main__':
    unittest.main()
