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

    def test_the_description_section_is_not_swallowed_as_fields(self):
        # Prose lines contain colons; none of them may become keys.
        d = ST.parse_nfo(WYRD)
        for key in d:
            self.assertNotIn(' ', key.strip())

    def test_media_information_is_ignored(self):
        # Encoder details describe the rip, not the book.
        d = ST.parse_nfo(WYRD)
        for junk in ('ripper', 'source format', 'sample rate', 'bitrate'):
            self.assertNotIn(junk, [k.lower() for k in d])



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

if __name__ == '__main__':
    unittest.main()
