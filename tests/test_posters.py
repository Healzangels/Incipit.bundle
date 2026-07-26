"""
Poster identity and the caching decisions around it.

"Is this image the artist's photo?" is the question the whole poison machinery
turns on, and it has to answer correctly for the PADDED form too. The agent no
longer MINTS pads for book covers (v1.3.112 -- see DeliberateDeselection below)
but albums touched before that are still wearing image+RESELECT_PAD copies, so
a poisoned album that went through one pre-112 cycle carries 20 extra bytes. An
exact-byte comparison missed that for months (found live on Kyle Mills /
"Fade").
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']
PAD = AG.RESELECT_PAD

ARTIST = b'\xff\xd8\xff\xe0 artist photo bytes'
COVER = b'\xff\xd8\xff\xe0 a real book cover'


class ArtistArtIdentity(unittest.TestCase):
    def test_exact_match_is_artist_art(self):
        self.assertTrue(AG.selection_is_artist_art(ARTIST, ARTIST))

    def test_padded_match_is_artist_art(self):
        # The form the pre-1.3.103 guard could not see.
        self.assertTrue(AG.selection_is_artist_art(ARTIST, ARTIST + PAD))

    def test_a_real_cover_is_not_artist_art(self):
        self.assertFalse(AG.selection_is_artist_art(ARTIST, COVER))

    def test_unknown_artist_art_is_never_a_match(self):
        # None means "could not tell". It must never read as a positive, or a
        # failed read would look like proof of poison.
        self.assertFalse(AG.selection_is_artist_art(None, COVER))
        self.assertFalse(AG.selection_is_artist_art(ARTIST, None))

    def test_double_padding_is_not_recognised(self):
        # One pad level is the documented boundary; a second means something
        # upstream is re-padding and should NOT be silently accepted.
        self.assertFalse(AG.selection_is_artist_art(ARTIST, ARTIST + PAD + PAD))


class PaddedVariants(unittest.TestCase):
    def test_shas_are_distinct_and_deterministic(self):
        sha, sha_padded, padded = AG.padded_variants(ARTIST)
        self.assertNotEqual(sha, sha_padded)
        self.assertEqual(padded, ARTIST + PAD)
        self.assertEqual(AG.padded_variants(ARTIST)[0], sha)

    def test_the_pad_is_exactly_twenty_bytes(self):
        # poison_sweep.py hardcodes this length to recognise padded copies from
        # outside the agent; a change here silently breaks that reader.
        self.assertEqual(len(PAD), 20)


class SearchCacheTime(unittest.TestCase):
    def test_a_manual_search_is_never_cached(self):
        # Fix Match is the operator saying "the automatic answer is wrong", so
        # replaying the automatic answer is the one thing it must not do. The
        # dialog builds the SAME url the scan built, which made it a guaranteed
        # cache hit showing a ranking up to an hour old.
        self.assertEqual(AG.search_cache_time(True), 0)

    def test_a_scan_still_caches(self):
        # The per-track fan-out is what the cache exists for; a multi-part book
        # fires the same album search once per track.
        self.assertEqual(AG.search_cache_time(False), 3600)

    def test_the_default_is_the_cached_scan_path(self):
        self.assertEqual(AG.search_cache_time(), 3600)


class SelectionOwnership(unittest.TestCase):
    def test_no_selection_is_ours(self):
        self.assertTrue(AG.selection_is_agent_owned(None, ['abc']))

    def test_our_own_upload_is_ours(self):
        self.assertTrue(
            AG.selection_is_agent_owned('upload://posters/abc123', ['abc123']))

    def test_a_user_upload_is_not_ours(self):
        # Plex names a user upload upload://posters/<sha> with no agent id.
        self.assertFalse(
            AG.selection_is_agent_owned('upload://posters/deadbeef', ['abc123']))

    def test_an_agent_container_poster_is_ours(self):
        self.assertTrue(AG.selection_is_agent_owned(
            'metadata://posters/com.plexapp.agents.incipit_x', ['abc123']))


class WorkMemo(unittest.TestCase):
    def setUp(self):
        AG.recent_work_memo.clear()

    def test_the_same_token_is_suppressed_within_the_ttl(self):
        self.assertTrue(AG.should_run('tag', 'guid', 'token', 600))
        AG.mark_done('tag', 'guid', 'token')
        self.assertFalse(AG.should_run('tag', 'guid', 'token', 600))

    def test_a_changed_token_always_re_runs(self):
        # This is what lets a repaired cover.jpg take effect immediately
        # instead of waiting out the TTL.
        AG.mark_done('tag', 'guid', 'token')
        self.assertTrue(AG.should_run('tag', 'guid', 'DIFFERENT', 600))

    def test_a_zero_ttl_never_suppresses(self):
        AG.mark_done('tag', 'guid', 'token')
        self.assertTrue(AG.should_run('tag', 'guid', 'token', 0))


class DeliberateDeselection(unittest.TestCase):
    """
    A cover Plex already OFFERS but is not selecting was de-selected on purpose.

    The escalation ladder in upload_and_select_poster had three rungs: post the
    cover when Plex does not have it; post a byte-padded copy when Plex has it
    but has it de-selected; give up when both copies exist de-selected. The
    middle rung is the operator picking a different poster and the agent
    overriding them on the next refresh.

    Measured live on Will Wight (Soulsmith/Blackflame/Skysworn, 2026-07-25):
    each showed "uploaded + selected (... PADDED re-select)" at 14:01 with byte
    counts exactly 20 over the file on disk, and the hand-picked poster was
    gone. A second attempt at 14:10 "worked" only because the pad had already
    been spent, leaving the agent out of levers -- so the same action gave
    opposite results and the fix looked random.

    Only the FIRST rung is a legitimate job for a BOOK: a new album whose
    cover.jpg Plex has never seen. If Plex already lists it, standing down is
    what lets backup_selected_poster mirror the operator's choice to disk.

    AUTHOR art is the exception (pref_asserted below): there the de-selector is
    the AGENT's own unpin, not a person, and the operator's wish is expressed by
    the authors_prefer_hardcover pref itself -- so the pad lever survives for
    that caller alone. Without it, pin->unpin->re-pin wedged the pref forever.
    """

    def setUp(self):
        self.posts = []
        self.real = AG.HTTP.Request

        def recorder(url, **kwargs):
            # Records and SUCCEEDS, so tests can assert the success path
            # (mark_done / return True) actually runs -- a raising recorder was
            # swallowed by the plugin's `except Exception` and proved nothing
            # past the call itself.
            self.posts.append((url, kwargs))

            class FakeResponse(object):
                content = 'ok'

            return FakeResponse()

        AG.HTTP.Request = recorder
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.recent_work_memo.clear()

    def _state(self, keys, selected='upload://posters/something-else'):
        # (rk, selected_key, keys, parent_thumb)
        return ('101', selected, keys, None)

    def test_an_offered_but_deselected_cover_is_left_alone(self):
        sha, _padded, _bytes = AG.padded_variants(COVER)
        result = AG.upload_and_select_poster(
            'guid-a', COVER, 'test', state=self._state([sha]))
        self.assertFalse(result)
        self.assertEqual(self.posts, [], 'must not re-upload a padded copy')
        # mark_done separates STANDING DOWN from an upload that merely failed:
        # the failure path deliberately does not mark, so a blip retries.
        self.assertIn(('test', 'guid-a'), AG.recent_work_memo)

    def test_both_variants_deselected_still_stands_down(self):
        sha, sha_padded, _bytes = AG.padded_variants(COVER)
        result = AG.upload_and_select_poster(
            'guid-b', COVER, 'test', state=self._state([sha, sha_padded]))
        self.assertFalse(result)
        self.assertEqual(self.posts, [])

    def test_a_cover_plex_has_never_seen_is_still_uploaded(self):
        # The rung that must survive: a new book, nothing of ours in the list.
        result = AG.upload_and_select_poster(
            'guid-c', COVER, 'test', state=self._state([], selected=None))
        self.assertTrue(result, 'a successful POST must report success')
        self.assertEqual(len(self.posts), 1, 'the birth case must still upload')
        self.assertIn(('test', 'guid-c'), AG.recent_work_memo)

    def test_an_already_selected_cover_is_a_no_op(self):
        sha, _padded, _bytes = AG.padded_variants(COVER)
        result = AG.upload_and_select_poster(
            'guid-d', COVER, 'test', state=self._state([sha], selected=sha))
        self.assertTrue(result)
        self.assertEqual(self.posts, [])


class PrefAssertedReselect(unittest.TestCase):
    """
    The author-pin caller must still be able to RE-select its own de-selected
    upload -- the agent's own unpin is what de-selected it, so reading that as
    an operator choice wedged authors_prefer_hardcover permanently after one
    pin->unpin round trip.
    """

    def setUp(self):
        self.posts = []
        self.real = AG.HTTP.Request

        def recorder(url, **kwargs):
            self.posts.append((url, kwargs))

            class FakeResponse(object):
                content = 'ok'

            return FakeResponse()

        AG.HTTP.Request = recorder
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.recent_work_memo.clear()

    def _state(self, keys, selected='upload://posters/something-else'):
        return ('101', selected, keys, None)

    def test_a_pref_reassert_posts_the_padded_copy(self):
        # Re-pin: the portrait's plain bytes exist de-selected. The pref speaks
        # for the operator, so the pad lever fires -- new content to the store,
        # identical pixels, which re-selects.
        sha, _padded, _bytes = AG.padded_variants(ARTIST)
        result = AG.upload_and_select_poster(
            'guid-p', ARTIST, 'test', state=self._state([sha]),
            pref_asserted=True)
        self.assertTrue(result)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][1]['data'], ARTIST + PAD,
                         'the re-select must be the padded copy, not a no-op re-POST')

    def test_a_pref_reassert_out_of_levers_stands_down(self):
        # Both variants already exist de-selected: the one-level pad budget is
        # spent, and minting deeper pads would grow without bound.
        sha, sha_padded, _bytes = AG.padded_variants(ARTIST)
        result = AG.upload_and_select_poster(
            'guid-q', ARTIST, 'test', state=self._state([sha, sha_padded]),
            pref_asserted=True)
        self.assertFalse(result)
        self.assertEqual(self.posts, [])

    def test_books_still_stand_down_without_the_flag(self):
        # The default is unchanged: a book's de-selected cover is a choice.
        sha, _padded, _bytes = AG.padded_variants(COVER)
        result = AG.upload_and_select_poster(
            'guid-r', COVER, 'test', state=self._state([sha]))
        self.assertFalse(result)
        self.assertEqual(self.posts, [])


class PortraitDeferralMirror(unittest.TestCase):
    """
    A portrait cover.jpg must not disable the mirror for the whole book.

    The gate used to skip backup_selected_poster entirely whenever the local
    cover was portrait-deferred, to stop the agent's AUTOMATIC online-cover
    default from overwriting the operator's file. Right fear, wrong scope: it
    also swallowed every DELIBERATE pick -- measured live on Nick Jones /
    "The Unexpected Gift of Joseph Bridgeman" (2026-07-25), where a hand-picked
    poster plus Refresh Metadata changed nothing on disk, silently, because the
    old cover.jpg happened to be a print jacket.

    The distinguishing fact: the agent only ever auto-selects the ONLINE cover
    it deferred to. Any other selection was a human act. So the mirror now runs
    on portrait books too, and skips only when the selection is byte-identical
    to that deferred default (fail-closed when the default can't be fetched).
    This is the third repair path this flag has silently suppressed (poison
    repair in v1.3.108, now the mirror) -- prefer narrowing it over gating on it.
    """

    ONLINE = b'\xff\xd8\xff\xe0 the square online cover'
    PICKED = b'\xff\xd8\xff\xe0 the poster the operator picked'
    PORTRAIT = b'\xff\xd8\xff\xe0 old portrait print jacket'

    def setUp(self):
        AG.recent_work_memo.clear()
        self.writes = []
        self.saved = (AG.HTTP.Request, AG.Core.storage.load, AG.write_cover_sidecar,
                      AG.fetch_url_bytes, AG.read_poster_state)
        self.selected_bytes = self.PICKED
        self.online_bytes = self.ONLINE
        outer = self

        def router(url, **kwargs):
            class FakeResponse(object):
                content = ''
            reply = FakeResponse()
            if '/library/all' in url:
                reply.content = ('<MediaContainer size="1">'
                                 '<Directory ratingKey="55" '
                                 'thumb="/library/metadata/55/thumb/1"/>'
                                 '</MediaContainer>')
            elif '/thumb/' in url:
                reply.content = outer.selected_bytes
            return reply

        AG.HTTP.Request = router
        AG.Core.storage.load = lambda path: outer.PORTRAIT
        AG.write_cover_sidecar = (
            lambda path, data: outer.writes.append((path, data)) or True)
        AG.fetch_url_bytes = lambda url: outer.online_bytes
        # F13 curated-file guard: no poster state -> the guard stands aside,
        # which is this book's real shape (a deferred portrait was never
        # uploaded, so there is no upload key to protect).
        AG.read_poster_state = lambda guid, tag: None

    def tearDown(self):
        (AG.HTTP.Request, AG.Core.storage.load, AG.write_cover_sidecar,
         AG.fetch_url_bytes, AG.read_poster_state) = self.saved
        AG.recent_work_memo.clear()

    def _helper(self):
        class FakeMetadata(object):
            guid = 'com.plexapp.agents.incipit://TESTBRIDGE_us'

        class FakeHelper(object):
            metadata = FakeMetadata()
            thumb = 'https://images.example/online-cover.jpg'
            force = True

            def album_file_path(self):
                return '/data/media/x/1 - Book/file.m4b'

        return FakeHelper()

    def test_a_deliberate_pick_is_mirrored_despite_the_portrait(self):
        # The Bridgeman case: portrait on disk, operator picked a different
        # poster. The old gate skipped the mirror wholesale; the pick must land.
        AG.backup_selected_poster(self._helper(), portrait_deferred=True)
        self.assertEqual(len(self.writes), 1, 'the pick must reach cover.jpg')
        self.assertEqual(self.writes[0][1], self.PICKED)

    def test_the_deferred_default_is_still_never_mirrored(self):
        # The danger the gate existed for: the agent's own automatic online
        # default must not overwrite the operator's portrait file.
        self.selected_bytes = self.ONLINE
        AG.backup_selected_poster(self._helper(), portrait_deferred=True)
        self.assertEqual(self.writes, [])

    def test_an_unreadable_default_fails_closed(self):
        # Cannot tell a pick from the default -> do not guess with a write.
        self.online_bytes = None
        AG.backup_selected_poster(self._helper(), portrait_deferred=True)
        self.assertEqual(self.writes, [])

    def test_a_normal_book_still_mirrors_without_the_flag(self):
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][1], self.PICKED)

    def test_a_pick_mirrors_even_when_the_disk_bytes_were_once_uploaded(self):
        # Bridgeman's live state, 2026-07-25 16:26. Portrait deferred, the
        # operator picked an agent METADATA poster, and the disk file's own
        # bytes sit in a DE-selected upload left by the pre-1.3.112 flows. The
        # v1.3.114 review guard read that as "curated file needs protecting"
        # and blocked the very mirror the pick required -- but it could not
        # tell a deliberate pick of an agent poster from an automatic re-seat,
        # and the automatic cases are already covered: a re-match's default is
        # either the local cover (byte-identical, unchanged-skip) or the
        # portrait book's online default (refused above). The guard only ever
        # fired on human picks, so it is gone.
        sha, sha_padded, unused = AG.padded_variants(self.PORTRAIT)
        AG.read_poster_state = lambda guid, tag: (
            '103831', 'metadata://posters/com.plexapp.agents.incipit_pick',
            ['upload://posters/' + sha], None)
        AG.backup_selected_poster(self._helper(), portrait_deferred=True)
        self.assertEqual(len(self.writes), 1, 'the pick must still reach cover.jpg')
        self.assertEqual(self.writes[0][1], self.PICKED)


if __name__ == '__main__':
    unittest.main()


class SquareTilePortraitChoice(unittest.TestCase):
    """
    Which of two author portraits fills Plex's SQUARE artist tile better.

    The agent used to always select the Audible image (`imageAlt`), because a
    two-key validate_keys selects the LAST key and the Audible one was appended
    last. Measured across the live library, that rule is right about half the
    time and wrong the rest: Robert Harris has a 270x270 Hardcover square and a
    211x250 Audible photo, William Gibson 500x500 against 219x315 -- in both the
    discarded image was squarer AND higher resolution.

    The metric is the SHORT EDGE, because that is exactly the resolution left
    after cropping to a square tile. A 3072x2304 landscape yields 2304px of
    usable image; a 117x150 thumbnail yields 117 and looks like a postage stamp
    however square it is. That single number keeps Glen Cook on his 3072x2304
    photo, which a naive "prefer square" rule would have swapped for the 117x150.

    The squareness tiebreak only applies when the short edges are COMPARABLE
    (within 25%): there a native square wins, because cropping a tall portrait
    to a square cuts the top or bottom of the subject, and the pixels given up
    are few. That is what takes Bryce O'Connor's 820x820 over his 1000x1500.
    """

    def test_the_bigger_short_edge_wins_outright(self):
        # Glen Cook: the squarer option is a 117x150 thumbnail. Squareness must
        # never buy a blurry image.
        self.assertEqual(
            AG.better_square_portrait((117, 150), (3072, 2304)), (3072, 2304))

    def test_a_square_wins_when_the_short_edges_are_close(self):
        # Bryce O'Connor: 820 vs 1000 is within 25%, so the native square wins
        # and no cropping is needed.
        self.assertEqual(
            AG.better_square_portrait((820, 820), (1000, 1500)), (820, 820))

    def test_squarer_AND_larger_is_never_discarded(self):
        # Robert Harris and William Gibson: the old always-Audible rule threw
        # away an image that was better on BOTH axes.
        self.assertEqual(
            AG.better_square_portrait((270, 270), (211, 250)), (270, 270))
        self.assertEqual(
            AG.better_square_portrait((500, 500), (219, 315)), (500, 500))

    def test_a_much_larger_tall_photo_still_wins(self):
        # Philip Pullman: 419 vs 984 is far outside the tie band, so the extra
        # resolution decides even though the loser is squarer.
        self.assertEqual(
            AG.better_square_portrait((419, 500), (984, 1380)), (984, 1380))

    def test_landscape_and_portrait_are_treated_alike(self):
        # The short edge is orientation-blind: a wide photo crops the sides,
        # a tall one crops top and bottom, and both keep min(w, h).
        self.assertEqual(
            AG.better_square_portrait((2000, 800), (900, 900)), (900, 900))

    def test_an_unmeasurable_image_never_wins_by_accident(self):
        # image_dimensions returns None for anything it cannot parse. A None
        # must lose to a known-good image rather than sort first.
        self.assertEqual(AG.better_square_portrait(None, (400, 400)), (400, 400))
        self.assertEqual(AG.better_square_portrait((400, 400), None), (400, 400))

    def test_two_unmeasurable_images_leave_the_order_alone(self):
        # Nothing to decide with: return None so the caller keeps its default.
        self.assertIsNone(AG.better_square_portrait(None, None))

    def test_identical_images_are_stable(self):
        self.assertEqual(
            AG.better_square_portrait((500, 500), (500, 500)), (500, 500))


class BestFitAuthorArtSelect(unittest.TestCase):
    """
    The container only decides on a FRESH scan, so an already-scanned artist
    keeps whatever Plex persisted -- which is why 39 artists in the live library
    sat on the worse-fitting portrait even after the ordering was fixed.

    `prefer_square_author_art` opts into force-selecting the better fit on an
    already-scanned artist. OFF by default: it re-selects images the operator
    may have chosen by hand, and a container key is indistinguishable from a
    deliberate click (the same reason unpin_hardcover_author_art refuses to
    touch one). Opting in is the operator saying they want the tile filled.
    """

    def setUp(self):
        AG.recent_work_memo.clear()
        self.calls = []
        self.real = AG.converge_author_art
        AG.converge_author_art = lambda helper, target, other, tag, **kw: \
            self.calls.append((target, other, tag))
        self.prefs = dict(plexenv.FakePrefs.DEFAULTS)

    def tearDown(self):
        AG.converge_author_art = self.real
        AG.recent_work_memo.clear()

    def _helper(self):
        class FakeHelper(object):
            thumb = 'https://hardcover/portrait.jpg'
            thumb_secondary = 'https://audible/photo.jpg'
        return FakeHelper()

    def test_it_selects_the_better_fit(self):
        # Robert Harris: 270x270 square (thumb) beats 211x250 (secondary).
        AG.select_best_fit_author_art(self._helper(), (270, 270), (211, 250))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'https://hardcover/portrait.jpg')

    def test_it_selects_the_secondary_when_that_fits_better(self):
        # Glen Cook: the 3072x2304 secondary beats a 117x150 thumb.
        AG.select_best_fit_author_art(self._helper(), (117, 150), (3072, 2304))
        self.assertEqual(self.calls[0][0], 'https://audible/photo.jpg')

    def test_an_unmeasurable_pair_does_nothing_at_all(self):
        # No evidence, no re-selection: leave the artist exactly as they are.
        AG.select_best_fit_author_art(self._helper(), None, (400, 400))
        AG.select_best_fit_author_art(self._helper(), (400, 400), None)
        AG.select_best_fit_author_art(self._helper(), None, None)
        self.assertEqual(self.calls, [])

    def test_identical_dimensions_do_nothing(self):
        # Nothing to gain, and a needless upload/select round trip per refresh.
        AG.select_best_fit_author_art(self._helper(), (500, 500), (500, 500))
        self.assertEqual(self.calls, [])

    def test_a_missing_second_image_does_nothing(self):
        helper = self._helper()
        helper.thumb_secondary = ''
        AG.select_best_fit_author_art(helper, (270, 270), (211, 250))
        self.assertEqual(self.calls, [])


class SelectionAlreadyShowsThisImage(unittest.TestCase):
    """
    Do not upload a copy of the image Plex is ALREADY displaying.

    A container poster's key is a hash of the KEY STRING we filed it under
    (sha1('incipit-local-cover')), never the image's byte sha -- the fact
    selection_is_agent_owned records. So the `sha in selected_key` skip at the
    top of upload_and_select_poster cannot recognise "the selection already IS
    these pixels" when the selection is a container entry, and the agent
    uploaded a byte-identical duplicate.

    Measured live 2026-07-25 on the audiobook library: 147 of 150 sampled
    albums (98%) carried an upload holding bytes their own incipit container
    already offered, and 75 of 169 artists carried the same duplication -- the
    two identical tiles in the poster picker. Nearly half of every poster tile
    in the library was a copy of another tile.

    The guard must FAIL OPEN. Uploading when we cannot read the selection is
    the status quo and merely wastes a POST; SKIPPING on an unreadable
    selection would leave a wrong poster in place, which is the destructive
    direction and the one the poison guards already fail closed against.
    """

    def setUp(self):
        self.posts = []
        self.reads = []
        self.real = AG.HTTP.Request
        self.served = COVER

        def router(url, **kwargs):
            if kwargs.get('data') is not None:
                self.posts.append(url)

                class Posted(object):
                    content = 'ok'

                return Posted()
            self.reads.append(url)
            if self.served is None:
                raise IOError('poster read failed')

            class Fetched(object):
                content = self.served

            Fetched.content = self.served
            return Fetched()

        AG.HTTP.Request = router
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.recent_work_memo.clear()

    # A container key, shaped exactly like the live ones: the agent id plus
    # sha1 of the key string, NOT of the image.
    CONTAINER = ('metadata://posters/com.plexapp.agents.incipit_'
                 '124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc')

    def _state(self, selected):
        return ('101', selected, [], None)

    def test_no_upload_when_the_selection_is_already_these_pixels(self):
        result = AG.upload_and_select_poster(
            'guid-same', COVER, 'test', state=self._state(self.CONTAINER))
        self.assertTrue(result, 'already-correct counts as converged')
        self.assertEqual(self.posts, [],
                         'the selected poster IS this image -- uploading it '
                         'again just adds a duplicate tile')

    def test_a_changed_cover_still_uploads(self):
        # The v1.3.45 behaviour that must survive: replacing cover.jpg with a
        # DIFFERENT image has to reach Plex. Only byte equality may skip.
        self.served = ARTIST
        result = AG.upload_and_select_poster(
            'guid-changed', COVER, 'test', state=self._state(self.CONTAINER))
        self.assertTrue(result)
        self.assertEqual(len(self.posts), 1, 'a changed cover must still upload')

    def test_an_unreadable_selection_fails_open_and_uploads(self):
        self.served = None
        result = AG.upload_and_select_poster(
            'guid-blip', COVER, 'test', state=self._state(self.CONTAINER))
        self.assertTrue(result)
        self.assertEqual(len(self.posts), 1,
                         'could-not-tell must behave exactly as before, not skip')

    def test_a_padded_copy_of_the_selection_is_not_uploaded_either(self):
        # Albums touched before v1.3.112 wear image+RESELECT_PAD. That is still
        # the same picture, so it is still a duplicate.
        self.served = COVER + PAD
        result = AG.upload_and_select_poster(
            'guid-padded', COVER, 'test', state=self._state(self.CONTAINER))
        self.assertTrue(result)
        self.assertEqual(self.posts, [])

    def test_no_selection_at_all_still_uploads_without_a_read(self):
        # The birth case: nothing selected, nothing to compare against.
        result = AG.upload_and_select_poster(
            'guid-new', COVER, 'test', state=self._state(None))
        self.assertTrue(result)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.reads, [], 'no selection means no read to make')


class PrintJacketSelectionIsCorrected(unittest.TestCase):
    """
    A print-jacket cover.jpg that won a fresh scan must not be permanent.

    The portrait deferral decides cover.jpg is a print jacket and declines to
    make it the default -- but the posters CONTAINER only wins on a fresh scan,
    so when the square online cover was not yet available at scan time (an
    unresolved match, a CDN blip) the print jacket is selected and no later
    refresh can move it. Measured live 2026-07-25: 3 of 1403 albums (Enemy of
    the State, The Ghost, Extraction) were frozen on a portrait while their own
    container already held a square 2400x2400. The Ghost's own UPLOAD was the
    portrait, proving the deferral had not fired on the pass that selected it.

    Fixing it needs the upload lever, which CAN move a persisted selection.

    This one FAILS CLOSED, the opposite of the duplicate guard above: it
    OVERRIDES a selection, so "could not read it" must never license a write.
    Only a positive byte-match against the measured print jacket may act.
    """

    def setUp(self):
        self.posts = []
        self.real = AG.HTTP.Request
        self.real_state = AG.read_poster_state
        self.served = COVER          # what the selected poster returns
        self.selected = ('metadata://posters/com.plexapp.agents.incipit_'
                         '124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc')

        def router(url, **kwargs):
            if kwargs.get('data') is not None:
                self.posts.append(kwargs['data'])

                class Posted(object):
                    content = 'ok'
                return Posted()
            if self.served is None:
                raise IOError('poster read failed')

            class Fetched(object):
                content = self.served
            return Fetched()

        AG.HTTP.Request = router
        AG.read_poster_state = lambda guid, tag: ('101', self.selected, [], None)
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.read_poster_state = self.real_state
        AG.recent_work_memo.clear()

    def _helper(self):
        class FakeHelper(object):
            class metadata(object):
                guid = 'guid-portrait'
        return FakeHelper()

    def test_a_print_jacket_selection_is_replaced_by_the_square(self):
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(len(self.posts), 1, 'the square must be uploaded + selected')
        self.assertEqual(self.posts[0], ARTIST, 'it must post the SQUARE, not the jacket')

    def test_our_own_upload_of_the_print_jacket_is_also_corrected(self):
        # The Ghost: the agent had itself uploaded the portrait, so the
        # selection is an upload:// carrying the jacket's own sha.
        sha, _p, _b = AG.padded_variants(COVER)
        self.selected = 'upload://posters/' + sha
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(len(self.posts), 1)

    def test_a_foreign_upload_is_never_touched(self):
        # A poster the operator uploaded by hand. Not ours to override.
        self.selected = 'upload://posters/somebodyelsesposter'
        self.served = b'\xff\xd8\xff\xe0 a poster the operator chose'
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(self.posts, [])

    def test_an_unreadable_selection_fails_closed(self):
        # Cannot prove the selection is the print jacket -> must not overwrite.
        self.served = None
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(self.posts, [], 'a blip must never license an override')

    def test_a_selection_that_is_not_the_print_jacket_is_left_alone(self):
        # Somebody already moved this book to a different agent poster.
        self.served = b'\xff\xd8\xff\xe0 some other agent cover'
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(self.posts, [])

    def test_nothing_happens_without_a_square_to_offer(self):
        # The McKinty case: every cover we hold is a jacket. Leave it be.
        AG.correct_portrait_selection(self._helper(), COVER, None)
        self.assertEqual(self.posts, [])

    def test_an_unreadable_poster_state_does_nothing(self):
        AG.read_poster_state = lambda guid, tag: None
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(self.posts, [])
