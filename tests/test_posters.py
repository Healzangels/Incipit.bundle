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


if __name__ == '__main__':
    unittest.main()
