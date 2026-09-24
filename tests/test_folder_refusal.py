"""
The folder fallback must not turn a folder that states no series into a shelf.

Chaptarr files every book as <Author>/<Series>/<NN - Title>, inventing a series
folder even for a book that belongs to none. When neither the provider nor
Goodreads names a series, the fallback used to trust that folder, and prod
carried one-book shelves like "Stand, Book 1 - The Stand" and "Insomnia
Split-Volume, Book 1 - Insomnia". Every case below is a real folder from the
prod library, 2026-09-23, unless it says otherwise.

folder_series_refusal is pure: the caller reads the sibling book folders
(Core.storage, verified on the test box) and passes them in, or passes None when
they cannot be read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
refuse = MODULES['update_tools'].folder_series_refusal
folder_book_number = MODULES['update_tools'].folder_book_number


class EditionMarkerFolders(unittest.TestCase):
    """Rule 1: an edition marker in the folder NAME -- no listing needed."""

    def test_split_volume_folders_are_refused(self):
        for folder, book in (('Insomnia Split-Volume', '1 - Insomnia'),
                             ('Needful Things (Split-Volume)', '1 - Needful Things'),
                             ('Duma Key Split-Volume', '2 - Duma Key')):
            self.assertIsNotNone(refuse(folder, book, []), folder)

    def test_an_edition_folder_is_refused(self):
        # George Orwell/The English Edition/3 - 1984 -> "English Edition, Book 3".
        self.assertIsNotNone(refuse('The English Edition', '3 - 1984', []))

    def test_the_marker_applies_even_when_the_folder_cannot_be_listed(self):
        # None siblings = the listing failed; the NAME rule still holds.
        self.assertIsNotNone(refuse('Insomnia Split-Volume', '1 - Insomnia', None))

    def test_a_real_series_is_not_mistaken_for_an_edition(self):
        # Word-bounded: these name series, not editions.
        self.assertIsNone(refuse('Order of the Centurion', '1 - Order of the Centurion', None))
        self.assertIsNone(refuse('The Expeditionary Force', '3 - Paradise', None))


class OneBookFolderNamedAfterTheBook(unittest.TestCase):
    """Rule 2: alone, numbered 1, and named after the book itself."""

    def test_standalones_in_their_own_folder_are_refused(self):
        for folder, book in (('The Stand', '1 - The Stand'),
                             ('The Green Mile', '1 - The Green Mile'),
                             ('Danse macabre', '1 - Danse Macabre'),
                             ('Stranger in a Strange Land', '1 - Stranger in a Strange Land')):
            self.assertIsNotNone(refuse(folder, book, []), folder)

    def test_a_folder_name_contained_in_the_title_is_refused(self):
        # Stephen King/Sunset/1 - Just After Sunset -> "Sunset, Book 1".
        self.assertIsNotNone(refuse('Sunset', '1 - Just After Sunset', []))
        # An omnibus of a trilogy alone in the trilogy's folder.
        self.assertIsNotNone(refuse('Society of the Sword', '1 - The Society of the Sword Trilogy', []))

    def test_a_two_letter_folder_is_not_contained_in_any_title_that_spells_it(self):
        # Invented guard case: the letters "it" really are inside "The Witcher"
        # (w-I-T-cher) -- the first draft of this test used "Twilight", which does
        # NOT contain them, so it passed with the guard deleted. A short folder
        # name is not a series name just because a title happens to spell it.
        self.assertIn('it', 'thewitcher')
        self.assertIsNone(refuse('It', '1 - The Witcher', []))

    def test_the_cosmere_keeps_arcanum_unbounded(self):
        # The fallback's reason to exist: the OPERATOR filing a book no provider
        # can place. A one-book folder with a DIFFERENT name is not touched.
        self.assertIsNone(refuse('The Cosmere', '18 - Arcanum Unbounded', []))

    def test_a_lone_book_past_number_1_is_left_alone(self):
        # Owning only book 3 of a series is not a standalone.
        self.assertIsNone(refuse('Dune', '3 - Children of Dune', []))

    def test_a_first_book_with_siblings_keeps_its_series(self):
        # Defiance of the Fall: every book carries the series title; an Apple
        # match has no provider series, so the folder is its ONLY source.
        self.assertIsNone(refuse('Defiance of the Fall', '1 - Defiance of the Fall',
                                 ['2 - Defiance of the Fall', '10 - Defiance of the Fall']))

    def test_an_unreadable_folder_changes_nothing(self):
        self.assertIsNone(refuse('The Stand', '1 - The Stand', None))

    def test_a_misfiled_book_is_out_of_reach_by_design(self):
        # Cormac McCarthy/Katie Kazoo, Switcheroo/31 - Child of God. No name
        # rule can tell this from a real one-book folder; it is a folder to move.
        self.assertIsNone(refuse('Katie Kazoo, Switcheroo', '31 - Child of God', []))


class EveryBookOnTheSameNumber(unittest.TestCase):
    """Rule 3: all books in the folder share one number -- a default, not a position."""

    def test_a_collection_and_its_own_novella_both_at_1_are_refused(self):
        self.assertIsNotNone(refuse('Different Seasons', '1 - Apt Pupil', ['1 - Different Seasons']))
        self.assertIsNotNone(refuse('Different Seasons', '1 - Different Seasons', ['1 - Apt Pupil']))
        self.assertIsNotNone(refuse('Skeleton Crew', '1 - The Mist', ['1 - Skeleton Crew']))

    def test_01_and_1_are_the_same_number(self):
        self.assertEqual(folder_book_number('01 - A'), folder_book_number('1 - B'))
        self.assertIsNotNone(refuse('Collection', '01 - Story', ['1 - Collection']))

    def test_a_real_series_with_wrong_numbers_is_still_left_alone(self):
        # The Discworld folder numbers by sub-arc and collides at several
        # positions; the numbers DIFFER across the folder, so it is not a default.
        self.assertIsNone(refuse('Discworld', '14 - Lords and Ladies',
                                 ['1 - The Colour of Magic', '14 - Interesting Times', '2 - The Light Fantastic']))

    def test_a_range_folder_is_a_real_grouping(self):
        self.assertIsNone(refuse('Sovereign of the Seven Isles', '1 - A Warrior Made',
                                 ['1-3 - The Sovereign of the Seven Isles']))


if __name__ == '__main__':
    unittest.main()
