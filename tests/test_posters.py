"""
Poster identity and the caching decisions around it.

"Is this image the artist's photo?" is the question the whole poison machinery
turns on, and it has to answer correctly for the PADDED form too -- the agent
re-POSTs image+RESELECT_PAD to force a re-selection, so a poisoned album that
has been through one cycle wears 20 extra bytes. An exact-byte comparison
missed that for months (found live on Kyle Mills / "Fade").
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


if __name__ == '__main__':
    unittest.main()
