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


if __name__ == '__main__':
    unittest.main()
