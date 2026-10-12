"""
Square local art of last resort (v1.3.219).

When nothing square exists online or in cover.jpg, the square art the book's own
files carry -- its embedded cover, as Local Media Assets offers it -- becomes the
selection instead of the print jacket.

Measured on prod 2026-10-10: of 1,756 albums exactly four showed a portrait
poster, all four G. Norman Lippert's James Potter books. Fan audiobooks no store
sells, so each matched a Hardcover BOOK record whose cover is the 1900x2850 print
jacket; Apple's square lookup found nothing; three folders held a cover.jpg that
is the same jacket; and every .m4b carried a 1600x1600 cover that Local Media
Assets offered as a tile -- which correct_portrait_selection, by its own
docstring, could not select.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']


def png(width, height, tag=b''):
    """A PNG header image_dimensions can measure, made distinct by `tag`."""
    return (b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR'
            + struct.pack('>II', width, height) + tag)


JACKET = png(1900, 2850, b'hardcover print jacket')
EMBEDDED = png(1600, 1600, b'the cover embedded in the m4b')
ARTIST = png(1000, 1000, b'the author photo')
INCIPIT_KEY = 'metadata://posters/com.plexapp.agents.incipit_8bd89fd07f8577df'
LMA_SQUARE = 'metadata://posters/com.plexapp.agents.localmedia_2333d2623626b63c'
LMA_JACKET = 'metadata://posters/com.plexapp.agents.localmedia_9a1b0c7d5e3f2a10'
GUID = 'com.plexapp.agents.incipit://hardcover-book-699486_us?lang=en'


class SquareShape(unittest.TestCase):
    def test_square_and_near_square_are_square(self):
        self.assertTrue(AG.is_square_art((1600, 1600)))
        self.assertTrue(AG.is_square_art((1000, 1100)))
        self.assertTrue(AG.is_square_art((1100, 1000)))

    def test_portrait_and_landscape_are_not(self):
        self.assertFalse(AG.is_square_art((1900, 2850)))
        self.assertFalse(AG.is_square_art((2850, 1900)))

    def test_unmeasurable_is_never_square(self):
        self.assertFalse(AG.is_square_art(None))
        self.assertFalse(AG.is_square_art((0, 100)))


class LocalSquareCandidate(unittest.TestCase):
    def test_the_embedded_square_is_chosen_over_the_jackets(self):
        entries = [(INCIPIT_KEY, JACKET), (LMA_SQUARE, EMBEDDED), (LMA_JACKET, JACKET)]
        self.assertEqual(AG.local_square_candidate(entries, ARTIST), EMBEDDED)

    def test_only_local_media_tiles_qualify(self):
        # Our own key and an upload are never candidates, however square.
        entries = [(INCIPIT_KEY, EMBEDDED), ('upload://posters/0123abcd', EMBEDDED)]
        self.assertIsNone(AG.local_square_candidate(entries, ARTIST))

    def test_the_artist_photo_never_qualifies(self):
        self.assertIsNone(AG.local_square_candidate([(LMA_SQUARE, ARTIST)], ARTIST))

    def test_a_small_embed_never_replaces_a_jacket(self):
        tiny = png(300, 300, b'thumbnail-sized embed')
        self.assertIsNone(AG.local_square_candidate([(LMA_SQUARE, tiny)], ARTIST))

    def test_the_minimum_edge_is_inclusive(self):
        edge = png(AG.LOCAL_SQUARE_MIN_EDGE, AG.LOCAL_SQUARE_MIN_EDGE, b'at the floor')
        self.assertEqual(AG.local_square_candidate([(LMA_SQUARE, edge)], ARTIST), edge)

    def test_the_larger_square_wins_then_the_smaller_key(self):
        small = png(800, 800, b'smaller')
        big = png(1600, 1600, b'bigger')
        self.assertEqual(
            AG.local_square_candidate([(LMA_SQUARE, small), (LMA_JACKET, big)], ARTIST), big)
        twin_a = png(1600, 1600, b'a')
        twin_b = png(1600, 1600, b'b')
        self.assertEqual(
            AG.local_square_candidate([(LMA_SQUARE, twin_a), (LMA_JACKET, twin_b)], ARTIST),
            twin_a)

    def test_an_unreadable_tile_is_skipped(self):
        self.assertIsNone(AG.local_square_candidate([(LMA_SQUARE, None)], ARTIST))


class SelectLocalSquareArt(unittest.TestCase):
    NAMES = ('read_poster_state', 'selected_poster_bytes', 'poster_file_bytes',
             'thumb_field_locked', 'artist_poster_bytes', 'upload_and_select_poster')

    def setUp(self):
        AG.recent_work_memo.clear()
        self.saved = dict((n, getattr(AG, n)) for n in self.NAMES)
        self.state = ('754956', INCIPIT_KEY, [INCIPIT_KEY, LMA_SQUARE, LMA_JACKET],
                      '/library/metadata/754955/thumb/1')
        self.selected = JACKET
        self.selected_known = True
        self.tiles = {LMA_SQUARE: EMBEDDED, LMA_JACKET: JACKET}
        self.locked = False
        self.lock_known = True
        self.artist_known = True
        self.state_reads = []
        self.lock_reads = []
        self.uploads = []
        outer = self
        AG.read_poster_state = lambda guid, tag: outer.state_reads.append(guid) or outer.state
        AG.selected_poster_bytes = lambda rk, key, tag: (outer.selected, outer.selected_known)
        AG.poster_file_bytes = lambda rk, key, tag: outer.tiles.get(key)
        AG.thumb_field_locked = (
            lambda rk, tag: outer.lock_reads.append(rk) or (outer.locked, outer.lock_known))
        AG.artist_poster_bytes = (
            lambda guid, tag, parent_thumb=None: (ARTIST, outer.artist_known))

        def upload(guid, image_bytes, tag, token=None, state=None,
                   pref_asserted=False, selected=None):
            # The real contract: a successful upload marks the work done under
            # the caller's token, which is what spares an album's sibling tracks.
            outer.uploads.append(image_bytes)
            AG.mark_done(tag, guid, token)
            return True
        AG.upload_and_select_poster = upload

    def tearDown(self):
        for n, f in self.saved.items():
            setattr(AG, n, f)
        AG.recent_work_memo.clear()

    def run_it(self, online=JACKET, cover=None):
        class FakeMetadata(object):
            guid = GUID

        class FakeHelper(object):
            metadata = FakeMetadata()
        AG.select_local_square_art(FakeHelper(), online, cover)

    def test_the_james_potter_shape_selects_the_embedded_square(self):
        self.run_it()
        self.assertEqual(self.uploads, [EMBEDDED])

    def test_a_record_with_no_online_cover_at_all_still_gets_it(self):
        self.run_it(online=None)
        self.assertEqual(self.uploads, [EMBEDDED])

    def test_an_online_square_leaves_the_book_to_the_existing_paths(self):
        self.run_it(online=png(1400, 1400, b'apple square'))
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.state_reads, [])

    def test_a_persons_upload_is_never_changed(self):
        # Locked, as Plex leaves every upload; ownership stands it down first.
        self.state = ('754956', 'upload://posters/feedfacecafe', self.state[2], self.state[3])
        self.locked = True
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_the_hand_uploaded_embedded_art_stays(self):
        # The live James Potter state since 2026-10-10: the operator uploaded each
        # book's own embedded art. Not bytes the agent holds, and square besides.
        sha = AG.pad_family_shas(EMBEDDED)[0][0]
        self.state = ('754956', 'upload://posters/' + sha, self.state[2], self.state[3])
        self.selected = EMBEDDED
        self.locked = True
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_our_own_upload_of_the_jacket_is_ours_to_move(self):
        # Plex locks EVERY upload, the agent's own included (prod 2026-10-10: 215
        # of 215 album upload selections), so the lock is not asked of an upload
        # the agent provably wrote. v1.3.219 asked it, and stood down.
        sha = AG.pad_family_shas(JACKET)[0][0]
        self.state = ('754956', 'upload://posters/' + sha, self.state[2], self.state[3])
        self.locked = True
        self.run_it()
        self.assertEqual(self.uploads, [EMBEDDED])
        self.assertEqual(self.lock_reads, [])

    def test_our_own_upload_of_cover_jpg_is_ours_to_move(self):
        cover = png(1900, 2850, b'cover.jpg, the same print jacket saved to disk')
        sha = AG.pad_family_shas(cover)[0][0]
        self.state = ('754950', 'upload://posters/' + sha, self.state[2], self.state[3])
        self.selected = cover
        self.locked = True
        self.run_it(cover=cover)
        self.assertEqual(self.uploads, [EMBEDDED])

    def test_an_unreadable_lock_never_matters_for_our_own_upload(self):
        sha = AG.pad_family_shas(JACKET)[0][0]
        self.state = ('754956', 'upload://posters/' + sha, self.state[2], self.state[3])
        self.lock_known = False
        self.run_it()
        self.assertEqual(self.uploads, [EMBEDDED])

    def test_a_square_cover_jpg_is_curated_art_and_left_alone(self):
        # Local Media Assets offers cover.jpg as a tile too, and the larger embed
        # would outrank it, so the function stands down before choosing at all.
        self.run_it(cover=png(1400, 1400, b'a curated square cover.jpg'))
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.state_reads, [])

    def test_a_selection_that_is_already_square_is_left(self):
        self.selected = EMBEDDED
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_a_field_locked_jacket_is_a_human_pick(self):
        # The default state selects our CONTAINER tile: the one key form whose
        # lock tells a person's click from a scan default.
        self.locked = True
        self.run_it()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.lock_reads, ['754956'])

    def test_an_unreadable_lock_fails_closed_and_retries_next_pass(self):
        self.lock_known = False
        self.run_it()
        self.assertEqual(self.uploads, [])
        # Not memoised as done: the next pass, with the lock readable, acts.
        self.lock_known = True
        self.run_it()
        self.assertEqual(self.uploads, [EMBEDDED])

    def test_an_unreadable_selection_fails_closed(self):
        self.selected_known = False
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_an_unreadable_artist_poster_fails_closed(self):
        self.artist_known = False
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_no_square_tile_leaves_the_jacket(self):
        self.tiles = {LMA_JACKET: JACKET}
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_the_artist_photo_tile_is_never_selected(self):
        self.tiles = {LMA_SQUARE: ARTIST, LMA_JACKET: JACKET}
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_no_selection_at_all_changes_nothing(self):
        self.state = ('754956', None, self.state[2], self.state[3])
        self.run_it()
        self.assertEqual(self.uploads, [])

    def test_sibling_tracks_do_not_repeat_the_work(self):
        self.run_it()
        self.run_it()
        self.assertEqual(len(self.state_reads), 1)


class CorrectPortraitSelectionNeedsASquare(unittest.TestCase):
    """correct_portrait_selection's docstring already said "does nothing without a
    square to offer"; the code never measured. A jacket moved onto another jacket
    is churn, so a MEASURED non-square refuses before any read -- while bytes it
    cannot measure proceed exactly as before."""

    def setUp(self):
        AG.recent_work_memo.clear()
        self.saved = AG.read_poster_state
        self.reads = []
        outer = self
        AG.read_poster_state = lambda guid, tag: outer.reads.append(guid) or None

    def tearDown(self):
        AG.read_poster_state = self.saved
        AG.recent_work_memo.clear()

    def helper(self):
        class FakeMetadata(object):
            guid = GUID

        class FakeHelper(object):
            metadata = FakeMetadata()
        return FakeHelper()

    def test_a_measured_jacket_is_not_a_square_to_offer(self):
        AG.correct_portrait_selection(self.helper(), JACKET, png(1900, 2850, b'online jacket'))
        self.assertEqual(self.reads, [])

    def test_a_measured_square_still_proceeds(self):
        AG.correct_portrait_selection(self.helper(), JACKET, png(1400, 1400, b'apple square'))
        self.assertEqual(self.reads, [GUID])

    def test_unmeasurable_bytes_proceed_as_before(self):
        AG.correct_portrait_selection(self.helper(), JACKET, b'\xff\xd8\xff\xe0 unmeasurable')
        self.assertEqual(self.reads, [GUID])


if __name__ == '__main__':
    unittest.main()
