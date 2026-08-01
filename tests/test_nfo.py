"""
NFO sidecars: the ripper's own description of the file.

WHY THIS EXISTS
    Chaptarr writes the metadata.json sidecar from ITS match. The .nfo comes
    from the source/provider that produced the file, so where the two disagree
    the NFO is the better witness -- and a metadata.json can be
    SELF-CONSISTENTLY wrong (a whole sidecar generated for the wrong edition,
    which no field cross-check can catch).

    Measured 2026-07-31 across the operator's 259 nfo files: 186 (72%) use one
    consistent three-section layout, and of those ~180 carry Title, Author,
    Read By, Duration, Chapters and Publisher.

    DURATION is the field worth the most. It is file-accurate -- checked against
    real Plex track sums, 7 of 8 books landed within 0.12% and most within 0.04%
    -- and the duration veto, the primary wrong-edition guard, is otherwise
    DORMANT until Plex analyses a file.

    The eighth book is why duration precedence runs the other way from every
    other field: "Small Gods" reads 11.98h in the nfo and its library file is
    50 SECONDS. The nfo describes the SOURCE; only the file describes the file.
    So Plex's own measurement wins where it exists, and the nfo fills the gap.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']


# A real file, verbatim (Terry Pratchett - Wyrd Sisters). Kept whole rather than
# trimmed to a happy path: the alignment, the blank lines and the trailing
# description are all things the parser has to survive.
WYRD = """General Information
===================
 Title:                  Wyrd Sisters: Discworld, Book 6
 Author:                 Terry Pratchett
 Read By:                Indira Varma, Peter Serafinowicz, Bill Nighy
 Copyright:              2022
 Audiobook Copyright:    2022
 Genre:                  Audiobook
 Publisher:              Penguin Audio
 Duration:               9 hours, 53 minutes, 19 seconds
 Chapters:               96


Media Information
=================
 Source Format:          Audible AAX
 Source Sample Rate:     44100 Hz

 Ripper:                 inAudible 1.97


Book Description
================
Three witches gathered on a lonely heath.
"""

# The other real shape: multi-author with a ROLE annotation, a marketplace
# line, an Unabridged flag, and an omnibus note.
SHERLOCK = """General Information
===================
 Title:                  Sherlock Holmes - The Definitive Collection
 Author:                 Arthur Conan Doyle, Stephen Fry (introductions)
 Read By:                Stephen Fry
 Audible.co.uk Release:  February 27th, 2017
 Original Publication:   2017
 Genre:                  Classical Crime Mystery
 Publisher:              Audible Studios
 Duration:               71 hours, 57 minutes, 52 seconds
 Chapters:               141
 Unabridged:             Yes

 Note(s):                Omnibus of books 1-9 of Sherlock Holmes:

                         1. A Study in Scarlet (1887).
                         2. The Sign of Four (1890).
"""

# WYRD with two traps, both field-shaped by NFO_FIELD so ONLY the section
# truncation keeps them out -- and `raw` is last-write-wins, so a later section
# does not merely add keys, it OVERWRITES the book's real values:
#   * a Duration in Media Information (encoder trivia, a different number)
#   * a Title inside the Book Description prose
WYRD_WITH_TRAPS = (
    WYRD
    .replace(' Source Format:          Audible AAX',
             ' Duration:               99 hours, 1 minute, 1 second\n'
             ' Source Format:          Audible AAX')
    .replace('Three witches gathered on a lonely heath.',
             ' Title:                  NOT THE BOOK TITLE\n'
             ' Publisher:              Some Blurb Publisher\n'
             'Three witches gathered on a lonely heath.')
)

# The same file with a non-ASCII credit. In py2 this arrives from
# Core.storage.load as BYTES; parse_nfo has to hand back unicode, like the
# metadata.json sidecar it claims to mimic.
ACCENTED = WYRD.replace('Terry Pratchett', u'Antoine de Saint-Exupéry')


class TestParseNfo(unittest.TestCase):
    def test_returns_none_for_something_that_is_not_an_nfo(self):
        for junk in (None, '', 'just some text', '<?xml version="1.0"?><movie/>'):
            self.assertIsNone(ST.parse_nfo(junk))

    def test_core_fields(self):
        d = ST.parse_nfo(WYRD)
        self.assertEqual(d['title'], 'Wyrd Sisters: Discworld, Book 6')
        self.assertEqual(d['authors'], ['Terry Pratchett'])
        self.assertEqual(
            d['narrators'], ['Indira Varma', 'Peter Serafinowicz', 'Bill Nighy'])
        self.assertEqual(d['publisher'], 'Penguin Audio')

    def test_duration_is_milliseconds(self):
        # The query hint and the Plex probe are both ms; converting here keeps
        # one unit through the whole path.
        d = ST.parse_nfo(WYRD)
        self.assertEqual(d['duration'], (9 * 3600 + 53 * 60 + 19) * 1000)

    def test_duration_tolerates_missing_components(self):
        self.assertEqual(
            ST.parse_nfo(WYRD.replace('9 hours, 53 minutes, 19 seconds',
                                      '11 hours, 30 minutes'))['duration'],
            (11 * 3600 + 30 * 60) * 1000)
        self.assertEqual(
            ST.parse_nfo(WYRD.replace('9 hours, 53 minutes, 19 seconds',
                                      '47 minutes, 5 seconds'))['duration'],
            (47 * 60 + 5) * 1000)

    def test_a_role_annotation_is_not_a_second_author(self):
        # "Stephen Fry (introductions)" is a CREDIT, not a co-author. Sending it
        # as one skews the author score against the real match.
        d = ST.parse_nfo(SHERLOCK)
        self.assertEqual(d['authors'], ['Arthur Conan Doyle'])

    def test_marketplace_becomes_a_region(self):
        self.assertEqual(ST.parse_nfo(SHERLOCK)['region'], 'uk')
        self.assertEqual(
            ST.parse_nfo(SHERLOCK.replace('Audible.co.uk', 'Audible.com'))['region'], 'us')

    def test_unabridged_flag(self):
        self.assertIs(ST.parse_nfo(SHERLOCK)['abridged'], False)
        self.assertIs(ST.parse_nfo(SHERLOCK.replace('Unabridged:             Yes',
                                                    'Unabridged:             No'))['abridged'], True)
        # Absent means UNKNOWN, not "unabridged" -- most files simply omit it.
        self.assertIsNone(ST.parse_nfo(WYRD).get('abridged'))

    def test_a_junk_genre_is_dropped(self):
        # 'Audiobook' as a genre is noise; a real one is kept.
        self.assertIsNone(ST.parse_nfo(WYRD).get('genre'))
        self.assertEqual(ST.parse_nfo(SHERLOCK)['genre'], 'Classical Crime Mystery')

    def test_the_media_information_section_cannot_overwrite_the_duration(self):
        """
        THE truncation test. Asserting over parse_nfo's KEYS proves nothing --
        it only ever writes a fixed whitelist (title|authors|narrators|
        publisher|duration|genre|abridged|region), so `for key in d` iterates a
        constant set that can never contain 'ripper' or a space. Measured:
        disabling the truncation (`if len(marks) > 1:` -> `if False and ...`)
        left all 15 tests green.

        The mutation DOES change values, which is the sharper danger: `raw` is
        last-write-wins, so a Media Information "Duration:" line silently
        REPLACES the book's real one -- and duration is the field this whole
        parser exists for (it is the primary wrong-edition guard, dormant until
        Plex analyses a file).
        """
        real = (9 * 3600 + 53 * 60 + 19) * 1000
        self.assertEqual(ST.parse_nfo(WYRD)['duration'], real)
        self.assertNotEqual(real, (99 * 3600 + 61) * 1000, 'trap must differ')
        self.assertEqual(
            ST.parse_nfo(WYRD_WITH_TRAPS)['duration'], real,
            'the encoder section\'s Duration overwrote the book\'s')

    def test_the_description_section_cannot_overwrite_the_title(self):
        # Book Description prose is full of colons, and a field-shaped line in
        # it lands on the SAME key as the real one.
        d = ST.parse_nfo(WYRD_WITH_TRAPS)
        self.assertEqual(d['title'], 'Wyrd Sisters: Discworld, Book 6')
        self.assertEqual(d['publisher'], 'Penguin Audio')

    def test_no_value_comes_from_a_later_section(self):
        # The general form of both cases above: nothing outside General
        # Information may reach ANY parsed value.
        for source in (WYRD, WYRD_WITH_TRAPS):
            for value in ST.parse_nfo(source).values():
                text = str(value)
                for leak in ('inAudible', 'Audible AAX', '44100',
                             'NOT THE BOOK TITLE', 'Some Blurb Publisher',
                             'lonely heath'):
                    self.assertNotIn(leak, text)



class TestNfoPeople(unittest.TestCase):
    """
        Splitting a credited-name field.

        The shipped function did `value.split(',')` BEFORE looking at the role
        note, and a role note routinely carries a comma. Measured with it:

          nfo_people("Arthur Conan Doyle, Stephen Fry (introductions, notes)")
            -> ['Arthur Conan Doyle', 'Stephen Fry (introductions', 'notes)']
          nfo_people("Terry Pratchett, Neil Gaiman (foreword, 2006)")
            -> ['Terry Pratchett', 'Neil Gaiman (foreword', '2006)']

        Two bogus names per field -- one with an unbalanced paren -- handed
        straight to score_author, which is the very skew this function exists
        to prevent. The old test only covered the comma-free "(introductions)"
        form, which the naive split happens to survive.

        It also hand-rolled a splitter the file already owns: MULTI_AUTHOR_RE
        is the canonical co-author separator set, so "Terry Pratchett & Neil
        Gaiman" used to come back as ONE joined name.
    """

    def test_a_role_note_containing_a_comma_does_not_leak_two_names(self):
        self.assertEqual(
            ST.nfo_people('Arthur Conan Doyle, Stephen Fry (introductions, notes)'),
            ['Arthur Conan Doyle'])

    def test_a_role_note_containing_a_year(self):
        self.assertEqual(
            ST.nfo_people('Terry Pratchett, Neil Gaiman (foreword, 2006)'),
            ['Terry Pratchett'])

    def test_no_entry_ever_carries_an_unbalanced_paren(self):
        for field in ('Arthur Conan Doyle, Stephen Fry (introductions, notes)',
                      'Terry Pratchett, Neil Gaiman (foreword, 2006)',
                      'A. Author (translation, 1999), B. Writer'):
            for name in ST.nfo_people(field):
                self.assertEqual(name.count('('), name.count(')'),
                                 'unbalanced paren in %r from %r' % (name, field))

    def test_the_comma_free_role_note_still_works(self):
        self.assertEqual(
            ST.nfo_people('Arthur Conan Doyle, Stephen Fry (introductions)'),
            ['Arthur Conan Doyle'])

    def test_the_canonical_separators_all_split(self):
        for field, expected in (
            ('Terry Pratchett & Neil Gaiman',
             ['Terry Pratchett', 'Neil Gaiman']),
            ('Terry Pratchett and Neil Gaiman',
             ['Terry Pratchett', 'Neil Gaiman']),
            ('John Bellairs/George Guidall',
             ['John Bellairs', 'George Guidall']),
            ('A. Author; B. Writer', ['A. Author', 'B. Writer']),
        ):
            self.assertEqual(ST.nfo_people(field), expected, field)

    def test_a_plain_comma_list_is_unchanged(self):
        self.assertEqual(
            ST.nfo_people('Indira Varma, Peter Serafinowicz, Bill Nighy'),
            ['Indira Varma', 'Peter Serafinowicz', 'Bill Nighy'])

    def test_a_name_with_an_internal_and_is_not_split(self):
        # " and " needs whitespace on both sides for exactly this reason.
        self.assertEqual(ST.nfo_people('Robert Jordan'), ['Robert Jordan'])
        self.assertEqual(ST.nfo_people('Poul Anderson'), ['Poul Anderson'])

    def test_junk_input(self):
        for junk in (None, '', '   ', ','):
            self.assertEqual(ST.nfo_people(junk), [])


class TestNfoValuesAreUnicode(unittest.TestCase):
    """
        parse_nfo's docstring promises "the SAME keys as the metadata.json
        sidecar, so every existing consumer reads it unchanged". sidecar()
        builds its values out of json.loads -- UNICODE -- while parse_nfo
        slices them out of a Core.storage.load payload, which under py2 is a
        byte str. A consumer that then does title.encode('utf-8')
        (ScoreTool.score_album does exactly that) raises UnicodeDecodeError on
        the first accented credit.

        WHAT THIS HARNESS CAN AND CANNOT PROVE. It runs py3, where `str` IS
        text and plexenv aliases `unicode` to `str` -- so the broken and the
        fixed version are INDISTINGUISHABLE on the type itself. Only a live
        py2 load separates them.

        What it CAN prove, and does below: that a bytes payload is accepted at
        all (before the fix parse_nfo would have been handed py2 bytes and
        returned py2 bytes; here bytes reach `'Title:' not in text` and return
        None outright), that decoding is lossless, and that a unicode payload
        survives untouched. Treat the type assertion as documentation of intent
        and the round-trip ones as the real pins.
    """

    def test_a_utf8_bytes_payload_is_decoded_not_rejected(self):
        # py2: this is what Core.storage.load actually returns.
        d = ST.parse_nfo(ACCENTED.encode('utf-8'))
        self.assertIsNotNone(
            d, 'a bytes payload must be decoded, not dropped on the floor')
        self.assertEqual(d['authors'], [u'Antoine de Saint-Exupéry'])

    def test_the_decode_is_lossless(self):
        self.assertEqual(ST.parse_nfo(ACCENTED.encode('utf-8')),
                         ST.parse_nfo(ACCENTED))

    def test_values_are_text_not_bytes(self):
        # The intent, stated. Under py2 this is the whole bug; under this
        # harness `unicode` is `str`, so it is a tautology -- see the class
        # docstring before treating a green here as proof.
        for value in ST.parse_nfo(ACCENTED).values():
            if isinstance(value, (bool, int)):
                continue
            self.assertNotIsInstance(value, bytes)

    def test_a_unicode_payload_is_passed_through(self):
        self.assertEqual(ST.parse_nfo(WYRD)['authors'], ['Terry Pratchett'])

    def test_an_undecodable_payload_does_not_raise(self):
        # A latin-1 ripper nfo. Better a slightly wrong glyph than losing the
        # duration -- and never an exception out of a search.
        d = ST.parse_nfo(ACCENTED.encode('latin-1'))
        self.assertIsNotNone(d)
        self.assertEqual(d['duration'], (9 * 3600 + 53 * 60 + 19) * 1000)


class TestNfoCandidatePaths(unittest.TestCase):
    """
        Finding the .nfo WITHOUT listing the directory.

        Core.storage exposes load/save; a directory listing would mean reaching
        for a framework API this plugin has never used, and an unavailable one
        fails at call time inside a sandbox that kills the whole plugin for far
        less. Measured across the operator's 193 folders that hold both an nfo
        and audio, the name is derivable instead:

          165 (85%)  nfo basename == the audio basename
           26        multi-file rip: audio is "<nfo basename> - 001.mp3", so the
                     nfo name is the audio name with a trailing part number cut
            2        nfo basename == the folder name

        Cheap to probe -- a handful of failed Core.storage.load calls -- and it
        needs nothing the plugin does not already do.
    """

    def test_the_common_case_is_first(self):
        got = ST.nfo_candidate_paths('/books/A/Wyrd Sisters.m4b')
        self.assertEqual(got[0], '/books/A/Wyrd Sisters.nfo')

    def test_a_multi_file_rip_trims_the_part_number(self):
        got = ST.nfo_candidate_paths('/books/A/Small Gods Discworld, Book 13 - 001.mp3')
        self.assertIn('/books/A/Small Gods Discworld, Book 13.nfo', got)

    def test_the_folder_name_is_a_candidate_too(self):
        got = ST.nfo_candidate_paths('/books/Terry Pratchett - Wyrd Sisters/01 - Chapter.mp3')
        self.assertIn('/books/Terry Pratchett - Wyrd Sisters/Terry Pratchett - Wyrd Sisters.nfo', got)

    def test_it_stays_small(self):
        # Each candidate is a real SMB round-trip on a miss; probing a long list
        # per track would cost more than the metadata is worth.
        got = ST.nfo_candidate_paths('/books/A/Some Book - 001.mp3')
        self.assertLessEqual(len(got), 4)
        self.assertEqual(len(got), len(set(got)), 'no duplicate probes')

    def test_junk_input_yields_nothing(self):
        for junk in (None, '', 'no-slash-here.m4b'):
            self.assertEqual(ST.nfo_candidate_paths(junk), [])

    def test_the_media_path_is_unquoted_like_the_sidecar_reader_does(self):
        """
        Plex hands media.filename over PERCENT-ENCODED -- sidecar() unquotes
        and decodes it before use, and this omitted both. A space is %20, so
        without the unquote every probed path is a path that cannot exist and
        the feature would miss essentially the whole library the moment it is
        wired up. Non-ASCII author folders are the other half.
        """
        self.assertEqual(
            ST.nfo_candidate_paths('/books/A/Wyrd%20Sisters.m4b')[0],
            '/books/A/Wyrd Sisters.nfo')

    def test_a_non_ascii_path_survives(self):
        got = ST.nfo_candidate_paths(
            '/books/Antoine%20de%20Saint-Exup%C3%A9ry/Le%20Petit%20Prince.m4b')
        self.assertEqual(
            got[0], u'/books/Antoine de Saint-Exupéry/Le Petit Prince.nfo')

    def test_an_already_decoded_path_is_not_double_decoded(self):
        # Plex is not consistent about this and the same value reaches other
        # readers already decoded; unquoting a plain path must be a no-op.
        self.assertEqual(
            ST.nfo_candidate_paths(u'/books/Antoine de Saint-Exupéry/Le Petit Prince.m4b')[0],
            u'/books/Antoine de Saint-Exupéry/Le Petit Prince.nfo')

if __name__ == '__main__':
    unittest.main()
