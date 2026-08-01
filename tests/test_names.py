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
ST = MODULES['search_tools']
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


class OutboundArtistQueryName(unittest.TestCase):
    """
        ArtistSearchTool.cleanup_author_name -- the name that goes into
        `name=` on the artist lookup.

        It carried its OWN honorific list, ['Dr.', 'EdD', 'Prof.',
        'Professor'], substituted out with re.escape and NO word boundary and
        no minimum remainder. Measured before the fix:

            cleanup_author_name('Dr. Seuss')           -> ' Seuss'
            cleanup_author_name('Professor Elemental') -> ' Elemental'

        i.e. the outbound query was literally `name=%20Seuss`. Meanwhile
        COURTESY_TITLES, 260 lines below, carried a comment saying '"Dr." is
        excluded because Dr. Seuss exists' -- two lists, disagreeing, and the
        older one destroying exactly the name the newer one was protecting.
        There is one list now, and the protection lives in the stripper: at
        least two tokens must remain.
    """

    def clean(self, name):
        tool = ST.ArtistSearchTool.__new__(ST.ArtistSearchTool)
        return tool.cleanup_author_name(name)

    def test_a_two_token_pen_name_is_never_touched(self):
        for name in ('Dr. Seuss', 'Professor Elemental', 'Lord Dunsany',
                     'Mr. Men'):
            self.assertEqual(
                self.clean(name), name,
                '%r is the whole pen name; reducing it to one token loses the '
                'author entirely' % (name,))

    def test_no_result_ever_starts_with_whitespace(self):
        # The query is `name=` + this value, so a leading space is %20 in the
        # URL, not cosmetic.
        for name in ('Dr. Seuss', 'Professor Elemental', '[uk] Neil Gaiman',
                     'Sir Arthur Conan Doyle', 'Jane Doe, EdD'):
            got = self.clean(name)
            self.assertEqual(got, got.strip(), repr(name))
            self.assertTrue(got, repr(name))

    def test_a_real_leading_honorific_is_still_removed(self):
        self.assertEqual(self.clean('Sir Arthur Conan Doyle'),
                         'Arthur Conan Doyle')
        self.assertEqual(self.clean('Dr. Jordan B. Peterson'),
                         'Jordan B. Peterson')

    def test_a_trailing_credential_is_still_removed(self):
        # 'EdD' is what the old list carried it for; dropping the handling
        # silently would be a behaviour change of its own.
        self.assertEqual(self.clean('Jane Doe, EdD'), 'Jane Doe')

    def test_plain_names_are_byte_identical(self):
        for name in ('Arthur Conan Doyle', 'Ursula K. Le Guin',
                     'Brandon Sanderson', 'Agatha Christie'):
            self.assertEqual(self.clean(name), name)

    def test_the_initials_rule_is_untouched(self):
        # The one transformation this function is actually for.
        self.assertEqual(self.clean('A. E. van Vogt'), 'A E van Vogt')
        self.assertEqual(self.clean('W. E. B. Du Bois'), 'W E B Du Bois')

    def test_bracketed_text_is_still_stripped(self):
        self.assertEqual(self.clean('[uk] Neil Gaiman'), 'Neil Gaiman')

    def test_there_is_only_one_honorific_list_in_the_file(self):
        """
        The defect was two lists that DISAGREED, so behaviour alone cannot pin
        it: every test above stays green if someone re-adds a private list
        somewhere the tested names do not reach.

        AST, not a text search -- the comments that record this history quote
        the old list verbatim, and a guard that trips on its own explanation is
        a guard people delete.
        """
        import ast
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', 'search_tools.py')
        with open(path) as handle:
            tree = ast.parse(handle.read(), path)

        honorifics = set(('sir', 'dame', 'lord', 'lady', 'rev', 'reverend',
                          'father', 'sister', 'dr', 'prof', 'professor',
                          'mr', 'mrs', 'ms', 'edd', 'phd', 'md'))
        allowed = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Name)
                        and target.id in ('COURTESY_TITLES', 'POST_NOMINALS')):
                    allowed.add(node.value.lineno)

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                continue
            words = [item.value.strip().lower().rstrip('.')
                     for item in node.elts
                     if isinstance(item, ast.Constant)
                     and isinstance(item.value, str)]
            if len(words) != len(node.elts) or not words:
                continue
            if not [w for w in words if w in honorifics]:
                continue
            if node.lineno in allowed:
                continue
            offenders.append((node.lineno, words))

        self.assertEqual(len(allowed), 2,
                         'COURTESY_TITLES and POST_NOMINALS must both exist')
        self.assertEqual(
            offenders, [],
            'a SECOND honorific list is back -- that is the defect, not the '
            'symptom: %r' % (offenders,))


if __name__ == '__main__':
    unittest.main()
