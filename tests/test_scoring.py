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


class TestHonorificDoesNotBlockAnAuthorMatch(unittest.TestCase):
    """
        A LEADING HONORIFIC MUST NOT COST THE MATCH.

        Found live 2026-07-31. The library carried two artists for one man:
        "Arthur Conan Doyle" (2 albums, photo + bio) and "Sir Arthur Conan
        Doyle" (1 album, no art, no bio). Fix Match on the second offered
        "Arthur Conan Doyle" at score 70 -- and Plex's auto-apply bar is 80, so
        it never matched automatically and the artist stayed metadata-less.

        The arithmetic is exact and reproducible:
            reduce_string("Sir Arthur Conan Doyle") -> "sirarthurconandoyle"
            reduce_string("Arthur Conan Doyle")     -> "arthurconandoyle"
            Levenshtein = 3 ("sir") * the author weight 10 = 30
            100 - 30 = 70

        So three characters of courtesy title put a correct author match under
        the bar. "Sir Terry Pratchett" and "Dame ..." are the same shape.

        NOTE ON THE HARNESS: the sibling scorer score_album genuinely cannot be
        driven here (it does title.encode('utf-8'), a py2 str but py3 bytes).
        score_author does NOT encode, and plexenv stubs LevenshteinDistance, so
        this path IS reachable -- the blanket "scoring is untestable" note in
        TestScoringConstants applies to score_album only.
    """

    def tool_for(self, tagged_artist):
        tool = ST.ScoreTool.__new__(ST.ScoreTool)
        tool.calculate_score = ST.Util.LevenshteinDistance

        class FakeMedia(object):
            artist = tagged_artist

        class FakeHelper(object):
            media = FakeMedia()

        tool.helper = FakeHelper()
        return tool

    def test_an_exact_author_still_costs_nothing(self):
        tool = self.tool_for('Arthur Conan Doyle')
        self.assertEqual(tool.score_author('Arthur Conan Doyle'), 0)

    def test_a_leading_Sir_costs_nothing(self):
        tool = self.tool_for('Sir Arthur Conan Doyle')
        deduction = tool.score_author('Arthur Conan Doyle')
        self.assertEqual(
            deduction, 0,
            'a courtesy title cost %s points; 30 of them put the match at 70, '
            'under Plex\'s auto-apply bar of 80' % deduction)

    def test_it_works_in_the_other_direction_too(self):
        # The provider is equally free to carry the honorific.
        tool = self.tool_for('Arthur Conan Doyle')
        self.assertEqual(tool.score_author('Sir Arthur Conan Doyle'), 0)

    def test_other_common_courtesy_titles(self):
        for tagged, provider in (('Sir Terry Pratchett', 'Terry Pratchett'),
                                 ('Dame Agatha Christie', 'Agatha Christie'),
                                 ('Rev. W. Awdry', 'W. Awdry')):
            tool = self.tool_for(tagged)
            self.assertEqual(tool.score_author(provider), 0,
                             '%s should match %s' % (tagged, provider))

    def test_a_GENUINELY_different_author_is_still_penalised(self):
        # The relaxation must not turn into "any two authors match".
        tool = self.tool_for('Sir Arthur Conan Doyle')
        self.assertGreater(tool.score_author('Agatha Christie'), 0)
        tool = self.tool_for('Arthur Conan Doyle')
        self.assertGreater(tool.score_author('Arthur C. Clarke'), 0)

    def test_stripping_can_never_make_a_match_WORSE(self):
        # Why score_author takes min(plain, stripped) rather than just stripped.
        # Stripping is applied to both sides by the same rule, so it usually
        # helps or ties -- but when only ONE side carries a leading courtesy
        # title AND the other carries extra leading words, removing it can move
        # the strings further apart. min() makes the relaxation monotonic: it
        # can lower a deduction, never raise one.
        tool = self.tool_for('Sir A B')
        self.assertLessEqual(
            tool.score_author('X Sir A B'),
            # what the unstripped comparison alone would have cost
            ST.Util.LevenshteinDistance('sirab', 'xsirab') * 10,
            'the relaxation must never cost more than not relaxing at all')

    def test_a_TWO_word_credit_is_left_intact(self):
        # "Lord Dunsany" is the whole pen name. Reducing a two-token credit to
        # one token is too thin to compare on, so the strip requires at least
        # two tokens to remain.
        self.assertEqual(ST.strip_courtesy_title('Lord Dunsany'), 'Lord Dunsany')
        self.assertEqual(ST.strip_courtesy_title('Sir Pratchett'), 'Sir Pratchett')
        # Three tokens is enough to be safe.
        self.assertEqual(
            ST.strip_courtesy_title('Sir Terry Pratchett'), 'Terry Pratchett')

    def test_a_non_title_first_word_is_never_stripped(self):
        for name in ('Arthur Conan Doyle', 'Ursula K. Le Guin', 'J. R. R. Tolkien'):
            self.assertEqual(ST.strip_courtesy_title(name), name)

# LAST STATEMENT IN THE FILE, always. This guard used to sit mid-file, and
# `python3 tests/<file>.py` then ran only the classes DEFINED ABOVE IT and
# printed OK -- measured: test_scoring ran 8 of 16, test_cache_times 4 of 20,
# test_sort_titles 3 of 18. Discovery was unaffected, so the suite stayed
# honest while a direct run (how a single fix gets checked) silently skipped
# the new tests. tests/test_deploy_gate.py pins the position for every file.
class TestScorersCompareTextNotBytes(unittest.TestCase):
    """
    THE NON-ASCII AUTHOR SKEW.

    Under the deployed py2, `media.artist` is a utf-8 BYTE string while the
    provider's author arrives from JSON as UNICODE. score_author compared them
    raw, so Levenshtein counted every non-ASCII letter as the 2-3 bytes that
    encode it: an exact-match author with an accent scored as a MISMATCH. This
    library is full of them -- Beren and Lúthien, Der große Bruderkrieg,
    Japanese titles -- and the deduction is weighted x10, so a few accents
    push a correct author under Plex's 80-point auto-apply bar.

    to_text decodes instead of encoding, which fixes py2 AND removes the
    harness trap the file above documents: score_album's `.encode('utf-8')`
    made it undriveable here (bytes have no .replace under py3), so it had
    NEVER been tested. It can be now.
    """

    def tool(self, tagged_artist, tagged_album=u'A Book'):
        tool = ST.ScoreTool.__new__(ST.ScoreTool)
        tool.calculate_score = ST.Util.LevenshteinDistance

        class Media(object):
            artist = tagged_artist
            album = tagged_album

        class Helper(object):
            media = Media()

        tool.helper = Helper()
        return tool

    def test_to_text_converges_both_spellings(self):
        # The identity that makes the comparison meaningful at all.
        self.assertEqual(ST.to_text(u'Lu\u0301thien'), u'Lu\u0301thien')
        self.assertEqual(ST.to_text(u'Luthien'.encode('utf-8')), u'Luthien')
        self.assertEqual(ST.to_text(u'gro\xdfe'.encode('utf-8')), u'gro\xdfe')

    def test_an_exact_nonascii_author_scores_zero_deduction(self):
        # The bug: byte-tag vs unicode-JSON inflated this to a real deduction.
        name = u'Bj\xf6rn Andr\xe9asson'
        tool = self.tool(name.encode('utf-8'))
        self.assertEqual(tool.score_author(name), 0)

    def test_an_exact_nonascii_ALBUM_scores_zero_deduction(self):
        # score_album could not be driven from this harness at all before
        # to_text; this is its first test.
        title = u'Der gro\xdfe Bruderkrieg'
        tool = self.tool(u'X'.encode('utf-8'), tagged_album=title.encode('utf-8'))
        self.assertEqual(tool.score_album(title), 0)

    def test_a_genuinely_different_author_still_deducts(self):
        # The fix must not make everything match.
        tool = self.tool(u'Bj\xf6rn Andr\xe9asson'.encode('utf-8'))
        self.assertGreater(tool.score_author(u'Completely Different Person'), 0)


if __name__ == '__main__':
    unittest.main()
