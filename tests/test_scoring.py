"""
The score the agent hands Plex.

WHY THIS EXISTS
    The 2026-07-28 mutation sweep applied 157 mutations to the bundle and 129
    survived a fully green suite. The scoring path was among the worst: with
    `score = 100` hard-coded -- confidence discarded entirely -- the suite
    stayed green. Every API candidate would then cross Plex's measured
    auto-apply bar of 80, auto-apply, and STICK (only a manual Fix Match
    clears one). Also green: IGNORE_SCORE 45->0, INITIAL_SCORE 100->175,
    `incipit_conf * 100` -> `* 130`, and both Levenshtein weights zeroed.

    These tests drive the real ScoreTool and assert on what Plex receives.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']


class FakeMedia(object):
    def __init__(self, album=None, artist=None):
        self.album = album
        self.artist = artist
        self.title = None
        self.name = None
        self.filename = None
        self.tracks = None
        self.children = None


class FakeHelper(object):
    def __init__(self, album=None, artist=None):
        self.media = FakeMedia(album, artist)


def score_tool(result_dict, index=0, album='A Title', artist='An Author',
               info=None):
    return ST.ScoreTool(
        FakeHelper(album, artist), index, info if info is not None else [],
        'en', MODULES["framework"]["Util"].LevenshteinDistance, result_dict,
    )


def api_row(confidence, title='A Title', author='An Author'):
    return {
        'confidence': confidence, 'title': title, 'asin': 'B0TEST0001',
        'author': [{'name': author}], 'narrator': [{'name': 'A Narrator'}],
        'date': '', 'year': None, 'language': 'english', 'region': 'us',
    }


class TestApiConfidenceBecomesThePlexScore(unittest.TestCase):
    """
        The API already scored these candidates on the normalized title +
        author + duration, so the bundle trusts that confidence rather than
        re-deriving a Levenshtein score from the raw album tag. The mapping
        must be the identity (x100), because Plex's auto-apply bar is
        expressed in those units.
    """

    def plex_score(self, confidence, index=0):
        info = []
        score_tool(api_row(confidence), index=index, info=info).run_score_book()
        return info[0]['score'] if info else None

    def test_confidence_maps_one_to_one(self):
        self.assertEqual(self.plex_score(1.0), 100)
        self.assertEqual(self.plex_score(0.85), 85)
        self.assertEqual(self.plex_score(0.5), 50)

    def test_a_weak_candidate_does_not_score_like_a_strong_one(self):
        # The mutation that killed the suite: a fabricated constant score.
        self.assertLess(self.plex_score(0.5), self.plex_score(1.0))
        self.assertLess(self.plex_score(0.46), 80)

    def test_index_nudges_ties_downward_but_only_slightly(self):
        self.assertEqual(self.plex_score(1.0, index=0), 100)
        self.assertEqual(self.plex_score(1.0, index=3), 97)

    def test_below_the_floor_the_candidate_is_not_offered_at_all(self):
        info = []
        score_tool(api_row(0.20), info=info).run_score_book()
        self.assertEqual(info, [])

    def test_at_the_floor_the_candidate_is_offered(self):
        info = []
        score_tool(api_row(0.45), info=info).run_score_book()
        self.assertEqual(len(info), 1)

    def test_the_floor_sits_below_plex_auto_apply_but_above_junk(self):
        # A floor of 0 would offer every candidate the API ever returned.
        self.assertGreater(ST.ScoreTool.IGNORE_SCORE, 0)
        self.assertLess(ST.ScoreTool.IGNORE_SCORE, 80)


class TestScoringConstants(unittest.TestCase):
    """
        The stock Levenshtein path (used only when the operator has NOT set
        api_base_url) cannot be driven from this harness: score_album does
        `title.encode('utf-8')`, which is a py2 str but py3 BYTES, so
        reduce_string's `.replace('-', '')` raises here and cannot in
        production. That is the documented py2/py3 harness trap, not a bug --
        and it is exactly why the constants below are asserted directly:
        INITIAL_SCORE 100->175 and IGNORE_SCORE 45->0 both survived the
        mutation sweep, and both are reachable without the encode path.

        Anyone changing score_album/score_author must verify against a LIVE
        py2 plugin load; a green harness proves nothing about them.
    """

    def test_the_ceiling_is_one_hundred(self):
        # 175 would let a stock-path candidate be offered above any real
        # confidence, outranking every API-scored row.
        self.assertEqual(ST.ScoreTool.INITIAL_SCORE, 100)

    def test_the_floor_is_meaningful(self):
        # 0 would offer every candidate the provider ever returned.
        self.assertEqual(ST.ScoreTool.IGNORE_SCORE, 45)
        self.assertGreater(ST.ScoreTool.IGNORE_SCORE, 0)
        self.assertLess(ST.ScoreTool.IGNORE_SCORE, 80)


if __name__ == '__main__':
    unittest.main()
