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

    `prefer_square_author_art` force-selects the better fit on an
    already-scanned artist. ON by default since v1.3.132 (operator decision:
    the ownership gate already refuses user uploads, so the only overridable
    class is the agent's own two provider images). Because the default-on
    elif now swallows every two-image author in update(), the function must
    REPORT whether it formed a verdict -- a silent no-op made the unpin
    fallback unreachable for exactly the stuck-pin class it exists to heal.
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

    def test_a_verdict_is_reported_to_the_caller(self):
        self.assertTrue(AG.select_best_fit_author_art(
            self._helper(), (270, 270), (211, 250)))

    def test_no_evidence_is_reported_as_no_verdict(self):
        # The caller needs to KNOW nothing was decided, so the pre-1.3.132
        # remedy (unpin a stuck agent pin) can still run on the no-opinion
        # paths. A silent no-op left the pin stuck forever.
        self.assertFalse(AG.select_best_fit_author_art(
            self._helper(), None, None))
        self.assertFalse(AG.select_best_fit_author_art(
            self._helper(), (500, 500), (500, 500)))
        helper = self._helper()
        helper.thumb_secondary = ''
        self.assertFalse(AG.select_best_fit_author_art(
            helper, (270, 270), (211, 250)))

    def test_update_falls_through_to_unpin_when_no_verdict(self):
        # The elif in update() swallows every two-image author now that the
        # pref defaults ON; without this fall-through the unpin branch is
        # unreachable for them and a stuck agent-upload pin never reverts.
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', '__init__.py'
        )
        with open(path) as f:
            src = f.read()
        self.assertIn('if not select_best_fit_author_art(', src)


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
        # Two tests below flip this on; leaking it made every later-ordered
        # test in the process run with prefer_local defaults it never asked
        # for (order-dependent greens are how that class of bug hides).
        AG.Prefs.pop('prefer_local_cover', None)
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

    def test_no_unicode_error_names_in_except_clauses(self):
        # UnicodeDecodeError / UnicodeEncodeError have no in-repo whitelist
        # precedent -- the same class as the bytes kill, made worse by py2
        # evaluating except TUPLES lazily: a missing name becomes a NameError
        # at the exact moment the fallback should fire, aborting the whole
        # update on precisely the input the fallback exists to survive.
        # ValueError is their superclass (Unicode*Error < UnicodeError <
        # ValueError) and HAS precedent (json_decode) -- catch that instead.
        import re as re_mod
        for name, src in self.code_sources():
            self.assertIsNone(
                re_mod.search(r'except[^\n]*Unicode\w*Error', src),
                '%s catches a Unicode error by NAME; use ValueError' % name
            )

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

    def test_no_builtin_without_in_repo_precedent(self):
        # An ALLOWLIST, not a blocklist. `bytes` was banned by name after it
        # shipped, and the very next unlisted builtin still got through:
        # v1.3.154 used `frozenset(...)` at MODULE level in search_tools, so
        # the NameError fired at import and killed the entire plugin -- no
        # matching at all, evidenced only by a CRITICAL "Exception starting
        # plug-in" in the agent log. A module-level call is the worst case:
        # it cannot be reached by any test and it takes everything with it.
        #
        # Only builtins with existing in-repo precedent may appear. Adding
        # one here is a deliberate act that should be verified against a live
        # plugin load, not a harness.
        # Builtins the sandbox does NOT provide, despite looking ordinary.
        # any()/all()/sum() and getattr/dir are proven live; frozenset was
        # proven live on 2026-07-28; the rest share their shape.
        # Evidence-based, not a guess: `reduce` IS present (py2 builtin, and
        # sum_scores uses it precisely because sum() is not), so listing it
        # here would block working code.
        banned = set([
            'frozenset', 'bytes', 'bytearray', 'getattr', 'setattr', 'delattr',
            'hasattr', 'dir', 'any', 'all', 'sum', 'eval', 'exec', 'compile',
            'execfile', 'reload', 'memoryview',
        ])
        # Tokenize rather than grep: the first cut flagged the COMMENT that
        # documents this very rule ("not any(): the sandbox does not provide
        # any()/all()/sum()"). Only real NAME tokens immediately followed by
        # '(' count, and an attribute access (self.dir(...)) is not a builtin.
        import io
        import tokenize
        for name, src in self.code_sources():
            toks = [
                t for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type in (tokenize.NAME, tokenize.OP)
            ]
            for i, tok in enumerate(toks[:-1]):
                if tok.type != tokenize.NAME or tok.string not in banned:
                    continue
                nxt = toks[i + 1]
                if not (nxt.type == tokenize.OP and nxt.string == '('):
                    continue
                prev = toks[i - 1] if i else None
                if prev is not None and prev.type == tokenize.OP and prev.string == '.':
                    continue  # an attribute, not the builtin
                self.fail(
                    '%s line %d calls %s(), which the Plex sandbox does not '
                    'provide -- at MODULE level this kills the WHOLE plugin '
                    '(no matching at all, only a CRITICAL "Exception starting '
                    'plug-in" in the agent log)'
                    % (name, tok.start[0], tok.string)
                )

class OnlineCopyRedundancy(unittest.TestCase):
    """
        The online cover must not be offered (or kept) when it would just
        list cover.jpg's bytes a second time. Two ways those bytes are
        already on display: our local mirror took the default (local_set),
        or the mirror offer was WITHHELD because another source's poster
        shows them (mirror_skipped). v1.3.133 keyed the guard on local_set
        alone -- so the very pass that suppressed the local copy re-offered
        the identical ONLINE copy, re-creating the duplicate through the
        other key.
    """

    IMG = b'\xff\xd8SAMEBYTES'
    OTHER = b'\xff\xd8DIFFERENT'

    def test_redundant_when_local_mirror_took_the_default(self):
        self.assertTrue(AG.online_copy_is_redundant(self.IMG, self.IMG, True, False))

    def test_redundant_when_the_mirror_was_skipped_as_a_duplicate(self):
        # The v1.3.133 hole.
        self.assertTrue(AG.online_copy_is_redundant(self.IMG, self.IMG, False, True))

    def test_a_unique_online_cover_is_never_suppressed(self):
        self.assertFalse(AG.online_copy_is_redundant(self.OTHER, self.IMG, True, True))

    def test_missing_bytes_on_either_side_fail_open(self):
        self.assertFalse(AG.online_copy_is_redundant(None, self.IMG, True, True))
        self.assertFalse(AG.online_copy_is_redundant(self.IMG, None, True, True))

    def test_no_display_anywhere_means_no_suppression(self):
        # Neither flag: nothing shows these bytes, the offer must stand.
        self.assertFalse(AG.online_copy_is_redundant(self.IMG, self.IMG, False, False))

    def test_a_padded_copy_is_still_the_same_picture(self):
        self.assertTrue(AG.online_copy_is_redundant(self.IMG + PAD, self.IMG, True, False))


class AlbumCoverDecisionMemo(unittest.TestCase):
    """
        update() runs once per TRACK, and a dup-skip never adds our key to
        the container -- so the membership guard never engaged and every
        track of a curated album re-read cover.jpg, re-read the poster
        state, and re-downloaded up to DUPLICATE_CHECK_MAX_FETCHES container
        posters to reach the identical decision (a 27-part book: ~200 extra
        requests per pass). The memo carries the first track's flags to its
        siblings; the container itself survives between tracks.
    """

    FLAGS = {'local_set': True, 'mirror_skipped': False}

    def setUp(self):
        AG.album_cover_memo.clear()
        self.real_time = AG.time

    def tearDown(self):
        AG.time = self.real_time

    def test_miss_then_hit(self):
        self.assertIsNone(AG.album_cover_decision('guid-1', False))
        AG.remember_album_cover_decision('guid-1', False, self.FLAGS)
        self.assertEqual(AG.album_cover_decision('guid-1', False), self.FLAGS)

    def test_force_and_scan_passes_never_share_an_entry(self):
        # force PROMISES a freshly-read cover.jpg; a scan-pass decision must
        # not satisfy it, nor the other way around.
        AG.remember_album_cover_decision('guid-2', False, self.FLAGS)
        self.assertIsNone(AG.album_cover_decision('guid-2', True))
        AG.remember_album_cover_decision('guid-3', True, self.FLAGS)
        self.assertIsNone(AG.album_cover_decision('guid-3', False))

    def test_entries_expire(self):
        # Bounded staleness, same contract as artist_art_memo: an operator
        # who replaces cover.jpg and refreshes again gets a fresh read once
        # the window lapses (force passes are also keyed apart, above).
        AG.remember_album_cover_decision('guid-4', False, self.FLAGS)
        real = self.real_time
        AG.time = lambda: real() + AG.ALBUM_COVER_TTL + 1
        self.assertIsNone(AG.album_cover_decision('guid-4', False))


class CoverBlockFlowGuards(unittest.TestCase):
    """
        Container-flow facts only the SOURCE can witness (the cover block is
        inline in update(), driven by framework objects this harness cannot
        construct): the per-track memo is consulted, both online-offer sites
        route through online_copy_is_redundant, and a thumb-less record still
        prunes a skipped mirror's stale entry.
    """

    def source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', '__init__.py'
        )
        with open(path) as f:
            return f.read()

    def test_update_consults_the_album_cover_memo(self):
        src = self.source()
        self.assertIn('album_cover_decision(', src)
        self.assertIn('remember_album_cover_decision(', src)

    def test_the_album_memo_is_not_gated_on_prefer_local(self):
        # v1.3.152's cross-source leg computes the online flags with the pref
        # OFF too, so gating the memo on prefer_local (as v1.3.153 did) meant
        # a forced pass of a multi-file book re-fetched the online cover and
        # re-swept the container once per track -- and a sibling restored
        # with DEFAULT flags could resurrect the online entry track 1 pruned
        # (online_redundant=False keeps the thumb in the keep-list).
        src = self.source()
        self.assertNotIn('if prefer_local and remembered is None:', src,
                         'the memo write must run for both pref states')
        self.assertIn('if remembered is None:\n'
                      '            remember_album_cover_decision(', src)
        # The read side likewise: consulted before, not inside, the
        # prefer_local branch.
        self.assertIn('remembered = album_cover_decision('
                      'helper.metadata.guid, helper.force)\n'
                      '        if remembered is not None:', src)

    def SUPERSEDED_test_online_offer_and_keep_list_share_the_redundancy_verdict(self):
        # The predicate judges where the bytes are in hand (the offer), and
        # the keep-list plus the sibling-track restore reuse the STORED
        # verdict -- re-judging with no bytes would fail open and re-open
        # the S1 asymmetry.
        src = self.source()
        self.assertGreaterEqual(src.count('online_copy_is_redundant('), 2)
        self.assertIn('if online_redundant:', src)
        self.assertIn('online_redundant) = remembered', src)

    def test_a_thumbless_record_still_prunes_a_skipped_mirror(self):
        # Hardcover/OpenLibrary book-level matches have no online cover, so
        # they never enter the `if helper.thumb:` membership pass -- the
        # stale mirror entry they skipped must be pruned on its own branch.
        # Since the 2026-07-28 review that prune is gated on BYTE identity:
        # a perceptual verdict withholds the offer but must never delete the
        # operator's curated cover.jpg entry.
        src = self.source()
        self.assertIn('mirror_skipped and mirror_byte_exact', src)
        self.assertIn('and local_key in helper.metadata.posters', src)

    def test_the_online_prune_consults_the_selection(self):
        # The confirmed finding of the 2026-07-28 review: `keep = []` had no
        # selection rail, so a SELECTED online cover could be pruned.
        src = self.source()
        self.assertIn('online_prune_allowed(', src)


if __name__ == '__main__':
    unittest.main()

class ConvergePrunesItsOwnDuplicate(unittest.TestCase):
    """
        The instant converge_author_art's upload is selected, the agent's
        CONTAINER copy of the same image becomes a duplicate -- and the
        offer-time dedupe cannot see an upload that does not exist yet, so
        the picker showed the new picture twice for exactly one pass
        (measured live on Robert Harris, 2026-07-27; the next refresh's
        author-offer dedupe then withheld it). Prune where the knowledge
        is: right after the select lands. The OTHER image stays -- byte-
        identical shown once, unique alternatives never hidden -- and the
        selection is the upload:// key, so the prune cannot touch it.
    """

    TARGET = 'https://hardcover/new-portrait.jpg'
    OTHER = 'https://audible/photo.jpg'

    def setUp(self):
        AG.recent_work_memo.clear()
        self.saved = (AG.read_poster_state, AG.fetch_url_bytes,
                      AG.upload_and_select_poster)
        AG.read_poster_state = lambda guid, tag: (
            '101', 'metadata://posters/com.plexapp.agents.incipit_aaaa',
            ['metadata://posters/com.plexapp.agents.incipit_aaaa'], None)
        AG.fetch_url_bytes = lambda url: b'\xff\xd8 the converged image'
        self.validated = []

    def tearDown(self):
        (AG.read_poster_state, AG.fetch_url_bytes,
         AG.upload_and_select_poster) = self.saved
        AG.recent_work_memo.clear()

    def _helper(self, keys):
        outer = self

        class FakePosters(dict):
            def validate_keys(self, keep):
                outer.validated.append(list(keep))

        class FakeMetadata(object):
            guid = 'com.plexapp.agents.incipit://CONVTEST_us'
            posters = FakePosters()

        for k in keys:
            FakeMetadata.posters[k] = 'proxy'

        class FakeHelper(object):
            metadata = FakeMetadata()

        return FakeHelper()

    def test_a_successful_select_prunes_the_container_copy(self):
        AG.upload_and_select_poster = lambda *a, **kw: True
        helper = self._helper([self.TARGET, self.OTHER])
        AG.converge_author_art(
            helper, self.TARGET, self.OTHER, 'incipit author-art-fit')
        self.assertEqual(self.validated, [[self.OTHER]])

    def test_a_declined_select_prunes_nothing(self):
        # The stand-down paths (user upload, de-selection respected, spent
        # pad budget) leave the container exactly as it was.
        AG.upload_and_select_poster = lambda *a, **kw: False
        helper = self._helper([self.TARGET, self.OTHER])
        AG.converge_author_art(
            helper, self.TARGET, self.OTHER, 'incipit author-art-fit')
        self.assertEqual(self.validated, [])

    def test_no_container_copy_means_no_prune(self):
        AG.upload_and_select_poster = lambda *a, **kw: True
        helper = self._helper([self.OTHER])
        AG.converge_author_art(
            helper, self.TARGET, self.OTHER, 'incipit author-art-fit')
        self.assertEqual(self.validated, [])

class ConvergenceRunsBeforeTheOffers(unittest.TestCase):
    """
        Proven live on Ernest Cline (2026-07-27, v1.3.141): converge's own
        validate_keys prune logged success, but the framework serializes
        same-pass dict entries REGARDLESS of the valid-keys list -- the
        container copy the offer phase had already added survived, and the
        picker showed the just-selected image twice anyway.

        You cannot un-offer what this pass offered. So on a forced refresh
        the convergence must run FIRST: the offer phase's poster-state read
        then sees the fresh upload as the selection, the existing dedupe
        withholds the container copy of the same bytes, and the keep-list
        prunes the stored stale entry -- single-pass clean, all through
        machinery that is already live-proven (the Harris pass-2 trace).
    """

    def source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', '__init__.py'
        )
        with open(path) as f:
            return f.read()

    def test_force_select_precedes_the_offer_state_read(self):
        src = self.source()
        marker = src.index('author-art convergence runs BEFORE the offer')
        state_read = src.index('author_dup_state = read_poster_state')
        fit_call = src.index('if not select_best_fit_author_art(')
        self.assertLess(marker, state_read)
        self.assertLess(fit_call, state_read)

class IdenticalSecondaryIsNotAnAlternative(unittest.TestCase):
    """
        Measured live on Aleron Kong (2026-07-27): the API served the SAME
        picture as both image and imageAlt -- byte-identical (sha-equal,
        96516 bytes) under two different provider URLs -- so the artist tile
        listed it twice, permanently: the cross-source dedupe deliberately
        never compares the agent's own two images against each other, and
        nothing else did either. A copy of the primary is not an
        alternative: withhold it and keep it out of the membership list,
        exactly like the cross-source skip. Fails open without the primary's
        bytes in hand.
    """

    def setUp(self):
        self.real_fetch = AG.fetch_url_bytes
        self.media = []
        outer = self
        real_media = AG.Proxy.Media
        AG.Proxy.Media = lambda data, **kw: outer.media.append(data) or ('media', 0)
        self.real_media = real_media

    def tearDown(self):
        AG.fetch_url_bytes = self.real_fetch
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

    def test_a_byte_identical_secondary_is_withheld(self):
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900)
        helper = self._helper()
        posters, dims = AG.offer_secondary_author_poster(
            helper, [helper.thumb], thumb_data=_jpeg(900, 900))
        self.assertNotIn(helper.thumb_secondary, helper.metadata.posters)
        self.assertEqual(posters, [helper.thumb])
        # Still measured: the select machinery compares by URL bytes.
        self.assertEqual(dims, (900, 900))

    def test_a_padded_copy_counts_as_identical(self):
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900) + PAD
        helper = self._helper()
        posters, _ = AG.offer_secondary_author_poster(
            helper, [helper.thumb], thumb_data=_jpeg(900, 900))
        self.assertNotIn(helper.thumb_secondary, helper.metadata.posters)

    def test_a_different_secondary_is_still_offered(self):
        AG.fetch_url_bytes = lambda url: _jpeg(800, 600)
        helper = self._helper()
        posters, _ = AG.offer_secondary_author_poster(
            helper, [helper.thumb], thumb_data=_jpeg(900, 900))
        self.assertIn(helper.thumb_secondary, helper.metadata.posters)
        self.assertIn(helper.thumb_secondary, posters)

    def test_no_primary_bytes_fails_open(self):
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900)
        helper = self._helper()
        posters, _ = AG.offer_secondary_author_poster(
            helper, [helper.thumb], thumb_data=None)
        self.assertIn(helper.thumb_secondary, helper.metadata.posters)

class IdenticalPairSelectionRail(unittest.TestCase):
    """
        Measured live on Aleron Kong AFTER v1.3.143: the withhold fired on
        both passes and the duplicate survived anyway, because the SELECTED
        poster was the secondary's own container entry -- Plex retains a
        selected entry regardless of what the agent lists (the server-side
        picked-poster-evaporates protection), so withholding the selection
        achieves nothing. The rail every other guard already has: never
        withhold the selected copy -- the withholdable copy of an identical
        pair is the NON-selected one, whichever side it is.
    """

    OWN_SECONDARY = None  # filled in setUp from own_container_key

    def setUp(self):
        self.real_fetch = AG.fetch_url_bytes
        self.media = []
        outer = self
        real_media = AG.Proxy.Media
        AG.Proxy.Media = lambda data, **kw: outer.media.append(data) or ('media', 0)
        self.real_media = real_media
        self.OWN_SECONDARY = AG.own_container_key('https://audible/photo.jpg')

    def tearDown(self):
        AG.fetch_url_bytes = self.real_fetch
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

    def test_a_selected_identical_secondary_is_NOT_withheld(self):
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900)
        helper = self._helper()
        state = ('101', self.OWN_SECONDARY, [self.OWN_SECONDARY], None)
        posters, _ = AG.offer_secondary_author_poster(
            helper, [helper.thumb], dup_state=state, thumb_data=_jpeg(900, 900))
        self.assertIn(helper.thumb_secondary, helper.metadata.posters)
        self.assertIn(helper.thumb_secondary, posters)

    def test_an_unselected_identical_secondary_is_still_withheld(self):
        AG.fetch_url_bytes = lambda url: _jpeg(900, 900)
        helper = self._helper()
        other_sel = AG.own_container_key(helper.thumb)
        state = ('101', other_sel, [other_sel], None)
        posters, _ = AG.offer_secondary_author_poster(
            helper, [helper.thumb], dup_state=state, thumb_data=_jpeg(900, 900))
        self.assertNotIn(helper.thumb_secondary, helper.metadata.posters)

    def test_update_withholds_the_thumb_when_the_selected_twin_is_the_secondary(self):
        # The thumb side of the rail lives inline in the artist update();
        # only the source can witness it.
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', '__init__.py'
        )
        with open(path) as f:
            src = f.read()
        self.assertIn('the SELECTED secondary -- not listing it twice', src)

class PassGuardIsTheMemoNotContainerMembership(unittest.TestCase):
    """
        Two libraries side by side share per-guid metadata bundles, so the
        DESERIALIZED container arrives pre-populated with a sibling library's
        entries -- 'local_key in posters' can mean 'inherited', not 'offered
        earlier this pass'. Measured live on the Testing library (2026-07-27):
        ZERO cover.jpg reads across an entire fresh scan, no book received
        its local cover, because every inherited entry satisfied the
        membership fast-path. The per-pass memo (album_cover_memo, v1.3.136)
        is the only honest this-pass signal; the membership guard it
        superseded must not exist.
    """

    def source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code', '__init__.py'
        )
        with open(path) as f:
            return f.read()

    def test_no_container_membership_fast_path(self):
        self.assertNotIn('elif local_key in helper.metadata.posters', self.source())

    def test_the_memo_is_still_consulted(self):
        src = self.source()
        self.assertIn('album_cover_decision(', src)
        self.assertIn('remember_album_cover_decision(', src)


class TestPerceptualConsult(unittest.TestCase):
    """
        v1.3.149: byte identity misses re-encodes of the SAME picture (census
        2026-07-27: four artists showed a hand-uploaded author photo next to
        our byte-different copy -- distances 0-2, invisible to same_image, so
        refreshes could never heal them). images_similar_via_api asks the
        api's POST /images/similar for a perceptual verdict; every failure
        answers None and callers treat None as "not similar" (fail-open: a
        duplicate tile is cosmetic, a hidden poster option is not).

        Memo contract: definitive verdicts (similar / dissimilar /
        undecodable) are remembered for the process lifetime because the same
        two blobs recur on every refresh pass; TRANSIENT failures are NOT
        memoized, so one network blip cannot pin a pair to "no verdict"
        until the next Plex restart.
    """

    A = b'\xff\xd8AAAA'
    B = b'\xff\xd8BBBB'

    def setUp(self):
        import types as T
        self.T = T
        self.calls = []
        self.real_http = AG.HTTP
        AG.Prefs['api_base_url'] = 'http://api.test:3737'
        AG.PERCEPTUAL_MEMO.clear()

    def tearDown(self):
        AG.HTTP = self.real_http
        AG.Prefs.pop('api_base_url', None)
        AG.PERCEPTUAL_MEMO.clear()

    def http_answering(self, body):
        def request(url, **kw):
            self.calls.append((url, kw))
            return self.T.SimpleNamespace(content=body)
        AG.HTTP = self.T.SimpleNamespace(Request=request)

    def http_failing(self):
        def request(url, **kw):
            self.calls.append((url, kw))
            raise Exception('connection refused')
        AG.HTTP = self.T.SimpleNamespace(Request=request)

    def test_no_api_base_url_no_call(self):
        AG.Prefs.pop('api_base_url', None)  # falls to the blank default
        self.http_answering('{"similar": true}')
        self.assertIsNone(AG.images_similar_via_api(self.A, self.B, 't'))
        self.assertEqual(self.calls, [])

    def test_similar_verdict_comes_back_true(self):
        self.http_answering(
            '{"similar": true, "distance": 1, "undecodable": false}')
        self.assertIs(AG.images_similar_via_api(self.A, self.B, 't'), True)
        url, kw = self.calls[0]
        self.assertTrue(url.endswith('/images/similar'))
        self.assertIn('"a"', kw['data'])

    def test_dissimilar_verdict_comes_back_false(self):
        self.http_answering(
            '{"similar": false, "distance": 31, "undecodable": false}')
        self.assertIs(AG.images_similar_via_api(self.A, self.B, 't'), False)

    def test_verdicts_are_memoized_either_order(self):
        self.http_answering(
            '{"similar": true, "distance": 0, "undecodable": false}')
        AG.images_similar_via_api(self.A, self.B, 't')
        self.assertIs(AG.images_similar_via_api(self.B, self.A, 't'), True)
        self.assertEqual(len(self.calls), 1)

    def test_undecodable_is_a_memoized_none(self):
        self.http_answering(
            '{"similar": false, "distance": null, "undecodable": true}')
        self.assertIsNone(AG.images_similar_via_api(self.A, self.B, 't'))
        self.assertIsNone(AG.images_similar_via_api(self.A, self.B, 't'))
        self.assertEqual(len(self.calls), 1)

    def test_transient_failure_is_not_memoized(self):
        self.http_failing()
        self.assertIsNone(AG.images_similar_via_api(self.A, self.B, 't'))
        self.http_answering(
            '{"similar": true, "distance": 2, "undecodable": false}')
        self.assertIs(AG.images_similar_via_api(self.A, self.B, 't'), True)
        self.assertEqual(len(self.calls), 2)


class TestPerceptualWithhold(unittest.TestCase):
    """
        duplicate_shown_elsewhere falls back to the perceptual consult when
        bytes differ -- and every existing rail stays exactly as strong: the
        consult is only reached where the byte check already ran, so no
        selection, fresh scan, or own-key state can be affected by it.
    """

    IMG = b'\xff\xd8IMAGEBYTES'
    REENCODE = b'\xff\xd8SAMEPICTUREOTHERBYTES'
    OWN = 'metadata://posters/com.plexapp.agents.incipit_124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc'
    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'
    LOCAL_KEY = 'incipit-local-cover'

    def setUp(self):
        self.real_pfb = AG.poster_file_bytes
        self.real_consult = AG.images_similar_via_api
        self.store = {}
        AG.poster_file_bytes = lambda rk, key, tag: self.store.get(key)

    def tearDown(self):
        AG.poster_file_bytes = self.real_pfb
        AG.images_similar_via_api = self.real_consult

    def state(self, selected, keys):
        return ('101', selected, keys, None)

    def test_perceptual_twin_withheld(self):
        self.store[self.UPLOAD] = self.REENCODE
        AG.images_similar_via_api = lambda a, b, tag: True
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        self.assertTrue(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_no_verdict_fails_open(self):
        self.store[self.UPLOAD] = self.REENCODE
        AG.images_similar_via_api = lambda a, b, tag: None
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_genuinely_different_still_offered(self):
        self.store[self.UPLOAD] = self.REENCODE
        AG.images_similar_via_api = lambda a, b, tag: False
        st = self.state(self.UPLOAD, [self.OWN, self.UPLOAD])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))

    def test_own_key_selected_rail_beats_the_consult(self):
        self.store[self.UPLOAD] = self.REENCODE
        AG.images_similar_via_api = lambda a, b, tag: True
        st = self.state(AG.own_container_key(self.LOCAL_KEY),
                        [self.OWN, self.UPLOAD])
        self.assertFalse(AG.duplicate_shown_elsewhere(
            st, self.IMG, self.LOCAL_KEY, 't'))


class TestOnlinePerceptualRedundant(unittest.TestCase):
    """
        v1.3.150: the ONLINE-cover leg gets the same perceptual fallback as
        the cross-source withhold. Live specimen (The Knight, rk 128504): our
        online cover is the Audible-bannered variant of the clean cover.jpg
        -- byte-different, dHash distance 2 -- so byte-only redundancy
        re-offered it as a fourth tile of the same picture every refresh.
        The existing gate (local_set or mirror_skipped) and the fail-open
        contract are untouched; only the equality widened.
    """

    IMG = b'\xff\xd8IMAGEBYTES'
    VARIANT = b'\xff\xd8SAMEPICTUREBANNERED'

    def setUp(self):
        self.real_consult = AG.images_similar_via_api

    def tearDown(self):
        AG.images_similar_via_api = self.real_consult

    def test_perceptual_twin_is_redundant(self):
        AG.images_similar_via_api = lambda a, b, tag: True
        self.assertTrue(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, True, False))

    def test_no_verdict_fails_open(self):
        AG.images_similar_via_api = lambda a, b, tag: None
        self.assertFalse(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, True, False))

    def test_genuinely_different_still_offered(self):
        AG.images_similar_via_api = lambda a, b, tag: False
        self.assertFalse(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, True, False))

    def test_gate_still_short_circuits_the_consult(self):
        # Without local_set/mirror_skipped the bytes are NOT on display
        # elsewhere, so the consult must not even run.
        def boom(a, b, tag):
            raise AssertionError('consult reached past the gate')
        AG.images_similar_via_api = boom
        self.assertFalse(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, False, False))


class TestOnlinePerceptualPref(unittest.TestCase):
    """
        v1.3.151 (operator's Option A): online_perceptual_dedupe gates ONLY
        the perceptual branch of the online-cover redundancy check. Default
        on -- the operator runs LMA today, so variants ride LMA's tiles and
        the picker stays clean. Unchecking it (the documented step before
        disabling LMA) makes every perceptually-suppressed online cover
        re-offer on the next refresh: suppression is stateless, so nothing
        is ever lost, only unlisted. Byte-identical suppression is zero-loss
        by definition and stays unconditional.
    """

    IMG = b'\xff\xd8IMAGEBYTES'
    VARIANT = b'\xff\xd8SAMEPICTUREBANNERED'

    def setUp(self):
        self.real_consult = AG.images_similar_via_api

    def tearDown(self):
        AG.images_similar_via_api = self.real_consult
        AG.Prefs.pop('online_perceptual_dedupe', None)

    def test_pref_defaults_on(self):
        AG.images_similar_via_api = lambda a, b, tag: True
        self.assertTrue(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, True, False))

    def test_pref_off_skips_the_consult_entirely(self):
        def boom(a, b, tag):
            raise AssertionError('consult reached with the pref off')
        AG.images_similar_via_api = boom
        AG.Prefs['online_perceptual_dedupe'] = False
        self.assertFalse(AG.online_copy_is_redundant(
            self.VARIANT, self.IMG, True, False))

    def test_pref_off_keeps_byte_identity_suppression(self):
        AG.Prefs['online_perceptual_dedupe'] = False
        self.assertTrue(AG.online_copy_is_redundant(
            self.IMG, self.IMG, True, False))


class TestOnlineOfferRedundant(unittest.TestCase):
    """
        v1.3.152: the ONLINE-cover offer finally gets the same cross-source
        check as the mirror. Measured post-sweep (2026-07-27, 27 of 246
        albums): a rip often embeds the very CDN file the record's cover URL
        serves, so LMA displays bytes IDENTICAL to our online cover, the
        selection is a third picture, and the online leg -- which only ever
        compared against cover.jpg -- re-offered the duplicate on every
        forced refresh. File surgery could not remove what the agent
        recreates each pass; only this check can.
    """

    IMG = b'\xff\xd8ONLINEBYTES'
    COVER = b'\xff\xd8COVERBYTES'
    THUMB_KEY = 'https://cdn.example/king-of-duels.jpg'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_9999888877776666555544443333222211110000'
    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'

    def setUp(self):
        self.real_pfb = AG.poster_file_bytes
        self.store = {}
        AG.poster_file_bytes = lambda rk, key, tag: self.store.get(key)

    def tearDown(self):
        AG.poster_file_bytes = self.real_pfb

    def state(self, selected, keys):
        return ('101', selected, keys, None)

    def test_cover_jpg_leg_still_fires_first(self):
        self.assertTrue(AG.online_offer_redundant(
            self.IMG, self.IMG, True, False, None, self.THUMB_KEY)[0])

    def test_lma_identical_bytes_withhold_the_online_copy(self):
        # King of Duels: LMA's embedded art == the online cover bytes, the
        # selection is a different hand-picked upload.
        self.store[self.LMA] = self.IMG
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        self.assertTrue(AG.online_offer_redundant(
            self.IMG, self.COVER, True, False, st, self.THUMB_KEY)[0])

    def test_selected_online_copy_is_never_undercut(self):
        self.store[self.LMA] = self.IMG
        own = AG.own_container_key(self.THUMB_KEY)
        st = self.state(own, [own, self.LMA])
        self.assertFalse(AG.online_offer_redundant(
            self.IMG, self.COVER, True, False, st, self.THUMB_KEY)[0])

    def test_unique_online_cover_still_offered(self):
        self.store[self.LMA] = self.COVER
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        self.assertFalse(AG.online_offer_redundant(
            self.IMG, self.COVER, True, False, st, self.THUMB_KEY)[0])

    def test_no_state_fails_open(self):
        self.assertFalse(AG.online_offer_redundant(
            self.IMG, self.COVER, True, False, None, self.THUMB_KEY)[0])

    def test_no_bytes_fails_open(self):
        self.assertFalse(AG.online_offer_redundant(
            None, self.COVER, True, False,
            self.state(self.UPLOAD, []), self.THUMB_KEY)[0])


class TestPerceptualRailsAfterReview(unittest.TestCase):
    """
        The 2026-07-28 review's confirmed poster findings, as executable rails.

        1. SELECTION RAIL ON THE ONLINE PRUNE. The keep-list at the end of the
           album-cover block never consulted the selection, so a SELECTED
           online cover could be pruned by `keep = []`. The docstring's
           "safe by construction" argument was fallacious: excluding incipit
           keys as comparison SOURCES says nothing about which key is
           SELECTED. Byte-identical suppression only costs a tile (cover.jpg
           holds the same picture), but the perceptual verdict prunes a
           variant the operator actually picked and never re-offers it.

        2. ONE GATE FOR THE PERCEPTUAL CONSULT. `online_perceptual_dedupe`
           gated only `online_copy_is_redundant`, while the cross-source leg
           reached the ungated consult inside `duplicate_shown_elsewhere` --
           so unchecking the pref did not restore variant covers, which is
           the one thing its label promises.

        3. A PERCEPTUAL VERDICT MUST NOT PRUNE THE LOCAL MIRROR. Byte
           identity means the picture is provably on display elsewhere;
           mere similarity does not, and the mirror is the operator's
           curated cover.jpg. It may be withheld as an offer, never deleted.
    """

    IMG = b'\xff\xd8ONLINEBYTES'
    VARIANT = b'\xff\xd8SAMEPICTUREBANNERED'
    THUMB = 'https://cdn.example/cover.jpg'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_9999888877776666555544443333222211110000'
    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'

    def setUp(self):
        self.real_consult = AG.images_similar_via_api
        self.real_pfb = AG.poster_file_bytes
        self.store = {}
        AG.poster_file_bytes = lambda rk, key, tag: self.store.get(key)

    def tearDown(self):
        AG.images_similar_via_api = self.real_consult
        AG.poster_file_bytes = self.real_pfb
        AG.Prefs.pop('online_perceptual_dedupe', None)

    def state(self, selected, keys):
        return ('101', selected, keys, None)

    # 1. selection rail
    def test_selected_online_cover_is_never_pruned(self):
        own_online = AG.own_container_key(self.THUMB)
        st = self.state(own_online, [own_online, self.LMA])
        self.assertFalse(AG.online_prune_allowed(st, self.THUMB))

    def test_prune_allowed_when_the_selection_is_elsewhere(self):
        own_online = AG.own_container_key(self.THUMB)
        st = self.state(self.UPLOAD, [own_online, self.UPLOAD])
        self.assertTrue(AG.online_prune_allowed(st, self.THUMB))

    def test_prune_allowed_without_state(self):
        # No state = no evidence of a selection; the pre-review behaviour.
        self.assertTrue(AG.online_prune_allowed(None, self.THUMB))

    # 2. one gate
    def test_pref_off_disables_the_cross_source_consult_too(self):
        self.store[self.LMA] = self.VARIANT
        AG.images_similar_via_api = lambda a, b, tag: True
        AG.Prefs['online_perceptual_dedupe'] = False
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        self.assertFalse(AG.online_offer_redundant(
            self.IMG, None, False, False, st, self.THUMB)[0])

    def test_pref_on_still_withholds_the_cross_source_twin(self):
        self.store[self.LMA] = self.VARIANT
        AG.images_similar_via_api = lambda a, b, tag: True
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        self.assertTrue(AG.online_offer_redundant(
            self.IMG, None, False, False, st, self.THUMB)[0])

    def test_byte_identity_ignores_the_pref(self):
        # Zero-loss by definition: the very same bytes are already listed.
        self.store[self.LMA] = self.IMG
        AG.Prefs['online_perceptual_dedupe'] = False
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        self.assertTrue(AG.online_offer_redundant(
            self.IMG, None, False, False, st, self.THUMB)[0])

    # 3. a perceptual verdict never prunes the mirror
    def test_perceptual_mirror_skip_does_not_prune(self):
        self.store[self.LMA] = self.VARIANT
        AG.images_similar_via_api = lambda a, b, tag: True
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        skipped, byte_exact = AG.mirror_withheld(st, self.IMG, 'k', 't')
        self.assertTrue(skipped)
        self.assertFalse(byte_exact)

    def test_byte_mirror_skip_still_prunes(self):
        self.store[self.LMA] = self.IMG
        st = self.state(self.UPLOAD, [self.UPLOAD, self.LMA])
        skipped, byte_exact = AG.mirror_withheld(st, self.IMG, 'k', 't')
        self.assertTrue(skipped)
        self.assertTrue(byte_exact)


class TestConsultNeverKillsTheUpdate(unittest.TestCase):
    """
        The consult sits inside `update()`, so anything it raises aborts the
        album mid-write (the mirror already in `metadata.posters`, the select
        and the cover backup never run). The 2026-07-28 review found three
        uncaught paths, all reachable with the api merely misbehaving rather
        than being down: sha1 over a py2 `unicode` body (a CDN interstitial
        served as text/html), `.get()` on non-object JSON, and a check-then-
        index race against a concurrent `.clear()` of the module memo.
    """

    def setUp(self):
        import types as T
        self.T = T
        self.real_http = AG.HTTP
        AG.Prefs['api_base_url'] = 'http://api.test:3737'
        AG.Prefs['online_perceptual_dedupe'] = True
        AG.PERCEPTUAL_MEMO.clear()

    def tearDown(self):
        AG.HTTP = self.real_http
        AG.Prefs.pop('api_base_url', None)
        AG.Prefs.pop('online_perceptual_dedupe', None)
        AG.PERCEPTUAL_MEMO.clear()

    def answer(self, body):
        AG.HTTP = self.T.SimpleNamespace(
            Request=lambda url, **kw: self.T.SimpleNamespace(content=body))

    def test_unicode_body_yields_no_verdict_instead_of_raising(self):
        self.answer('{"similar": true, "distance": 0, "undecodable": false}')
        html = u'<html>rate limited — try later</html>'
        self.assertIsNone(AG.images_similar_via_api(html, b'\xff\xd8REAL', 't'))

    def test_non_object_json_yields_no_verdict_instead_of_raising(self):
        for body in ('null', '[]', '"ok"', '42'):
            AG.PERCEPTUAL_MEMO.clear()
            self.answer(body)
            self.assertIsNone(
                AG.images_similar_via_api(b'\xff\xd8A', b'\xff\xd8B', 't'))

    def test_memo_lookup_survives_a_concurrent_clear(self):
        # Simulate the race: the dict answers "present" then empties.
        class RacyDict(dict):
            def __contains__(self, key):
                return True
        AG.PERCEPTUAL_MEMO = RacyDict()
        try:
            self.answer('{"similar": true, "distance": 1, "undecodable": false}')
            self.assertIs(
                AG.images_similar_via_api(b'\xff\xd8A', b'\xff\xd8B', 't'), True)
        finally:
            AG.PERCEPTUAL_MEMO = {}


class TestConsultIsCheapAndBounded(unittest.TestCase):
    """
        The consult POSTs both images (base64-inflated) over the network, so
        the 2026-07-28 review measured up to 6 multi-megabyte round trips per
        call and up to 60s of blocking inside one update() when the api is
        unreachable. Two guards, both free:

        * ASPECT PRE-FILTER: a re-encode or resize PRESERVES aspect ratio, so
          two blobs whose shapes differ cannot be the same picture. The bundle
          already parses dimensions from headers in microseconds with no
          network, which rejects the whole square-cover-vs-portrait-photo
          class before any POST.
        * ONE FAILURE ENDS THE PASS: transient failures are deliberately not
          memoized, so without this the next key, the next call site and the
          next track each pay the full timeout again.
    """

    SQUARE = 'square-bytes'
    TALL = 'tall-bytes'
    OTHER = 'other-square'

    def setUp(self):
        self.real_dims = AG.image_dimensions
        self.real_consult = AG.images_similar_via_api
        self.calls = []
        # OTHER is the same shape AND size as SQUARE: this class tests the
        # aspect pre-filter, so the resolution-preference rule added on
        # 2026-07-28 must not be what decides these cases.
        dims = {self.SQUARE: (1000, 1000), self.TALL: (600, 900),
                self.OTHER: (1000, 1000)}
        AG.image_dimensions = lambda data: dims.get(data)
        AG.Prefs['online_perceptual_dedupe'] = True

    def tearDown(self):
        AG.image_dimensions = self.real_dims
        AG.images_similar_via_api = self.real_consult
        AG.Prefs.pop('online_perceptual_dedupe', None)

    def consult(self, verdict):
        def fn(a, b, tag):
            self.calls.append((a, b))
            return verdict
        AG.images_similar_via_api = fn

    def test_different_aspect_never_reaches_the_api(self):
        self.consult(True)
        self.assertEqual(
            AG.same_picture(self.SQUARE, self.TALL, 't'), (False, False))
        self.assertEqual(self.calls, [])

    def test_same_aspect_still_consults(self):
        self.consult(True)
        self.assertEqual(
            AG.same_picture(self.SQUARE, self.OTHER, 't'), (True, False))
        self.assertEqual(len(self.calls), 1)

    def test_unknown_dimensions_still_consult(self):
        # Fails OPEN toward asking: an unparsed header must not silently
        # disable dedupe.
        self.consult(True)
        self.assertEqual(
            AG.same_picture('mystery-a', 'mystery-b', 't'), (True, False))
        self.assertEqual(len(self.calls), 1)


class TestCoverKeepList(unittest.TestCase):
    """
        The membership list handed to validate_keys, as a FUNCTION.

        The 2026-07-28 mutation sweep proved every guard in this block was
        unenforced: neutering `online_prune_allowed` at the call site, or
        deleting the byte-identity gate, or discarding the dedupe verdicts
        entirely, all left the suite 271/271 green -- because the only tests
        were `assertIn('foo(', source)`, which cannot tell a live guard from
        a discarded return value (and two of them matched a string that
        appears twice). Source slices are not tests. Extracting the decision
        makes it one.
    """

    THUMB = 'https://cdn.example/cover.jpg'
    LOCAL = 'incipit-local-cover'

    def keep(self, **over):
        args = dict(
            thumb_key=self.THUMB, local_key=self.LOCAL,
            thumb_present=True, local_present=True,
            online_redundant=False, online_byte_exact=False,
            online_prune_ok=True, mirror_skipped=False,
            mirror_byte_exact=False,
        )
        args.update(over)
        return AG.cover_keep_list(**args)

    def test_both_entries_kept_by_default(self):
        self.assertEqual(self.keep(), [self.THUMB, self.LOCAL])

    def test_byte_identical_online_copy_is_pruned(self):
        self.assertEqual(
            self.keep(online_redundant=True, online_byte_exact=True),
            [self.LOCAL])

    def test_a_perceptual_online_verdict_never_prunes(self):
        # It may be withheld as an OFFER, but the entry survives: a variant
        # deleted here never comes back, because the same bytes re-derive the
        # same verdict on every later pass.
        self.assertEqual(
            self.keep(online_redundant=True, online_byte_exact=False),
            [self.THUMB, self.LOCAL])

    def test_a_selected_online_cover_is_never_pruned(self):
        self.assertEqual(
            self.keep(online_redundant=True, online_byte_exact=True,
                      online_prune_ok=False),
            [self.THUMB, self.LOCAL])

    def test_byte_identical_mirror_is_pruned(self):
        self.assertEqual(
            self.keep(mirror_skipped=True, mirror_byte_exact=True),
            [self.THUMB])

    def test_a_perceptual_mirror_verdict_never_prunes_the_curated_file(self):
        self.assertEqual(
            self.keep(mirror_skipped=True, mirror_byte_exact=False),
            [self.THUMB, self.LOCAL])

    def test_absent_entries_are_not_invented(self):
        self.assertEqual(self.keep(thumb_present=False), [self.LOCAL])
        self.assertEqual(self.keep(local_present=False), [self.THUMB])


class TestAlbumCoverMemoRoundTrip(unittest.TestCase):
    """
        The memo replays a pass's decisions on tracks 2..N. It stored a
        positional 5-tuple of DECISIONS but not the EVIDENCE the prune rails
        read, so a 27-part book ran 26 un-railed passes: `dup_state` was None
        (making `online_prune_allowed` permissive) and `mirror_byte_exact`
        was False (flipping the mirror verdict). Three reviewers found this
        independently. A named mapping cannot drift positionally, and a
        missing key must read as the SAFE value.
    """

    def setUp(self):
        AG.album_cover_memo.clear()

    def tearDown(self):
        AG.album_cover_memo.clear()

    def test_every_prune_input_round_trips(self):
        flags = {
            'local_set': True, 'mirror_skipped': True,
            'mirror_byte_exact': True, 'deferred_portrait_local': False,
            'poisoned_local': False, 'online_redundant': True,
            'online_byte_exact': True, 'online_prune_ok': False,
        }
        AG.remember_album_cover_decision('g', True, flags)
        self.assertEqual(AG.album_cover_decision('g', True), flags)

    def test_a_sibling_track_reaches_the_same_keep_list_as_track_one(self):
        flags = {
            'local_set': False, 'mirror_skipped': True,
            'mirror_byte_exact': True, 'deferred_portrait_local': False,
            'poisoned_local': False, 'online_redundant': True,
            'online_byte_exact': True, 'online_prune_ok': False,
        }
        AG.remember_album_cover_decision('g2', True, flags)
        restored = AG.album_cover_decision('g2', True)
        first = AG.cover_keep_list(
            thumb_key='t', local_key='l', thumb_present=True,
            local_present=True, **{k: flags[k] for k in (
                'online_redundant', 'online_byte_exact', 'online_prune_ok',
                'mirror_skipped', 'mirror_byte_exact')})
        sibling = AG.cover_keep_list(
            thumb_key='t', local_key='l', thumb_present=True,
            local_present=True, **{k: restored[k] for k in (
                'online_redundant', 'online_byte_exact', 'online_prune_ok',
                'mirror_skipped', 'mirror_byte_exact')})
        self.assertEqual(first, sibling)

    def test_an_absent_flag_reads_as_the_safe_value(self):
        AG.remember_album_cover_decision('g3', True, {'local_set': True})
        restored = AG.album_cover_decision('g3', True)
        self.assertFalse(restored.get('online_prune_ok', False))
        self.assertFalse(restored.get('mirror_byte_exact', False))


class TestSandboxIdentifierGuard(unittest.TestCase):
    """
        The catastrophe class: Plex's RestrictedPython rejects ANY identifier
        beginning with an underscore at COMPILE time, which kills the entire
        plugin silently -- Fix Match spins forever, no UI error, and
        `py_compile` passes. It has shipped twice (v1.3.10, and a module
        global before that).

        The 2026-07-28 mutation sweep found NOTHING guarded this: nine
        mutations introducing `_acc`, `_stripped`, `_INTERNAL_MARK`,
        `def _series_key_impl`, `sum()`, `any()`, `getattr`, `hasattr` and
        `bytearray` all left the suite green -- including two on lines whose
        own comments say the builtin is unavailable.
    """

    import re as re_mod

    BANNED_BUILTINS = ('getattr', 'hasattr', 'dir', 'sum', 'any', 'all',
                       'bytearray', 'eval', 'exec', 'compile', 'open')

    def code_files(self):
        code_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'Contents', 'Code')
        for name in sorted(os.listdir(code_dir)):
            if name.endswith('.py'):
                with open(os.path.join(code_dir, name)) as handle:
                    yield name, self.strip_prose(handle.read())

    @staticmethod
    def strip_prose(src):
        """
            Blank out docstrings, comments and string literals, keeping line
            numbers, so prose ABOUT a banned construct is not mistaken for
            one -- this file documents `open()` being blocked, twice.
        """
        out = []
        fence = None
        for line in src.splitlines():
            if fence:
                out.append('')
                if fence in line:
                    fence = None
                continue
            stripped = line.strip()
            for quote in ('\"\"\"', "'''"):
                if stripped.startswith(quote) and stripped.count(quote) == 1:
                    fence = quote
                    break
            if fence:
                out.append('')
                continue
            line = line.split('#', 1)[0]
            line = TestSandboxIdentifierGuard.re_mod.sub(r'\"[^\"]*\"', '""', line)
            line = TestSandboxIdentifierGuard.re_mod.sub(r"'[^']*'", "''", line)
            out.append(line)
        return '\n'.join(out)

    def test_no_underscore_prefixed_identifiers(self):
        # Assignments, defs, classes and attribute reads alike. Dunders are
        # fine (the framework itself uses them) -- it is the SINGLE leading
        # underscore the sandbox rejects.
        patterns = (
            self.re_mod.compile(r'^\s*(_[a-zA-Z]\w*)\s*='),
            self.re_mod.compile(r'^\s*def\s+(_[a-zA-Z]\w*)'),
            self.re_mod.compile(r'^\s*class\s+(_[a-zA-Z]\w*)'),
            self.re_mod.compile(r'\bself\.(_[a-zA-Z]\w*)'),
        )
        offenders = []
        for name, src in self.code_files():
            for lineno, line in enumerate(src.splitlines(), 1):
                for pat in patterns:
                    found = pat.search(line)
                    if found and not found.group(1).startswith('__'):
                        offenders.append('%s:%d %s' % (name, lineno, found.group(1)))
        self.assertEqual(offenders, [], 'sandbox-fatal identifiers: %s' % offenders)

    def test_no_blocked_builtins(self):
        offenders = []
        for name, src in self.code_files():
            for lineno, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                for builtin in self.BANNED_BUILTINS:
                    if self.re_mod.search(r'(?<![\w.])%s\s*\(' % builtin, line):
                        offenders.append('%s:%d %s' % (name, lineno, builtin))
        self.assertEqual(offenders, [], 'sandbox-blocked builtins: %s' % offenders)


class TestTheBetterCopySurvives(unittest.TestCase):
    """
        When two sources show the same picture, the one that SURVIVES must be
        the higher-quality one.

        The agent can only ever withhold ITS OWN tile -- another agent's entry
        is not ours to remove -- so withholding unconditionally means the copy
        left standing is whatever the other source happens to hold. Measured
        on Men at Arms (2026-07-28): our poster is 739KB at full resolution,
        Local Media Assets holds a 67KB re-encode of the same design. Dropping
        ours leaves the operator with only the small one, which is the
        opposite of the intent -- and raising the perceptual threshold made
        that MORE likely, not less.

        So: byte-identical is a free swap (same pixels, keep theirs, drop
        ours). A merely SIMILAR match only justifies withholding when their
        copy is at least as detailed as ours. When ours is clearly better we
        keep it and accept the extra tile -- a duplicate tile is cosmetic,
        losing the good copy is not.
    """

    OURS = 'ours-big'
    THEIRS_SMALL = 'theirs-small'
    THEIRS_BIG = 'theirs-big'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_9999888877776666555544443333222211110000'
    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'

    def setUp(self):
        self.real_dims = AG.image_dimensions
        self.real_consult = AG.images_similar_via_api
        self.real_pfb = AG.poster_file_bytes
        dims = {self.OURS: (2400, 2400), self.THEIRS_SMALL: (500, 500),
                self.THEIRS_BIG: (3000, 3000)}
        AG.image_dimensions = lambda d: dims.get(d)
        AG.images_similar_via_api = lambda a, b, tag: True
        AG.Prefs['online_perceptual_dedupe'] = True
        self.store = {}
        AG.poster_file_bytes = lambda rk, key, tag: self.store.get(key)

    def tearDown(self):
        AG.image_dimensions = self.real_dims
        AG.images_similar_via_api = self.real_consult
        AG.poster_file_bytes = self.real_pfb
        AG.Prefs.pop('online_perceptual_dedupe', None)

    def test_we_keep_ours_when_theirs_is_lower_resolution(self):
        same, byte_exact = AG.same_picture(self.OURS, self.THEIRS_SMALL, 't')
        self.assertFalse(same)
        self.assertFalse(byte_exact)

    def test_we_withhold_ours_when_theirs_is_equal_or_better(self):
        self.assertEqual(AG.same_picture(self.OURS, self.THEIRS_BIG, 't'),
                         (True, False))

    def test_byte_identical_is_always_a_free_swap(self):
        # Same pixels: nothing is lost by dropping our copy.
        AG.image_dimensions = lambda d: (100, 100)
        self.assertEqual(AG.same_picture(b'\xff\xd8SAME', b'\xff\xd8SAME', 't'),
                         (True, True))

    def test_unknown_dimensions_still_withhold(self):
        # Fail toward the existing behaviour rather than silently disabling
        # dedupe whenever a header cannot be parsed.
        AG.image_dimensions = lambda d: None
        self.assertEqual(AG.same_picture('mystery-a', 'mystery-b', 't'),
                         (True, False))

    def test_the_container_scan_keeps_our_better_copy(self):
        # End to end through the predicate the offer paths actually call.
        self.store[self.LMA] = self.THEIRS_SMALL
        st = ('101', self.UPLOAD, [self.UPLOAD, self.LMA], None)
        shown, byte_exact = AG.duplicate_shown_detail(st, self.OURS, 'k', 't')
        self.assertFalse(shown)


class SweepFetchesEachPosterOnce(unittest.TestCase):
    """
        The per-pass sweep memo (v1.3.161): the mirror leg and the online leg
        each walk the SAME container keys through duplicate_shown_detail, so
        one track's pass downloaded every non-incipit poster twice -- and a
        failing key ate its 8s timeout once per leg. poster_file_bytes now
        memoizes per (rk, key) for POSTER_BYTES_TTL, failures included.

        selected_poster_bytes is DELIBERATELY exempt: a selection read feeds
        upload/skip decisions and our own metadata:// key serves mutable
        bytes, so a stale read there can skip a required upload. The sweep's
        comparisons only ever fail open.
    """

    UPLOAD = 'upload://posters/aaaa0000bbbb1111cccc2222dddd3333eeee4444'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_ffff0000'
    OWN = ('metadata://posters/com.plexapp.agents.incipit_'
           '124a757ccdffc12d2dbe1a4bdf291e5c6bebf1cc')

    def setUp(self):
        self.reads = []
        self.real = AG.HTTP.Request
        self.fail_urls = set()

        def router(url, **kwargs):
            self.reads.append(url)
            if url in self.fail_urls:
                raise IOError('poster read failed')

            class Fetched(object):
                content = b'\xff\xd8POSTERBYTES'

            return Fetched()

        AG.HTTP.Request = router
        AG.POSTER_BYTES_MEMO.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real
        AG.POSTER_BYTES_MEMO.clear()

    def test_second_leg_reuses_the_first_legs_download(self):
        state = ('301', self.UPLOAD, [self.OWN, self.UPLOAD, self.LMA], None)
        img = b'\xff\xd8ADIFFERENTIMAGE'
        # Mirror leg, then online leg, exactly the per-track sequence.
        AG.duplicate_shown_detail(state, img, 'incipit-local-cover', 't')
        first_leg = len(self.reads)
        self.assertGreater(first_leg, 0)
        AG.duplicate_shown_detail(state, img, 'incipit-online-cover', 't')
        self.assertEqual(
            len(self.reads), first_leg,
            'the online leg re-downloaded posters the mirror leg already had')

    def test_a_failed_fetch_is_not_retried_within_the_pass(self):
        url = (AG.PMS + '/library/metadata/302/file?url='
               + AG.urllib.quote(self.UPLOAD, ''))
        self.fail_urls.add(url)
        self.assertIsNone(AG.poster_file_bytes('302', self.UPLOAD, 't'))
        self.assertIsNone(AG.poster_file_bytes('302', self.UPLOAD, 't'))
        self.assertEqual(len(self.reads), 1,
                         'a doomed fetch must not repeat its 8s timeout')

    def test_the_memo_expires(self):
        AG.poster_file_bytes('303', self.UPLOAD, 't')
        for key in list(AG.POSTER_BYTES_MEMO):
            payload, stamp = AG.POSTER_BYTES_MEMO[key]
            AG.POSTER_BYTES_MEMO[key] = (payload, stamp - AG.POSTER_BYTES_TTL - 1)
        AG.poster_file_bytes('303', self.UPLOAD, 't')
        self.assertEqual(len(self.reads), 2, 'an expired entry must refetch')

    def test_distinct_keys_fetch_separately(self):
        AG.poster_file_bytes('304', self.UPLOAD, 't')
        AG.poster_file_bytes('304', self.LMA, 't')
        self.assertEqual(len(self.reads), 2)

    def test_the_selection_read_is_never_served_stale(self):
        # Two reads, two requests: the exemption is the contract, because our
        # own container key answers with NEW bytes the moment a pass rewrites
        # the offer, and a stale skip-decision here loses an upload.
        AG.selected_poster_bytes('305', self.OWN, 't')
        AG.selected_poster_bytes('305', self.OWN, 't')
        self.assertEqual(len(self.reads), 2)

    def test_a_memoized_sweep_failure_cannot_blind_the_selection_read(self):
        url = (AG.PMS + '/library/metadata/306/file?url='
               + AG.urllib.quote(self.OWN, ''))
        self.fail_urls.add(url)
        self.assertIsNone(AG.poster_file_bytes('306', self.OWN, 't'))
        self.fail_urls.clear()
        data, known = AG.selected_poster_bytes('306', self.OWN, 't')
        self.assertTrue(known, 'the selection read must retry, not inherit '
                               'the sweep memo\'s failure')


class TestPerceptualSignatureReplay(unittest.TestCase):
    """
        The signature client for POST /images/similar (api c49ed61): every
        full verdict returns a per-image signature; the bundle banks them by
        byte sha and replays tokens in place of the base64 bodies, so an
        image uploads once per process instead of once per PAIR. staleSig
        (the api retuned its grid across a deploy) drops both cached sides
        and replays ONCE with bytes; an older api that mints no signatures
        leaves every call bytes-only with no other behavior change.
    """

    A = b'\xff\xd8AAAA'
    B = b'\xff\xd8BBBB'
    C = b'\xff\xd8CCCC'

    VERDICT = ('{"similar": false, "distance": 17, "undecodable": false,'
               ' "aSig": "g1.SIGA", "bSig": "g1.SIGB"}')

    def setUp(self):
        import types as T
        self.T = T
        self.calls = []
        self.real_http = AG.HTTP
        AG.Prefs['api_base_url'] = 'http://api.test:3737'
        AG.PERCEPTUAL_MEMO.clear()
        AG.PERCEPTUAL_SIG_MEMO.clear()

    def tearDown(self):
        AG.HTTP = self.real_http
        AG.Prefs.pop('api_base_url', None)
        AG.PERCEPTUAL_MEMO.clear()
        AG.PERCEPTUAL_SIG_MEMO.clear()

    def http_answering_seq(self, bodies):
        """Each call consumes the next body; the last repeats."""
        state = {'i': 0}

        def request(url, **kw):
            self.calls.append((url, kw))
            body = bodies[min(state['i'], len(bodies) - 1)]
            state['i'] += 1
            return self.T.SimpleNamespace(content=body)
        AG.HTTP = self.T.SimpleNamespace(Request=request)

    def sent(self, call_index):
        import json as J
        return J.loads(self.calls[call_index][1]['data'])

    def test_a_known_image_rides_as_a_signature(self):
        self.http_answering_seq([self.VERDICT])
        AG.images_similar_via_api(self.A, self.B, 't')
        first = self.sent(0)
        self.assertIn('a', first)
        self.assertIn('b', first)
        # A new pair sharing image A: A goes as its token, C as bytes.
        AG.images_similar_via_api(self.A, self.C, 't')
        second = self.sent(1)
        self.assertEqual(second.get('aSig'), 'g1.SIGA')
        self.assertNotIn('a', second)
        self.assertIn('b', second)

    def test_stale_sig_drops_the_cache_and_replays_with_bytes_once(self):
        stale = '{"similar": false, "distance": null, "undecodable": true, "staleSig": true}'
        fresh = ('{"similar": true, "distance": 2, "undecodable": false,'
                 ' "aSig": "g2.NEWA", "bSig": "g2.NEWC"}')
        self.http_answering_seq([self.VERDICT, stale, fresh])
        AG.images_similar_via_api(self.A, self.B, 't')
        # This consult sends A's cached sig, is told it is stale, and must
        # recover to the fresh verdict IN THIS CALL via a bytes replay.
        self.assertIs(AG.images_similar_via_api(self.A, self.C, 't'), True)
        self.assertEqual(len(self.calls), 3)
        replay = self.sent(2)
        self.assertIn('a', replay)
        self.assertIn('b', replay)
        self.assertNotIn('aSig', replay)
        # ...and the re-minted signatures replaced the stale ones.
        self.assertEqual(AG.PERCEPTUAL_SIG_MEMO.get(
            AG.hashlib.sha1(self.A).hexdigest()), 'g2.NEWA')

    def test_an_older_api_without_signatures_stays_bytes_only(self):
        self.http_answering_seq(
            ['{"similar": false, "distance": 17, "undecodable": false}'])
        AG.images_similar_via_api(self.A, self.B, 't')
        AG.images_similar_via_api(self.A, self.C, 't')
        self.assertEqual(AG.PERCEPTUAL_SIG_MEMO, {})
        second = self.sent(1)
        self.assertIn('a', second)
        self.assertNotIn('aSig', second)

    def test_the_verdict_memo_still_short_circuits_before_any_request(self):
        self.http_answering_seq([self.VERDICT])
        AG.images_similar_via_api(self.A, self.B, 't')
        AG.images_similar_via_api(self.A, self.B, 't')
        self.assertEqual(len(self.calls), 1)

    def test_a_bytes_only_stale_claim_cannot_loop(self):
        # No sig was sent, so a server claiming staleSig on bytes gets no
        # replay -- one call, no verdict, fail-open.
        stale = '{"similar": false, "distance": null, "undecodable": true, "staleSig": true}'
        self.http_answering_seq([stale])
        self.assertIsNone(AG.images_similar_via_api(self.A, self.B, 't'))
        self.assertEqual(len(self.calls), 1)


class ResolutionGuardSideIsTheWithheldTile(unittest.TestCase):
    """
        same_picture's keep-the-better-copy rule (v1.3.159) is ASYMMETRIC:
        it protects the FIRST argument, which must be the tile that
        disappears on a True verdict. Two of the three call sites passed
        the surviving copy first, so at the online legs the guard ran
        backwards -- found by the 2026-07-28 review (five finders).

        Concretely, with a curated hi-res cover.jpg on display and the
        provider's low-res re-encode as the online cover:
          * the RIGHT behaviour is to withhold the low-res ONLINE tile,
          * the inverted guard instead refused the verdict and re-listed
            the duplicate every pass -- and in the mirror case it withheld
            the BETTER copy, the exact loss v1.3.159 exists to prevent.
    """

    HI = b'\xff\xd8' + b'HI' * 400
    LO = b'\xff\xd8' + b'LO' * 40

    def setUp(self):
        self.real_sim = AG.images_similar_via_api
        self.real_dims = AG.image_dimensions
        self.real_aspect = AG.aspect_could_match
        AG.images_similar_via_api = lambda a, b, tag: True
        AG.aspect_could_match = lambda a, b: True
        # Same design, wildly different resolution.
        dims = {self.HI: (2400, 2400), self.LO: (500, 500)}
        AG.image_dimensions = lambda data: dims.get(data)
        AG.Prefs['online_perceptual_dedupe'] = True

    def tearDown(self):
        AG.images_similar_via_api = self.real_sim
        AG.image_dimensions = self.real_dims
        AG.aspect_could_match = self.real_aspect

    def test_a_low_res_online_cover_is_withheld_beside_a_hi_res_local(self):
        # local_set=True: cover.jpg's hi-res picture is already displayed.
        self.assertTrue(
            AG.online_copy_is_redundant(self.LO, self.HI, True, False),
            'the low-res ONLINE tile must be suppressed, not re-listed')

    def test_a_hi_res_online_cover_is_kept_beside_a_low_res_local(self):
        # The mirror case: withholding here would lose the better copy.
        self.assertFalse(
            AG.online_copy_is_redundant(self.HI, self.LO, True, False),
            'the better ONLINE copy must survive')

    def test_the_sweep_leg_keeps_protecting_our_own_tile(self):
        # duplicate_shown_detail passes OUR tile first; that site was always
        # correct and must stay correct.
        self.assertFalse(AG.same_picture(self.HI, self.LO, 't')[0],
                         'our higher-res tile must not be withheld')
        self.assertTrue(AG.same_picture(self.LO, self.HI, 't')[0],
                        'our lower-res tile may be withheld')


class PaddedCopyRecognitionIsSymmetric(unittest.TestCase):
    """
        The RESELECT_PAD copy may sit on EITHER side: same_image matched it
        in one direction only, which the argument-order fix above exposed.
        Picture identity does not depend on argument order.
    """

    IMG = b'\xff\xd8IMAGEBYTES'

    def test_padded_second(self):
        self.assertTrue(AG.same_image(self.IMG, self.IMG + AG.RESELECT_PAD))

    def test_padded_first(self):
        self.assertTrue(AG.same_image(self.IMG + AG.RESELECT_PAD, self.IMG))

    def test_different_pictures_still_differ(self):
        self.assertFalse(AG.same_image(self.IMG, b'\xff\xd8SOMETHINGELSE'))


class LocalSelectIsNotPowerless(unittest.TestCase):
    """
    select_local_cover establishes ownership, then must actually ACT on it.

    The function's whole job is to force cover.jpg to become the selection on a
    forced Refresh of an already-scanned book, and it guards that correctly: a
    USER's custom upload returns early at selection_is_agent_owned, so hand
    picks survive. But having earned the right to act it called
    upload_and_select_poster in its DEFAULT mode, whose stand-down fires
    whenever our bytes are already offered de-selected -- which is precisely
    the state it is called to repair. Ownership was checked and then the action
    was silently vetoed: correct and powerless.

    Measured live 2026-07-29 on a fresh 1,509-album library (10.0.1.99): Plex's
    Local Media Assets files cover.jpg into the item's Uploads itself, so
    "our bytes exist de-selected" arises with NO human involvement at all, and
    the online cover held the selection on 49 of 60 sampled albums. Neither a
    plain nor a FORCED refresh moved any of them (0/4 and 0/4).

    The de-selection premise the stand-down encodes ("only a person produces
    that state") is a BOOK-level default that is true only until this caller
    has already disproved it by checking ownership.
    """

    COVER = b'\xff\xd8\xff\xe0 the cover.jpg on disk'
    AGENT_SELECTION = 'metadata://posters/com.plexapp.agents.incipit/online'
    USER_SELECTION = 'upload://posters/a-poster-the-operator-uploaded'

    class FakeHelper(object):
        class metadata(object):
            guid = 'guid-local-select'

    def setUp(self):
        self.posts = []
        self.real_request = AG.HTTP.Request
        self.real_state = AG.read_poster_state
        self.real_artist = AG.artist_poster_bytes

        def recorder(url, **kwargs):
            self.posts.append((url, kwargs))

            class FakeResponse(object):
                content = 'ok'

            return FakeResponse()

        AG.HTTP.Request = recorder
        # Not poisoned: the artist photo is a different picture entirely, and
        # `known` is True so the fail-closed guard does not abort for the wrong
        # reason and make this test pass vacuously.
        AG.artist_poster_bytes = lambda guid, tag, parent_thumb=None: (ARTIST, True)
        # The currently-selected poster is a DIFFERENT picture, stated
        # explicitly. Left to the faked HTTP layer this read is unpredictable,
        # and its "already showing this image" short-circuit then masks whether
        # the ownership guard fired at all -- which is exactly how the first
        # version of these tests survived deleting that guard.
        self.real_selected = AG.selected_poster_bytes
        AG.selected_poster_bytes = lambda rk, key, tag: (
            b'\xff\xd8\xff\xe0 a different poster entirely', True)
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real_request
        AG.read_poster_state = self.real_state
        AG.artist_poster_bytes = self.real_artist
        AG.selected_poster_bytes = self.real_selected
        AG.recent_work_memo.clear()

    def _with_state(self, selected, keys):
        AG.read_poster_state = lambda guid, tag: ('101', selected, keys, None)

    def test_an_agent_selection_is_overridden_even_when_ours_is_offered(self):
        # THE regression: cover.jpg sits in the container de-selected (Local
        # Media Assets put it there) while the agent's OWN online cover holds
        # the selection. Ownership says this is ours to change, so the re-select
        # must actually be posted.
        sha, _padded, _ = AG.padded_variants(self.COVER)
        self._with_state(self.AGENT_SELECTION, [sha])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(len(self.posts), 1,
                         'an agent-owned selection must be overridden, not stood down on')
        self.assertEqual(self.posts[0][1]['data'], self.COVER + PAD,
                         'the re-select needs NEW content, so it is the padded copy')

    def test_a_user_upload_is_still_never_touched(self):
        # The Will Wight protection, unchanged: the operator picked their own
        # poster, so this path must not fire at all.
        sha, _padded, _ = AG.padded_variants(self.COVER)
        self._with_state(self.USER_SELECTION, [sha])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(self.posts, [],
                         "a user's custom upload must survive prefer_local_cover")

    def test_a_user_upload_blocks_even_a_cover_plex_has_never_seen(self):
        # Isolates the OWNERSHIP guard itself. With nothing of ours in the
        # container, every later guard would happily post (that is the birth
        # case), so the only thing that can stop this is the user-upload check.
        # The weaker sibling above passes even with the guard deleted -- a
        # downstream guard masks it -- so this is the one that pins it.
        self._with_state(self.USER_SELECTION, [])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(self.posts, [],
                         'ownership must be checked BEFORE anything is posted')

    def test_a_converged_book_does_no_work(self):
        # cover.jpg is ALREADY the selection: nothing to push, and a refresh on
        # a converged library must stay free.
        sha, _padded, _ = AG.padded_variants(self.COVER)
        self._with_state('upload://posters/' + sha, [sha])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(self.posts, [])

    def test_no_selection_at_all_still_uploads(self):
        # A book Plex has never given a poster: ours by definition.
        self._with_state(None, [])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(len(self.posts), 1)


class TheUploadIsWhatSelects(unittest.TestCase):
    """
    Never suppress select_local_cover's upload. It is what SELECTS cover.jpg.

    v1.3.166 tried the opposite and was reverted the same hour. The reasoning
    looked sound: on a cold scan the agent files cover.jpg into the container at
    sort_order=0, so Plex should default to it, making the upload a redundant
    twin. Measured on a genuinely cold library (10.0.1.99 section 49, bundles
    and agent caches cleared) 33 of 33 albums did select cover.jpg AND 33 of 33
    carried one duplicate pair -- `(upload) + com.plexapp.agents.incipit` -- so
    the duplicate is real and the code records the same shape at 147 of 150
    albums (98%) on 2026-07-25.

    But the very next cold library, with the upload suppressed, showed NO local
    posters at all. The lesson: the container offer only makes an image
    AVAILABLE in the picker; this upload is what makes it the SELECTION. The
    evidence had been there and was misread -- on section 49 the *selected*
    entry was always the `(upload)`, never the container poster.

    The duplicate must therefore be removed from the CONTAINER side (withhold or
    prune that entry when we are going to upload), never by removing the upload.
    """

    COVER = b'\xff\xd8\xff\xe0 the cover.jpg on disk'
    AGENT_SELECTION = 'metadata://posters/com.plexapp.agents.incipit/online'

    class FakeHelper(object):
        class metadata(object):
            guid = 'guid-cold-scan'

    def setUp(self):
        self.posts = []
        self.real_request = AG.HTTP.Request
        self.real_state = AG.read_poster_state
        self.real_artist = AG.artist_poster_bytes
        self.real_selected = AG.selected_poster_bytes

        def recorder(url, **kwargs):
            self.posts.append((url, kwargs))

            class FakeResponse(object):
                content = 'ok'

            return FakeResponse()

        AG.HTTP.Request = recorder
        AG.artist_poster_bytes = lambda guid, tag, parent_thumb=None: (ARTIST, True)
        AG.selected_poster_bytes = lambda rk, key, tag: (
            b'\xff\xd8\xff\xe0 a different poster entirely', True)
        AG.recent_work_memo.clear()

    def tearDown(self):
        AG.HTTP.Request = self.real_request
        AG.read_poster_state = self.real_state
        AG.artist_poster_bytes = self.real_artist
        AG.selected_poster_bytes = self.real_selected
        AG.recent_work_memo.clear()

    def _with_state(self, selected, keys):
        AG.read_poster_state = lambda guid, tag: ('101', selected, keys, None)

    def test_a_cold_scan_STILL_uploads(self):
        # THE pin. v1.3.166 skipped this upload when the container already
        # offered the same bytes and nothing was selected yet, on the reasoning
        # that sort_order=0 would become Plex's default. Disproved live: a fresh
        # library showed NO local posters at all. This upload is what SELECTS
        # cover.jpg; the container offer only puts it in the picker.
        self._with_state(None, [])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(len(self.posts), 1,
                         'the upload is the only thing that selects cover.jpg')

    def test_a_persisted_selection_is_still_overridden(self):
        # The recovery case v1.3.165 exists for: Plex has PERSISTED the agent's
        # online cover, which a container cannot move.
        sha, _padded, _ = AG.padded_variants(self.COVER)
        self._with_state(self.AGENT_SELECTION, [sha])
        AG.select_local_cover(self.FakeHelper(), self.COVER)
        self.assertEqual(len(self.posts), 1,
                         'a persisted selection still needs the upload lever')

    def test_no_container_offer_argument_exists(self):
        # Guard against re-introducing the v1.3.166 shape. Any future dedupe
        # must remove the CONTAINER entry, never this upload.
        import inspect
        try:
            args = inspect.getfullargspec(AG.select_local_cover).args
        except AttributeError:  # py2 harness
            args = inspect.getargspec(AG.select_local_cover).args
        self.assertNotIn('container_offers', args,
                         'suppressing the upload was disproved live -- dedupe '
                         'the container side instead')
