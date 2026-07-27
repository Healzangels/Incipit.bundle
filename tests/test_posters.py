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
import struct
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

    v1.3.120 then narrowed it to refuse only the deferred-to online default, on
    the reasoning that the agent auto-selects nothing else. v1.3.121 removed the
    refusal outright: the only file it could protect is one the agent had itself
    measured as a print jacket and already refused to display, so preserving it
    left cover.jpg an unfaithful mirror and the book re-deciding on every scan.
    The flag is gone with it -- backup_selected_poster now takes only `helper`,
    and whatever Plex shows is what reaches disk.

    This is the third repair path that flag silently suppressed (poison repair
    in v1.3.108, then the mirror twice) -- the standing lesson is to narrow such
    a gate rather than widen what it covers.

    These tests run in CURATE mode (declared in setUp): replacement mirroring is
    the curation-session behaviour, and since v1.3.125 the default mode is
    seed-only, where none of these writes may happen at all (CoverMirrorModes
    pins that side).
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
        self.online_fetches = []
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
        # Records instead of returning: backup_selected_poster must no longer
        # call this at all (v1.3.121 removed the per-refresh CDN fetch that
        # existed only to tell a pick from the deferred-to default). A stub
        # that merely returned a value could not tell us the call was gone.
        AG.fetch_url_bytes = (
            lambda url: outer.online_fetches.append(url) or outer.online_bytes)
        AG.Prefs['cover_mirror_mode'] = (
            'Curation (the selected poster replaces cover.jpg)')
        # F13 curated-file guard: no poster state -> the guard stands aside,
        # which is this book's real shape (a deferred portrait was never
        # uploaded, so there is no upload key to protect).
        AG.read_poster_state = lambda guid, tag: None

    def tearDown(self):
        (AG.HTTP.Request, AG.Core.storage.load, AG.write_cover_sidecar,
         AG.fetch_url_bytes, AG.read_poster_state) = self.saved
        AG.recent_work_memo.clear()
        AG.Prefs.pop('cover_mirror_mode', None)

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
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1, 'the pick must reach cover.jpg')
        self.assertEqual(self.writes[0][1], self.PICKED)

    def test_the_deferred_default_IS_mirrored_over_the_print_jacket(self):
        # REVERSED in v1.3.121, deliberately -- this test used to assert the
        # opposite. The refusal protected "the operator's file", but the only
        # file it ever protected is one the agent had just POSITIVELY MEASURED
        # as a print jacket (that is what portrait_deferred means) and had
        # already refused to display. Keeping it made cover.jpg an unfaithful
        # mirror precisely where the agent judged the file wrong, and left the
        # book depending on the deferral firing on every future scan instead of
        # being settled on disk.
        #
        # Measured live on Douglas Preston / "Extraction" (2026-07-25): the
        # portrait-fix correctly force-selected the square 2400x2400, and this
        # refusal then left cover.jpg as the 31,820-byte jacket.
        #
        # Safe because it is self-limiting: it can only ever replace a portrait
        # file with the square the agent preferred. A book whose cover.jpg is
        # square never reaches here at all (verified against Brandon Sanderson /
        # "The Sunlit Man", whose hand-uploaded 1500x1500 is already on disk).
        self.selected_bytes = self.ONLINE
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1,
                         'the square we deferred TO belongs on disk')
        self.assertEqual(self.writes[0][1], self.ONLINE)

    def test_the_per_refresh_cdn_fetch_is_gone(self):
        # The old code fetched the online cover on EVERY portrait book just to
        # tell a pick from the default, and failed closed when it could not.
        # Both selections now mirror, so the question is gone -- and so is the
        # fetch. Asserting the call count, not just the outcome: re-introducing
        # an unconditional fetch whose result is discarded would leave every
        # other test in this class green.
        self.online_bytes = None
        AG.backup_selected_poster(self._helper())
        self.assertEqual(self.online_fetches, [],
                         'backup_selected_poster must not fetch helper.thumb')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][1], self.PICKED)

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
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1, 'the pick must still reach cover.jpg')
        self.assertEqual(self.writes[0][1], self.PICKED)


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


def _jpeg(width, height):
    return (b'\xff\xd8'
            + b'\xff\xe0' + struct.pack('>H', 16)
            + b'JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00'
            + b'\xff\xc0' + struct.pack('>H', 17) + b'\x08'
            + struct.pack('>HH', height, width)
            + b'\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01')


def _png(width, height):
    return (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR'
            + struct.pack('>II', width, height) + b'\x08\x02\x00\x00\x00')


def _bmp(width, height):
    return (b'BM' + struct.pack('<I', 100) + b'\x00' * 4 + struct.pack('<I', 54)
            + struct.pack('<I', 40) + struct.pack('<ii', width, height)
            + b'\x01\x00\x18\x00' + b'\x00' * 24)


def _riff(chunk):
    return b'RIFF' + struct.pack('<I', 4 + len(chunk)) + b'WEBP' + chunk


def _webp_lossy(width, height):
    body = b'\x00\x00\x00' + b'\x9d\x01\x2a' + struct.pack('<HH', width, height)
    return _riff(b'VP8 ' + struct.pack('<I', len(body)) + body)


def _webp_lossless(width, height):
    packed = (width - 1) | ((height - 1) << 14)
    body = b'\x2f' + struct.pack('<I', packed)
    return _riff(b'VP8L' + struct.pack('<I', len(body)) + body)


def _three(value):
    return struct.pack('<I', value)[:3]


def _webp_extended(width, height):
    body = b'\x00' * 4 + _three(width - 1) + _three(height - 1)
    return _riff(b'VP8X' + struct.pack('<I', len(body)) + body)


class ImageDimensionsFormats(unittest.TestCase):
    """
    Measuring covers must not depend on the file being a JPEG.

    image_dimensions handled JPEG and PNG only, and every other format fell
    through to None -- which local_cover_is_portrait reads as "not portrait",
    because an unmeasurable image must never be treated as a confident yes. The
    result is a SILENT failure of the portrait guard: a print-jacket cover.jpg
    in any other format is defaulted to, deferring never fires, and the book
    freezes on it exactly as Extraction did.

    Not hypothetical -- measured live 2026-07-25, three selected posters in the
    library were already non-JPEG: WebP extended (Cujo, 1080x1080), WebP lossy
    (The Return of the King, 760x760) and BMP (City of Endless Night, 300x300).

    These tests can exist at all only because the parser now uses byte idioms
    that behave the same in Python 2.7 and Python 3. The old `data[:2] !=
    '\\xff\\xd8'` and `ord(data[i])` compare bytes to str under py3, so the
    function returned None for EVERY image in this harness and could not be
    tested -- a green suite proved nothing about it.
    """

    def test_jpeg(self):
        self.assertEqual(AG.image_dimensions(_jpeg(510, 680)), (510, 680))

    def test_png(self):
        self.assertEqual(AG.image_dimensions(_png(1400, 1400)), (1400, 1400))

    def test_bmp(self):
        # City of Endless Night's live poster.
        self.assertEqual(AG.image_dimensions(_bmp(300, 300)), (300, 300))

    def test_bmp_with_a_negative_height_is_top_down_not_upside_down(self):
        # A negative height means top-down row order, not a negative size.
        self.assertEqual(AG.image_dimensions(_bmp(300, -420)), (300, 420))

    def test_webp_lossy(self):
        # The Return of the King's live poster.
        self.assertEqual(AG.image_dimensions(_webp_lossy(760, 760)), (760, 760))

    def test_webp_lossless(self):
        self.assertEqual(AG.image_dimensions(_webp_lossless(500, 500)), (500, 500))

    def test_webp_extended(self):
        # Cujo's live poster.
        self.assertEqual(AG.image_dimensions(_webp_extended(1080, 1080)), (1080, 1080))

    def test_garbage_is_still_none(self):
        self.assertIsNone(AG.image_dimensions(b'not an image at all'))
        self.assertIsNone(AG.image_dimensions(b''))
        self.assertIsNone(AG.image_dimensions(b'RIFF____WEBPnope'))

    def test_a_portrait_webp_cover_is_now_recognised(self):
        # THE POINT. Before this, a portrait WebP measured as None -> "not
        # portrait" -> the print jacket became the default with no warning.
        self.assertTrue(AG.local_cover_is_portrait(_webp_lossy(280, 420)))
        self.assertTrue(AG.local_cover_is_portrait(_bmp(510, 680)))

    def test_a_square_cover_in_any_format_is_not_portrait(self):
        self.assertFalse(AG.local_cover_is_portrait(_webp_extended(1080, 1080)))
        self.assertFalse(AG.local_cover_is_portrait(_bmp(300, 300)))
        self.assertFalse(AG.local_cover_is_portrait(_jpeg(1500, 1500)))


def _bmp_core(width, height):
    """An OS/2 v1 BMP: 12-byte BITMAPCOREHEADER, 16-bit dimensions at 18/20."""
    return (b'BM' + struct.pack('<I', 100) + b'\x00' * 4 + struct.pack('<I', 26)
            + struct.pack('<I', 12) + struct.pack('<HH', width, height)
            + b'\x01\x00\x18\x00' + b'\x00' * 24)


class ImageDimensionsStrictness(unittest.TestCase):
    """
    A confidently WRONG size is worse than None, for every format.

    image_dimensions' JPEG walk is deliberately strict on that reasoning, and
    the WebP branches check their sync/signature bytes for it. BMP shipped
    without the equivalent check: it read a 32-bit width/height at offset 18
    unconditionally, which is only the BITMAPINFOHEADER layout. An OS/2
    BITMAPCOREHEADER stores 16-bit dimensions at 18/20, so those files were
    measured as garbage rather than refused -- 510x680 came back as
    (44564990, 1572865).

    That is not merely a miss. better_square_portrait scores candidates on
    min(width, height), so a short edge in the millions beats every real image
    and such a file would win the author square-tile contest outright, and
    local_cover_is_portrait would call a print jacket landscape.
    """

    def test_an_os2_core_header_bmp_is_measured_correctly(self):
        self.assertEqual(AG.image_dimensions(_bmp_core(510, 680)), (510, 680))

    def test_an_os2_core_header_portrait_is_recognised_as_portrait(self):
        self.assertTrue(AG.local_cover_is_portrait(_bmp_core(510, 680)))

    def test_an_unknown_dib_header_size_is_refused_not_guessed(self):
        # A DIB size that is neither the 12-byte core nor a >=40-byte info
        # header is a layout we cannot place -- None, never a guess.
        odd = (b'BM' + struct.pack('<I', 100) + b'\x00' * 4 + struct.pack('<I', 26)
               + struct.pack('<I', 16) + b'\x00' * 40)
        self.assertIsNone(AG.image_dimensions(odd))

    def test_a_truncated_bmp_is_refused(self):
        self.assertIsNone(AG.image_dimensions(b'BM' + b'\x00' * 8))

    def test_a_webp_with_a_bad_sync_code_is_refused(self):
        # An HTML error body or an ALPH/ANIM-first WebP must not yield numbers.
        bad = bytearray(_webp_lossy(760, 760))
        bad[23:26] = b'\x00\x00\x00'
        self.assertIsNone(AG.image_dimensions(bytes(bad)))

    def test_a_webp_lossless_with_a_bad_signature_is_refused(self):
        bad = bytearray(_webp_lossless(500, 500))
        bad[20:21] = b'\x00'
        self.assertIsNone(AG.image_dimensions(bytes(bad)))

    def test_a_truncated_webp_chunk_is_refused(self):
        self.assertIsNone(AG.image_dimensions(_webp_lossy(760, 760)[:24]))
        self.assertIsNone(AG.image_dimensions(_webp_extended(1080, 1080)[:26]))


class PortraitFixCollapsesPerTrack(unittest.TestCase):
    """
    The portrait fix must cost nothing on tracks 2..N, and nothing when there is
    no selection to move off.

    Plex calls update() once per TRACK. Every step in correct_portrait_selection
    is a round trip -- read_poster_state is two, and selected_poster_bytes pulls
    a whole poster -- so asking the memo AFTER them made a 27-part book pay 54
    localhost GETs and 27 full-poster downloads per refresh to reach the answer
    track 1 already had. Its two siblings (select_local_cover, converge_author_art)
    both consult should_run first; this one did not, because the only mark_done
    for the tag lived inside upload_and_select_poster, which tracks 2..27 never
    reach when nothing needs fixing.
    """

    def setUp(self):
        self.posts = []
        self.reads = []
        self.states = []
        self.real = AG.HTTP.Request
        self.real_state = AG.read_poster_state
        self.selected = ('metadata://posters/com.plexapp.agents.incipit_'
                         '124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc')
        self.served = COVER
        outer = self

        def router(url, **kwargs):
            if kwargs.get('data') is not None:
                outer.posts.append(kwargs['data'])

                class Posted(object):
                    content = 'ok'
                return Posted()
            outer.reads.append(url)

            class Fetched(object):
                content = outer.served
            return Fetched()

        def state(guid, tag):
            outer.states.append(tag)
            return ('101', outer.selected, [], None)

        AG.HTTP.Request = router
        AG.read_poster_state = state
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.read_poster_state = self.real_state
        AG.recent_work_memo.clear()

    def _helper(self):
        class FakeHelper(object):
            class metadata(object):
                guid = 'guid-tracks'
        return FakeHelper()

    def test_a_second_track_does_no_io_at_all(self):
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        first_states, first_reads = len(self.states), len(self.reads)
        self.assertEqual(len(self.posts), 1)
        for unused in range(26):
            AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(len(self.states), first_states,
                         'tracks 2..27 must not re-read poster state')
        self.assertEqual(len(self.reads), first_reads,
                         'tracks 2..27 must not re-download the poster')
        self.assertEqual(len(self.posts), 1, 'one upload for the whole book')

    def test_a_replaced_cover_re_runs_immediately(self):
        # The memo is keyed on the cover sha, not the guid, so dropping a NEW
        # cover.jpg is not suppressed by the previous pass's entry.
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(len(self.posts), 1)
        replaced = b'\xff\xd8\xff\xe0 a different jacket entirely'
        self.served = replaced
        AG.correct_portrait_selection(self._helper(), replaced, ARTIST)
        self.assertEqual(len(self.posts), 2)

    def test_no_selection_is_not_reported_as_a_failed_read(self):
        # A book with nothing selected is healthy and has no jacket to move off.
        # It must not download anything, and must not log a read failure.
        self.selected = None
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        self.assertEqual(self.reads, [], 'nothing selected means nothing to read')
        self.assertEqual(self.posts, [])

    def test_the_selected_bytes_are_read_only_once(self):
        # correct_portrait_selection proves the selection IS the jacket, then
        # hands those bytes to upload_and_select_poster, whose duplicate guard
        # would otherwise pull the identical poster a second time.
        AG.correct_portrait_selection(self._helper(), COVER, ARTIST)
        poster_reads = [u for u in self.reads if '/file?url=' in u]
        self.assertEqual(len(poster_reads), 1,
                         'the selected poster must not be downloaded twice')


class AuthorArtIsActuallyMeasured(unittest.TestCase):
    """
    The author-art path must hand image_dimensions BYTES, not Plex's wrapper.

    make_request returns the lazy HTTP.Request object; only fetch_url_bytes
    unwraps it via .content. Both artist call sites passed the wrapper straight
    to image_dimensions, whose `data[:8]` raised TypeError into the outer except
    and became None -- so thumb_dims and secondary_dims were ALWAYS None.

    Confirmed live 2026-07-25, once per artist refresh:
        incipit cover: could not measure local cover
        ('HTTPRequest' object has no attribute '__getitem__')

    Consequences, both silent: the container reorder that puts the better
    square-tile fit last (v1.3.118) could never fire, and the opt-in
    prefer_square_author_art pref was inert because select_best_fit_author_art
    is handed the same two Nones. The unit tests missed it because they pass raw
    bytes directly to better_square_portrait, never through the fetch.
    """

    def setUp(self):
        self.real_fetch = AG.fetch_url_bytes
        self.real_request = AG.make_request
        self.media = []
        outer = self
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900)

        class Wrapper(object):
            """What make_request really returns: no __getitem__."""
            content = _jpeg(900, 900)

        AG.make_request = lambda url, cache_time=None: Wrapper()
        real_media = AG.Proxy.Media
        AG.Proxy.Media = lambda data, **kw: outer.media.append(data) or ('media', 0)
        self.real_media = real_media

    def tearDown(self):
        AG.fetch_url_bytes = self.real_fetch
        AG.make_request = self.real_request
        AG.Proxy.Media = self.real_media

    def _helper(self):
        class FakePosters(dict):
            def validate_keys(self, keys):
                self.validated = keys

        class FakeHelper(object):
            thumb = 'https://hardcover/portrait.jpg'
            thumb_secondary = 'https://audible/photo.jpg'
            force = True

            class metadata(object):
                posters = FakePosters()
        return FakeHelper()

    def test_the_secondary_poster_is_measured_not_swallowed(self):
        posters, dims = AG.offer_secondary_author_poster(self._helper(), [])
        self.assertEqual(dims, (900, 900),
                         'dims must be a real measurement, not None')

    def test_what_reaches_proxy_media_is_bytes(self):
        # Proxy.Media(wrapper) is what made this survive unnoticed -- the poster
        # still appeared, only the measurement was lost.
        AG.offer_secondary_author_poster(self._helper(), [])
        self.assertTrue(self.media, 'a poster must still be offered')
        for data in self.media:
            self.assertIsInstance(data, bytes)


class CoverMirrorModes(unittest.TestCase):
    """
    The direction of truth for cover.jpg is DECLARED, never inferred.

    2026-07-26: a library rebuild overwrote 92 hand-curated cover.jpg files.
    Two writers were live -- backup_selected_poster (mirrors whatever is
    selected) and promote_picked_cover (writes an offered online cover over
    disk on the premise that "selection == offered cover" implies a person
    picked it). During a scan both premises are false: the SCAN is what makes
    selections, so automatic choices flowed over the operator's files. There
    are no backups of that share; the originals are gone.

    The fix is the Lambda.bundle pattern: one pref, cover_mirror_mode, declares
    which side wins.

      Off      -- never write the media folder.
      Seed     -- (default) write cover.jpg ONLY where none exists. Safe during
                  any scan; an existing file can never be replaced.
      Curation -- Plex is truth for this session: picks replace cover.jpg.
                  The mode a human turns on while actively choosing art.

    The asymmetry is the point: forgetting to enable Curation costs a re-refresh
    later; the old design's failure cost unrecoverable curated art.
    """

    ONLINE = b'\xff\xd8\xff\xe0 the online cover plex selected'
    CURATED = b'\xff\xd8\xff\xe0 the operator curated file'

    def setUp(self):
        AG.recent_work_memo.clear()
        self.writes = []
        self.reads = []
        self.existing = self.CURATED
        self.saved = (AG.HTTP.Request, AG.Core.storage.load, AG.write_cover_sidecar,
                      AG.make_request, AG.read_poster_state)
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
                outer.reads.append(url)
                reply.content = outer.ONLINE
            return reply

        class OfferedResponse(object):
            content = self.ONLINE

        AG.HTTP.Request = router
        AG.Core.storage.load = lambda path: outer.existing
        AG.write_cover_sidecar = (
            lambda path, data: outer.writes.append((path, data)) or True)
        AG.make_request = lambda url, cache_time=None: OfferedResponse()
        AG.read_poster_state = lambda guid, tag: None

    def tearDown(self):
        (AG.HTTP.Request, AG.Core.storage.load, AG.write_cover_sidecar,
         AG.make_request, AG.read_poster_state) = self.saved
        AG.Prefs.pop('cover_mirror_mode', None)
        AG.recent_work_memo.clear()

    def _helper(self, guid='com.plexapp.agents.incipit://MODETEST_us'):
        class FakeMetadata(object):
            pass

        class FakeHelper(object):
            metadata = FakeMetadata()
            thumb = 'https://images.example/online-cover.jpg'
            thumb_secondary = None
            force = True

            def album_file_path(self):
                return '/data/media/x/1 - Book/file.m4b'

        FakeHelper.metadata.guid = guid
        return FakeHelper()

    # --- the default: seed only ---

    def test_default_mode_is_seed(self):
        self.assertEqual(AG.cover_mirror_mode(), 'seed')

    def test_seed_never_replaces_an_existing_file(self):
        AG.backup_selected_poster(self._helper())
        self.assertEqual(self.writes, [],
                         'an automatic selection must not replace curated art')

    def test_seed_refusal_skips_the_poster_download_entirely(self):
        # The refusal is decidable from the file's existence alone, so paying
        # for the selected poster's bytes first would be pure waste per track.
        AG.backup_selected_poster(self._helper())
        self.assertEqual(self.reads, [])

    def test_seed_still_writes_where_no_cover_exists(self):
        self.existing = None
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1, 'seeding absent covers must survive')
        self.assertEqual(self.writes[0][1], self.ONLINE)

    def test_promote_never_runs_outside_curation(self):
        # THE RUN-1 WRITER. Its premise -- selection matches an offered online
        # cover, therefore a person picked it -- is false during a scan.
        AG.Prefs['prefer_local_cover'] = True
        AG.promote_picked_cover(self._helper())
        self.assertEqual(self.writes, [])

    # --- off ---

    def test_off_does_nothing_at_all(self):
        AG.Prefs['cover_mirror_mode'] = 'Off'
        AG.backup_selected_poster(self._helper())
        AG.promote_picked_cover(self._helper())
        self.assertEqual(self.writes, [])

    # --- curation ---

    def test_curation_replaces_the_file(self):
        AG.Prefs['cover_mirror_mode'] = (
            'Curation (the selected poster replaces cover.jpg)')
        AG.backup_selected_poster(self._helper())
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0][1], self.ONLINE)

    def test_curation_lets_promote_write(self):
        AG.Prefs['cover_mirror_mode'] = (
            'Curation (the selected poster replaces cover.jpg)')
        AG.Prefs['prefer_local_cover'] = True
        AG.promote_picked_cover(self._helper())
        self.assertEqual(len(self.writes), 1)

    # --- resolver robustness ---

    def test_unknown_pref_values_resolve_to_seed(self):
        for weird in (None, True, False, '', 'banana', 0):
            AG.Prefs['cover_mirror_mode'] = weird
            self.assertEqual(AG.cover_mirror_mode(), 'seed',
                             'unknown value %r must fail SAFE' % (weird,))


class AuthorArtOrderingIsStable(unittest.TestCase):
    """
    The square-fit ordering must survive passes that do not re-fetch images.

    Dimensions were only measured on the pass that FETCHED an image. The artist
    update runs once per album, and on every later pass the images are already
    in the container, so the dims came back None, the reorder condition failed,
    and validate_keys re-ran in DEFAULT order -- undoing the correct ordering
    from the first pass. The last pass wins, so any author with several books
    always ended on the default (Audible) image.

    Verified live 2026-07-26 on Bryce O'Connor: Hardcover 820x820 vs Audible
    1000x1500 -- the rule picks the square unambiguously, yet the Audible tall
    was selected. Fix: dimensions are remembered per URL, so every pass can
    reproduce the same ordering.
    """

    def setUp(self):
        AG.IMAGE_DIMS_MEMO.clear()
        self.real_fetch = AG.fetch_url_bytes
        self.real_media = AG.Proxy.Media
        AG.fetch_url_bytes = lambda url: _jpeg(1000, 1500)
        AG.Proxy.Media = lambda data, **kw: ('media', 0)

    def tearDown(self):
        AG.fetch_url_bytes = self.real_fetch
        AG.Proxy.Media = self.real_media
        AG.IMAGE_DIMS_MEMO.clear()

    def _helper(self, force=False, preloaded=False):
        class FakePosters(dict):
            pass

        class FakeHelper(object):
            thumb = 'https://hardcover/portrait.jpg'
            thumb_secondary = 'https://audible/photo.jpg'

        FakeHelper.force = force
        FakeHelper.metadata = type('M', (object,), {'posters': FakePosters()})()
        if preloaded:
            FakeHelper.metadata.posters[FakeHelper.thumb_secondary] = ('media', 0)
        return FakeHelper()

    def test_first_pass_measures_and_remembers(self):
        posters, dims = AG.offer_secondary_author_poster(self._helper(), [])
        self.assertEqual(dims, (1000, 1500))
        self.assertEqual(AG.IMAGE_DIMS_MEMO.get('https://audible/photo.jpg'),
                         (1000, 1500))

    def test_a_later_pass_recalls_dims_without_fetching(self):
        AG.offer_secondary_author_poster(self._helper(), [])
        fetches = []
        AG.fetch_url_bytes = lambda url: fetches.append(url)
        posters, dims = AG.offer_secondary_author_poster(
            self._helper(preloaded=True), [])
        self.assertEqual(dims, (1000, 1500),
                         'pass 2 must know the dims or the ordering reverts')
        self.assertEqual(fetches, [], 'and must not pay a re-fetch for them')

    def test_remember_dims_ignores_unmeasurable_bytes(self):
        self.assertIsNone(AG.remember_dims('https://x/y.jpg', b'not an image'))
        self.assertNotIn('https://x/y.jpg', AG.IMAGE_DIMS_MEMO)


class SquareTieBandCalibration(unittest.TestCase):
    """
    The tie band decides when SQUARENESS may beat raw resolution, and 0.75 was
    too narrow for real provider art: Callie Hart's Hardcover square is 400x400
    against an Audible 576x768 portrait selfie -- 400/576 = 0.69, just outside
    the old band, so resolution won and the square professional photo lost.
    Verified live 2026-07-26 (the operator flagged the outcome as wrong).

    At 0.5 the square wins whenever it has at least HALF the tall image's short
    edge, which matches how these render in a square tile: a centre-crop of a
    3:4 portrait loses the top of the head, while a modest square stays a face.
    Glen Cook's guard case (117px thumbnail vs 3072x2304, ratio 0.05) still
    resolves to resolution, so the postage-stamp regression stays impossible.
    """

    def test_callie_hart_square_now_wins(self):
        self.assertIs(AG.better_square_portrait((400, 400), (576, 768))[0], 400)

    def test_bryce_oconnor_square_still_wins(self):
        self.assertEqual(AG.better_square_portrait((820, 820), (1000, 1500)),
                         (820, 820))

    def test_glen_cook_thumbnail_still_loses(self):
        self.assertEqual(AG.better_square_portrait((117, 150), (3072, 2304)),
                         (3072, 2304))


if __name__ == '__main__':
    unittest.main()


class TestDuplicateShownElsewhere(unittest.TestCase):
    """
        v1.3.133: the same picture must not be LISTED twice however many
        sources hold it, while a unique alternative is never hidden (operator
        rule, 2026-07-26). duplicate_shown_elsewhere is the predicate: True
        when a NON-incipit container poster (an upload, Local Media Assets)
        already displays the bytes we are about to offer.

        Rails, each with a test:
          * no state / no selection yet -> False (a fresh scan needs our key
            offered so it can be SELECTED -- the container is the only
            selection mechanism at that point);
          * our own key is the selection -> False (pruning the selected key is
            the picked-poster-evaporates failure);
          * fetch failures -> False (fail-open: a duplicate tile is cosmetic,
            a missing poster option is not).
    """

    IMG = b'\xff\xd8IMAGEBYTES'
    OTHER = b'\xff\xd8DIFFERENT'
    LOCAL_KEY = 'incipit-local-cover'
    OWN = 'metadata://posters/com.plexapp.agents.incipit_124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc'
    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_9999888877776666555544443333222211110000'
    ONLINE = 'metadata://posters/com.plexapp.agents.incipit_ffffeeeeddddccccbbbbaaaa99998888777766'

    def setUp(self):
        self.real_pfb = AG.poster_file_bytes
        self.store = {}
        AG.poster_file_bytes = lambda rk, key, tag: self.store.get(key)

    def tearDown(self):
        AG.poster_file_bytes = self.real_pfb

    def state(self, selected, keys):
        return ('101', selected, keys, None)

    def test_own_container_key_matches_the_live_local_cover_key(self):
        # The real-world anchor: sha1('incipit-local-cover') is the container
        # key every local-cover selection has used since 1.3.31.
        self.assertEqual(AG.own_container_key(self.LOCAL_KEY), self.OWN)

    def test_no_state_offers_as_always(self):
        self.assertFalse(AG.duplicate_shown_elsewhere(
            None, self.IMG, self.LOCAL_KEY, 't'))

    def test_fresh_scan_no_selection_offers_as_always(self):
        self.store[self.UPLOAD] = self.IMG
        st = self.state(None, [self.OWN, self.UPLOAD])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_own_key_selected_offers_as_always(self):
        # Even with an identical copy elsewhere: never undercut the selection.
        self.store[self.UPLOAD] = self.IMG
        st = self.state(self.OWN, [self.OWN, self.UPLOAD])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_identical_selected_upload_skips_our_copy(self):
        self.store[self.UPLOAD] = self.IMG
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        self.assertTrue(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_identical_lma_copy_skips_ours_even_when_unselected(self):
        # Selection is our ONLINE cover (a different incipit key), and Local
        # Media Assets holds a byte-identical copy of cover.jpg: our mirror
        # adds nothing the picker doesn't already show.
        self.store[self.LMA] = self.IMG
        st = self.state(self.ONLINE, [self.ONLINE, self.OWN, self.LMA])
        self.assertTrue(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_unique_alternatives_always_offered(self):
        self.store[self.UPLOAD] = self.OTHER
        self.store[self.LMA] = self.OTHER
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD, self.LMA])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_incipit_keys_are_not_comparison_sources(self):
        # Intra-agent duplication is handled where the images are OFFERED (the
        # online-vs-local guard); this predicate only looks across sources.
        self.store[self.ONLINE] = self.IMG
        st = self.state(self.UPLOAD, [self.ONLINE, self.OWN, self.UPLOAD])
        self.store[self.UPLOAD] = self.OTHER
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_fetch_failure_fails_open(self):
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        # store empty -> poster_file_bytes returns None
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_padded_reselect_copy_still_counts_as_the_same_picture(self):
        padded = self.IMG + AG.RESELECT_PAD
        self.store[self.UPLOAD] = padded
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        self.assertTrue(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))


class TestSandboxBuiltinGuards(unittest.TestCase):
    """
        The sandbox's builtin whitelist is IRREGULAR (no any/sum, but set()
        works) and py_compile + this py3 harness both pass code that dies at
        runtime in Plex. v1.3.133 shipped `isinstance(x, bytes)` and every
        album update crashed with "global name 'bytes' is not defined" --
        found only in the live CRITICAL traceback. No harness can catch a
        whitelist miss, so pin the SOURCE: builtins without in-repo precedent
        must not appear as bare names.
    """

    def code_sources(self):
        code_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code'
        )
        out = []
        for name in sorted(os.listdir(code_dir)):
            if name.endswith('.py'):
                with open(os.path.join(code_dir, name)) as f:
                    out.append((name, f.read()))
        return out

    def test_no_bare_bytes_builtin(self):
        # `image_bytes`/`cover_bytes` etc. are fine -- only the bare NAME is
        # forbidden (isinstance second args, bytes(...) calls).
        import re as re_mod
        for name, src in self.code_sources():
            self.assertIsNone(
                re_mod.search(r'isinstance\([^)]*[,(]\s*bytes\s*[),]', src),
                '%s uses the bytes builtin, absent from the sandbox whitelist' % name
            )
            self.assertIsNone(
                re_mod.search(r'(?<![A-Za-z0-9_])bytes\(', src),
                '%s calls bytes(), absent from the sandbox whitelist' % name
            )
