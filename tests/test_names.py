"""
Name folding and the folder-author anchor.

Every case here is a bug that reached production. The anchor decides whether a
book gets its series and volume from the folder tree at all, so when it bails
the book sorts as a bare title and falls off its own shelf -- a failure that
looks like bad metadata rather than a broken comparison, which is why it took
three separate live investigations to find.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
name_key = MODULES['search_tools'].name_key
author_name_variants = MODULES['update_tools'].author_name_variants
series_from_path_segments = MODULES['update_tools'].series_from_path_segments

ROOT = '/data/media/audiobooks-updated'


def anchor(folder_author, credited, series='Some Series', book='7 - A Book'):
    """Run the real anchor: credited author(s) vs a folder tree."""
    names = []
    for credit in credited:
        for variant in author_name_variants(credit):
            if variant not in names:
                names.append(variant)
    path = '%s/%s/%s/%s/A Book.m4b' % (ROOT, folder_author, series, book)
    return series_from_path_segments(path.split('/'), names)


class NameKey(unittest.TestCase):
    def test_punctuation_and_spacing_are_ignored(self):
        self.assertEqual(name_key('J.K. Rowling'), name_key('J. K. Rowling'))
        self.assertEqual(name_key('James S.A. Corey'), name_key('James S. A. Corey'))

    def test_accents_are_folded_not_deleted(self):
        # The old [^a-z0-9] class DELETED the accented letter, so an ASCII
        # folder never matched an accented tag and the swap-correction gate
        # silently never fired.
        self.assertEqual(name_key(u'Jos\xe9 Saramago'), 'josesaramago')
        self.assertEqual(name_key('Jose Saramago'), name_key(u'Jos\xe9 Saramago'))

    def test_multiword_non_latin_keeps_a_real_key(self):
        # THE regression: StripDiacritics is NFKD + ASCII-ignore, so a
        # multi-word non-Latin name folds to a bare SPACE -- which is truthy.
        # A bare `if folded:` therefore replaced the name with ' ' and returned
        # '', and every caller reading '' as "no name" bailed. A single-token
        # name folds to '' (falsy) and kept the original, hiding the hole.
        self.assertTrue(name_key(u'Фёдор Достоевский'))
        self.assertTrue(name_key(u'村上 春樹'))
        self.assertTrue(name_key(u'Пелевин'))

    def test_empty_and_none_are_empty(self):
        self.assertEqual(name_key(''), '')
        self.assertEqual(name_key(None), '')


class AuthorVariants(unittest.TestCase):
    def test_combined_credit_yields_each_author(self):
        self.assertEqual(
            author_name_variants('TheFirstDefier & JF Brink'),
            ['thefirstdefierjfbrink', 'thefirstdefier', 'jfbrink'],
        )

    def test_generational_suffix_is_not_an_author(self):
        # "Martin Luther King, Jr." split on the comma and offered 'jr' as a
        # one-token key that could anchor a folder literally named "Jr".
        self.assertNotIn('jr', author_name_variants('Martin Luther King, Jr.'))


class FolderAnchor(unittest.TestCase):
    def test_provider_combined_folder_single(self):
        # v1.3.105: Apple credits both authors in ONE string.
        self.assertEqual(
            anchor('TheFirstDefier', ['TheFirstDefier & JF Brink']),
            ('Some Series', '7'),
        )

    def test_folder_combined_provider_separate(self):
        # The mirror case. Widening only the provider side left this bailing.
        self.assertEqual(
            anchor('TheFirstDefier & JF Brink', ['TheFirstDefier', 'JF Brink']),
            ('Some Series', '7'),
        )
        self.assertEqual(
            anchor('Robert Jordan and Brandon Sanderson',
                   ['Robert Jordan', 'Brandon Sanderson']),
            ('Some Series', '7'),
        )

    def test_spacing_difference_still_anchors(self):
        # v1.3.106: the folder is "James S.A. Corey", the credit "James S. A.
        # Corey". One space cost The Expanse book 10 its series sort title.
        self.assertEqual(
            anchor('James S.A. Corey', ['James S. A. Corey']),
            ('Some Series', '7'),
        )

    def test_non_latin_author_anchors(self):
        self.assertEqual(
            anchor(u'Фёдор Достоевский',
                   [u'Фёдор Достоевский']),
            ('Some Series', '7'),
        )

    def test_unrelated_author_never_anchors(self):
        self.assertEqual(anchor('Someone Else', ['Brandon Sanderson']), (None, None))

    def test_suffix_fragment_never_anchors(self):
        self.assertEqual(anchor('Jr', ['Martin Luther King, Jr.']), (None, None))

    def test_unnumbered_book_folder_is_refused(self):
        # No leading number means no volume, so the layout does not match and
        # nothing is derived -- this is what keeps a standalone at
        # <Author>/<Title>/ from inheriting a fake series.
        self.assertEqual(
            anchor('TheFirstDefier', ['TheFirstDefier'], book='A Book'),
            (None, None),
        )

    def test_series_folder_equal_to_the_author_is_refused(self):
        self.assertEqual(
            anchor('TheFirstDefier', ['TheFirstDefier'], series='TheFirstDefier'),
            (None, None),
        )

    def test_shallow_path_is_refused(self):
        self.assertEqual(series_from_path_segments(['A Book.m4b'], ['someone']),
                         (None, None))


if __name__ == '__main__':
    unittest.main()
