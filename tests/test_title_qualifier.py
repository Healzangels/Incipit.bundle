"""
A trailing edition qualifier is stripped WITH its separator.

WHY THIS EXISTS
    Audible titles the Narnia readings "The Horse and His Boy: Unabridged".
    simplify_title stripped " Unabridged" but not the colon before it, so three
    albums sat on the shelf as "The Horse and His Boy:", "The Voyage of the Dawn
    Treader:" and "The Silver Chair:" -- a dangling colon, beside siblings with
    clean titles. Measured live 2026-08-11: exactly 3 albums, all Narnia.

    The strip is anchored to the END of the title, which is what keeps it from
    eating real words: "The Unabridged Journals of Sylvia Plath" and "Abridged
    Too Far" are legitimate titles and must survive untouched.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']


def simplified(title):
    tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
    tool.title = title
    return tool.simplify_title()


class TestTrailingQualifier(unittest.TestCase):
    def test_colon_goes_with_the_qualifier(self):
        # The three live cases.
        self.assertEqual(simplified('The Horse and His Boy: Unabridged'), 'The Horse and His Boy')
        self.assertEqual(
            simplified('The Voyage of the Dawn Treader: Unabridged'), 'The Voyage of the Dawn Treader'
        )
        self.assertEqual(simplified('The Silver Chair: Unabridged'), 'The Silver Chair')

    def test_abridged_too(self):
        self.assertEqual(simplified(u'The Magician’s Nephew: Abridged'), u'The Magician’s Nephew')

    def test_dash_and_comma_separators(self):
        self.assertEqual(simplified('Some Title - Abridged'), 'Some Title')
        self.assertEqual(simplified('Some Title, Unabridged'), 'Some Title')

    def test_the_existing_shapes_still_work(self):
        # Parenthesised and bare forms were already handled; they must stay so.
        self.assertEqual(simplified('Project Hail Mary (Unabridged)'), 'Project Hail Mary')
        self.assertEqual(simplified('Some Book Unabridged'), 'Some Book')

    def test_a_real_word_at_the_END_is_not_eaten(self):
        # Anchored to the end, so a qualifier-looking word elsewhere survives.
        self.assertEqual(
            simplified('The Unabridged Journals of Sylvia Plath'),
            'The Unabridged Journals of Sylvia Plath',
        )
        self.assertEqual(simplified('Abridged Too Far'), 'Abridged Too Far')

    def test_a_clean_title_is_untouched(self):
        self.assertEqual(simplified('Prince Caspian'), 'Prince Caspian')

    def test_no_title_left_behind_as_a_bare_separator(self):
        # Degenerate input must not yield ':' or '-' as the album title.
        self.assertEqual(simplified(': Unabridged'), '')



class TestTheSortTitleGetsItToo(unittest.TestCase):
    """
    The SORT title is the shelving key, and it had its own copy of the bug.

    v1.3.200 cleaned the DISPLAY title but set_metadata_sort_title composed from
    the raw self.title, so after deploying it the album page read "The Horse and
    His Boy" while the shelf still read "Book 5 - The Horse and His Boy:
    Unabridged" -- beside siblings that had no qualifier. Verified live on all
    three Narnia readings.
    """

    def album(self, title, series=None, volume=None, existing_sort=None):
        tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
        tool.title = title
        tool.series = series
        tool.volume = volume
        tool.force = True

        class MD(object):
            pass

        md = MD()
        md.title = title
        md.title_sort = existing_sort
        tool.metadata = md
        tool.set_metadata_sort_title()
        return md.title_sort

    def test_the_qualifier_is_stripped_from_the_shelving_key(self):
        self.assertEqual(
            self.album('The Horse and His Boy: Unabridged', 'The Chronicles of Narnia', 'Book 5'),
            'Chronicles of Narnia, Book 5 - The Horse and His Boy',
        )

    def test_siblings_compose_identically(self):
        # The whole point: the three readings must key the same way as the four
        # that never carried a qualifier.
        a = self.album('The Silver Chair: Unabridged', 'The Chronicles of Narnia', 'Book 4')
        b = self.album('Prince Caspian', 'The Chronicles of Narnia', 'Book 2')
        self.assertEqual(a, 'Chronicles of Narnia, Book 4 - The Silver Chair')
        self.assertEqual(b, 'Chronicles of Narnia, Book 2 - Prince Caspian')

    def test_a_trailing_volume_in_the_title_is_not_repeated(self):
        # The volume is already its own component.
        self.assertEqual(
            self.album('Some Title, Book 3', 'Some Series', 'Book 3'),
            'Some Series, Book 3 - Some Title',
        )

    def test_a_clean_title_composes_unchanged(self):
        self.assertEqual(
            self.album('The Last Battle', 'The Chronicles of Narnia', 'Book 7'),
            'Chronicles of Narnia, Book 7 - The Last Battle',
        )

if __name__ == '__main__':
    unittest.main()
