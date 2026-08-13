"""
An alternate cover that is the picture ALREADY on display is not a choice.

WHY THIS EXISTS
    `offer_alternate_covers` promises "real choice rather than duplicate
    tiles", but only ever deduped by URL. Measured live 2026-08-12 on prod
    Lamb (rk 740608): the container held our 500x500 cover and, borrowed from
    a near-tie row scoring 99, the SAME artwork at 1024x1024. Connor read the
    second tile as the feature misfiring; it was the feature working and then
    offering a twin.

    The verdict needed already existed and simply was not consulted here --
    /images/similar scored that pair distance 2 (similar) and scored the
    genuinely different OverDrive jacket 26.

    Two things must not rot:

      - a REDUNDANT alternate must never reach remember_alternate_refusal.
        That memo is module-global and keyed on the url alone, so a
        contextual verdict filed there would suppress the picture for every
        OTHER book in the library.
      - the checks FAIL OPEN. No verdict means offer it: a duplicate tile is
        cosmetic, a hidden cover option is not.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
AG = MODULES['agent']

PRIMARY = b'PRIMARY-COVER-BYTES'
TWIN = b'SAME-PICTURE-BIGGER'      # same artwork, different bytes
OTHER = b'A-GENUINELY-DIFFERENT-COVER'
LOCAL = b'LOCAL-COVER-JPG-BYTES'


class TwinBase(unittest.TestCase):
    def setUp(self):
        self.real = {n: getattr(AG, n) for n in (
            'same_image', 'perceptual_dedupe_enabled', 'aspect_could_match',
            'images_similar_via_api', 'fetch_url_bytes',
            'alternate_cover_acceptable', 'alternate_refused_recently',
            'remember_alternate_refusal')}
        self.refusals = []
        self.consults = []
        AG.same_image = lambda a, b: a == b
        AG.perceptual_dedupe_enabled = lambda: True
        AG.aspect_could_match = lambda a, b: True
        AG.alternate_cover_acceptable = lambda data: True
        AG.alternate_refused_recently = lambda url: False
        AG.remember_alternate_refusal = lambda url: self.refusals.append(url)
        # TWIN is the same picture as PRIMARY; everything else differs.
        def similar(a, b, tag):
            self.consults.append((a, b))
            return {PRIMARY, TWIN} == {a, b}
        AG.images_similar_via_api = similar

    def tearDown(self):
        for n, f in self.real.items():
            setattr(AG, n, f)

    def helper_offering(self, alternates, blobs):
        AG.fetch_url_bytes = lambda url: blobs.get(url)

        class FakeMetadata(object):
            def __init__(self):
                self.posters = {}

        class FakeHelper(object):
            def __init__(self):
                self.metadata = FakeMetadata()
                self.thumb_alternates = list(alternates)

        return FakeHelper()


class AlternateAlreadyOnDisplay(TwinBase):
    def test_byte_identical_to_the_shown_cover_is_a_twin(self):
        self.assertTrue(AG.alternate_already_on_display(PRIMARY, (PRIMARY,), 'u'))
        # Byte identity needs no api at all.
        self.assertEqual(self.consults, [])

    def test_the_SAME_PICTURE_at_a_different_size_is_a_twin(self):
        # The live Lamb case: 500x500 vs 1024x1024 of one design.
        self.assertTrue(AG.alternate_already_on_display(TWIN, (PRIMARY,), 'u'))

    def test_a_genuinely_different_cover_is_still_offered(self):
        self.assertFalse(AG.alternate_already_on_display(OTHER, (PRIMARY,), 'u'))

    def test_it_checks_EVERY_shown_image_not_just_the_first(self):
        # The local cover.jpg is on display too, so an alternate matching it
        # is equally a twin.
        self.assertTrue(AG.alternate_already_on_display(LOCAL, (PRIMARY, LOCAL), 'u'))

    def test_no_verdict_FAILS_OPEN(self):
        AG.images_similar_via_api = lambda a, b, tag: None
        self.assertFalse(AG.alternate_already_on_display(TWIN, (PRIMARY,), 'u'))

    def test_the_pref_gates_the_perceptual_leg_but_never_byte_identity(self):
        AG.perceptual_dedupe_enabled = lambda: False
        self.assertFalse(AG.alternate_already_on_display(TWIN, (PRIMARY,), 'u'))
        self.assertEqual(self.consults, [])
        # Byte-identical is not a perceptual judgement and stays suppressed.
        self.assertTrue(AG.alternate_already_on_display(PRIMARY, (PRIMARY,), 'u'))

    def test_a_clearly_different_SHAPE_is_not_consulted(self):
        AG.aspect_could_match = lambda a, b: False
        self.assertFalse(AG.alternate_already_on_display(TWIN, (PRIMARY,), 'u'))
        self.assertEqual(self.consults, [])

    def test_nothing_on_display_yet_means_nothing_to_be_a_twin_of(self):
        for shown in (None, (), (None,)):
            self.assertFalse(AG.alternate_already_on_display(TWIN, shown, 'u'))


class OfferAlternatesSkipsTwins(TwinBase):
    def test_the_twin_is_not_offered_and_the_real_alternative_is(self):
        h = self.helper_offering(
            ['http://x/twin.jpg', 'http://x/other.jpg'],
            {'http://x/twin.jpg': TWIN, 'http://x/other.jpg': OTHER})
        keys, stale = AG.offer_alternate_covers(h, shown=(PRIMARY, None))
        self.assertEqual(keys, ['http://x/other.jpg'])
        # Never offered, so there is no tile to prune: declining is enough.
        self.assertEqual(stale, [])
        self.assertNotIn('http://x/twin.jpg', h.metadata.posters)
        self.assertIn('http://x/other.jpg', h.metadata.posters)

    def test_A_REDUNDANT_ALTERNATE_IS_NEVER_FILED_AS_A_REFUSAL(self):
        # The trap: that memo is keyed on the url alone and is module-global,
        # so "already shown on THIS book" filed there would hide the picture
        # on every other book that legitimately offers it.
        h = self.helper_offering(['http://x/twin.jpg'], {'http://x/twin.jpg': TWIN})
        AG.offer_alternate_covers(h, shown=(PRIMARY,))
        self.assertEqual(self.refusals, [])

    def test_an_UNUSABLE_alternate_is_still_filed_as_a_refusal(self):
        # The pre-existing behaviour must survive: not-square art is a
        # property of the url and stays memoised.
        AG.alternate_cover_acceptable = lambda data: False
        h = self.helper_offering(['http://x/bad.jpg'], {'http://x/bad.jpg': OTHER})
        AG.offer_alternate_covers(h, shown=(PRIMARY,))
        self.assertEqual(self.refusals, ['http://x/bad.jpg'])

class AlreadyOfferedAlternatesAreStillJudged(TwinBase):
    """
        The half-fix that v1.3.208 shipped, and the case that had no test.

        The loop short-circuited on `url in helper.metadata.posters` -- "a
        re-offer costs a fetch and changes nothing" -- which stopped being
        true the moment re-offering became a DECISION. Measured on .99 with
        the v1.3.209 diagnostic build (Gravesong): "3 alternate(s), 2 shown
        image(s) to judge against" then "ALREADY in the container -- kept
        WITHOUT being judged". So a twin an older version had already offered
        was re-added forever, and only containers rebuilt from scratch ever
        looked fixed.
    """

    def fetches(self):
        calls = []
        real = AG.fetch_url_bytes
        return calls, real

    def test_a_twin_ALREADY_in_the_container_is_dropped_from_the_keep_list(self):
        h = self.helper_offering(['http://x/twin.jpg'], {'http://x/twin.jpg': TWIN})
        # It is already on display, exactly as an older version left it.
        h.metadata.posters['http://x/twin.jpg'] = 'existing'
        keys, stale = AG.offer_alternate_covers(h, shown=(PRIMARY,))
        # Absent from the keep list is what lets validate_keys prune it.
        self.assertEqual(keys, [])
        self.assertEqual(self.refusals, [])
        # ...and NAMED, so the caller can prune it even when the membership
        # branch that owns the keep list never runs (the prod Lamb case).
        self.assertEqual(stale, ['http://x/twin.jpg'])

    def test_a_GENUINE_alternate_already_in_the_container_is_KEPT(self):
        # The destructive direction: judging must not evict art that is fine.
        h = self.helper_offering(['http://x/other.jpg'], {'http://x/other.jpg': OTHER})
        h.metadata.posters['http://x/other.jpg'] = 'existing'
        keys, stale = AG.offer_alternate_covers(h, shown=(PRIMARY,))
        self.assertEqual(keys, ['http://x/other.jpg'])
        # And NOT re-written into the container. Falling through to the offer
        # block would put a fresh Proxy.Media over an entry Plex already holds
        # -- a redundant re-offer, which is how the serialize traps mint a
        # twin. Asserting only the keep list cannot see that: the url lands in
        # `added` either way, so this is the assertion that gives the branch
        # its keep (it survived mutation with the keep-list check alone).
        self.assertEqual(h.metadata.posters['http://x/other.jpg'], 'existing')

    def test_with_NOTHING_to_judge_against_the_free_path_is_kept(self):
        # No shown images means the twin question cannot be asked, so an
        # already-offered alternate must cost neither a fetch nor its place.
        fetched = []
        blobs = {'http://x/other.jpg': OTHER}
        h = self.helper_offering(['http://x/other.jpg'], blobs)
        inner = AG.fetch_url_bytes

        def counting(url):
            fetched.append(url)
            return inner(url)

        AG.fetch_url_bytes = counting
        h.metadata.posters['http://x/other.jpg'] = 'existing'
        keys, stale = AG.offer_alternate_covers(h, shown=(None, None))
        self.assertEqual(keys, ['http://x/other.jpg'])
        self.assertEqual(fetched, [], 'must not re-fetch when it cannot judge')


class AlternatesAreJudgedAgainstEachOther(TwinBase):
    """
        Two alternates that are the same picture as EACH OTHER.

        Each was compared only against the cover, never against its
        predecessor, so both passed. Measured on .99 over 30 albums
        (v1.3.210): 29 duplicate tiles went and exactly one pair survived --
        The Heroes, two of its six alternates identical at distance 0.
    """

    def test_the_SECOND_copy_of_one_picture_is_not_offered(self):
        # Neither matches the cover; they match one another.
        h = self.helper_offering(
            ['http://x/a.jpg', 'http://x/b.jpg'],
            {'http://x/a.jpg': TWIN, 'http://x/b.jpg': TWIN})
        keys, stale = AG.offer_alternate_covers(h, shown=(OTHER,))
        self.assertEqual(keys, ['http://x/a.jpg'])
        self.assertNotIn('http://x/b.jpg', h.metadata.posters)

    def test_two_DIFFERENT_alternates_are_both_offered(self):
        # The destructive direction: judging them against each other must not
        # collapse genuinely different art.
        h = self.helper_offering(
            ['http://x/a.jpg', 'http://x/b.jpg'],
            {'http://x/a.jpg': TWIN, 'http://x/b.jpg': LOCAL})
        keys, stale = AG.offer_alternate_covers(h, shown=(OTHER,))
        self.assertEqual(keys, ['http://x/a.jpg', 'http://x/b.jpg'])

    def test_a_twin_of_an_ALREADY_OFFERED_alternate_is_not_added(self):
        # The container already holds A from an earlier pass; B is the same
        # picture. A is on display, so B is a twin of it -- and A only counts
        # as something to compare against if the keep path feeds `judged`.
        h = self.helper_offering(
            ['http://x/a.jpg', 'http://x/b.jpg'],
            {'http://x/a.jpg': TWIN, 'http://x/b.jpg': TWIN})
        h.metadata.posters['http://x/a.jpg'] = 'existing'
        keys, stale = AG.offer_alternate_covers(h, shown=(OTHER,))
        self.assertEqual(keys, ['http://x/a.jpg'])
        self.assertNotIn('http://x/b.jpg', h.metadata.posters)

    def test_an_alternate_that_FAILED_to_land_does_not_suppress_a_later_copy(self):
        # It is not on display, so it cannot be the reason to hide a twin --
        # otherwise one failed container write loses the picture entirely.
        class Boom(dict):
            def __setitem__(self, key, value):
                if key == 'http://x/a.jpg':
                    raise IOError('container write failed')
                dict.__setitem__(self, key, value)

        h = self.helper_offering(
            ['http://x/a.jpg', 'http://x/b.jpg'],
            {'http://x/a.jpg': TWIN, 'http://x/b.jpg': TWIN})
        h.metadata.posters = Boom()
        keys, stale = AG.offer_alternate_covers(h, shown=(OTHER,))
        self.assertEqual(keys, ['http://x/b.jpg'])


class OfferAlternatesSkipsTwinsMore(TwinBase):
    def test_WITHOUT_shown_every_acceptable_alternate_is_offered(self):
        # Callers that pass nothing must behave exactly as before this change.
        h = self.helper_offering(
            ['http://x/twin.jpg', 'http://x/other.jpg'],
            {'http://x/twin.jpg': TWIN, 'http://x/other.jpg': OTHER})
        keys, stale = AG.offer_alternate_covers(h)
        self.assertEqual(keys, ['http://x/twin.jpg', 'http://x/other.jpg'])




class TwinPruneKeepList(unittest.TestCase):
    """
        The keep list for the stale-twin prune, built by SUBTRACTION.

        The safety argument lives here. cover_keep_list CONSTRUCTS a keep list
        from the online/local decisions, and in the very state this prune exists
        for it returns EMPTY (thumb withheld, mirror skipped) -- so reusing it
        would call validate_keys([]) and empty our namespace, which on a
        thumb-less match can hold the only copy of the operator's curated
        cover.jpg. Subtracting means nothing can be lost that was not named.
    """

    def test_only_the_condemned_key_is_dropped(self):
        keep = AG.twin_prune_keep_list(
            ['http://x/cover.jpg', 'incipit-local-cover', 'http://x/twin.jpg'],
            ['http://x/twin.jpg'])
        self.assertEqual(keep, ['http://x/cover.jpg', 'incipit-local-cover'])

    def test_our_online_and_local_entries_SURVIVE(self):
        # The existing membership branch owns those decisions; this prune must
        # not borrow them. Both are kept even though cover_keep_list, in this
        # state, would have dropped both.
        keep = AG.twin_prune_keep_list(['http://x/cover.jpg', 'incipit-local-cover'], [])
        self.assertEqual(keep, ['http://x/cover.jpg', 'incipit-local-cover'])

    def test_nothing_condemned_means_nothing_removed(self):
        mine = ['http://x/a.jpg', 'http://x/b.jpg']
        self.assertEqual(AG.twin_prune_keep_list(mine, []), mine)
        self.assertEqual(AG.twin_prune_keep_list(mine, None), mine)

    def test_a_duplicate_candidate_lands_once(self):
        # The online cover can also appear among the alternates.
        keep = AG.twin_prune_keep_list(
            ['http://x/cover.jpg', 'http://x/cover.jpg'], [])
        self.assertEqual(keep, ['http://x/cover.jpg'])

    def test_falsy_keys_never_enter_the_keep_list(self):
        # helper.thumb_secondary is routinely None.
        self.assertEqual(
            AG.twin_prune_keep_list([None, '', 'http://x/a.jpg'], []), ['http://x/a.jpg'])

    def test_condemning_EVERYTHING_yields_an_empty_list_for_the_caller_to_refuse(self):
        # The function reports the truth; the CALLER fails closed on it rather
        # than handing validate_keys an empty list.
        self.assertEqual(AG.twin_prune_keep_list(['http://x/a.jpg'], ['http://x/a.jpg']), [])


class TwinPruneIsWiredIn(unittest.TestCase):
    """The branch itself: a prune nobody calls removes nothing."""

    SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '..', 'Contents', 'Code', '__init__.py')

    def source(self):
        with open(self.SRC) as handle:
            return handle.read()

    def test_the_branch_is_keyed_on_what_was_condemned(self):
        self.assertIn('elif stale_alternates:', self.source())

    def code_of_branch(self):
        """The branch's CODE, comments stripped.

        Its comments name `cover_keep_list` and `validate_keys` to explain why
        it avoids them, so a plain substring test reads a warning as a
        violation -- which is exactly what it did first time round.
        """
        src = self.source()
        branch = src[src.index('elif stale_alternates:'):]
        branch = branch[:branch.index('posters.validate_keys(')]
        return '\n'.join(l for l in branch.splitlines()
                          if not l.strip().startswith('#'))

    def test_it_does_NOT_reuse_cover_keep_list(self):
        # The one-line "fix" that empties the namespace.
        self.assertNotIn('cover_keep_list(', self.code_of_branch())

    def test_it_fails_closed_on_an_empty_keep_list(self):
        src = self.source()
        branch = src[src.index('elif stale_alternates:'):]
        head = branch[:branch.index('posters.validate_keys(')]
        self.assertIn('if not keep:', head)

    def test_the_prune_announces_itself_at_WARN(self):
        # Prod ships at WARN. An info-level prune is invisible exactly where it
        # needs to be auditable -- the file's meta-test enforces this too.
        src = self.source()
        branch = src[src.index('elif stale_alternates:'):]
        branch = branch[:branch.index('posters.validate_keys(')]
        self.assertIn('log.warn(', branch)

class TwinPruneCandidates(unittest.TestCase):
    """Which keys the prune is allowed to KEEP. Omitting one deletes it."""

    def test_the_local_mirror_key_is_a_candidate(self):
        # Spec invariant 4. It is never condemned, but if it is not a candidate
        # the subtraction cannot keep it and validate_keys drops the operator's
        # curated cover entry. A mutation removing it survived every other test.
        mine = AG.twin_prune_candidates('http://x/cover.jpg', None,
                                        'incipit-local-cover', [])
        self.assertIn('incipit-local-cover', mine)

    def test_the_online_cover_and_its_secondary_are_candidates(self):
        mine = AG.twin_prune_candidates('http://x/a.jpg', 'http://x/b.jpg', 'L', [])
        self.assertIn('http://x/a.jpg', mine)
        self.assertIn('http://x/b.jpg', mine)

    def test_surviving_alternates_are_candidates(self):
        mine = AG.twin_prune_candidates('T', None, 'L', ['http://x/alt.jpg'])
        self.assertIn('http://x/alt.jpg', mine)

    def test_no_alternates_is_not_an_error(self):
        self.assertEqual(AG.twin_prune_candidates('T', None, 'L', None), ['T', None, 'L'])


class TwinPruneUsesTheSubtraction(unittest.TestCase):
    """
        The branch must compute its keep list by SUBTRACTION.

        A mutation replacing it with `keep = list(stale_alternates)` -- keep
        ONLY the condemned tile, prune everything else of ours -- survived every
        behavioural test, because nothing exercises the branch end to end. This
        pins the wiring the way the offer-is-invoked tests do.
    """

    SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '..', 'Contents', 'Code', '__init__.py')

    def test_the_branch_calls_twin_prune_keep_list_with_the_condemned_set(self):
        with open(self.SRC) as handle:
            src = handle.read()
        branch = src[src.index('elif stale_alternates:'):]
        branch = branch[:branch.index('posters.validate_keys(')]
        self.assertIn('twin_prune_keep_list(present, stale_alternates)', branch)
        # The ASSIGNMENT shape, not just a mention: `mine = [x] or
        # twin_prune_candidates(...)` keeps the call in the text while
        # bypassing it, and a bare substring test passes on that.
        self.assertIn('mine = twin_prune_candidates(', branch)




class TheSelectionRail(unittest.TestCase):
    """
        v1.3.212 shipped the prune with NO selection rail, and its own spec
        listed one as invariant 5. Found by a pre-sweep snapshot of prod, not
        by the tests: 23 of 1650 albums select one of OUR metadata tiles and 9
        of those carry an alternate matching the selected picture. Lamb
        survived only because its keep list came out empty first.
    """

    OURS = ('metadata://posters/com.plexapp.agents.incipit_'
            'b20a3838f4cfcf0c8d6c5e626198600ea5db6741')
    UPLOAD = 'upload://posters/com.plexapp.agents.incipit_b20a3838f4cfcf0c8d6c'
    LMA = 'metadata://posters/com.plexapp.agents.localmedia_69c1c6c5dc0578ec18'

    def test_our_own_selected_tile_BLOCKS_the_prune(self):
        self.assertTrue(AG.twin_prune_blocked_by_selection(('1', self.OURS, [], None)))

    def test_an_UPLOAD_selection_does_not_block(self):
        # validate_keys cannot touch an upload, and prod Lamb proves the key
        # can still carry our agent name -- matching on the name alone would
        # block the prune on nearly every album.
        self.assertFalse(AG.twin_prune_blocked_by_selection(('1', self.UPLOAD, [], None)))

    def test_another_agents_selection_does_not_block(self):
        self.assertFalse(AG.twin_prune_blocked_by_selection(('1', self.LMA, [], None)))

    def test_no_selection_at_all_does_not_block(self):
        self.assertFalse(AG.twin_prune_blocked_by_selection(('1', None, [], None)))

    def test_UNREADABLE_state_FAILS_CLOSED(self):
        # A blip must never be read as "nothing of the operator's is selected".
        for bad in (None, (), False):
            self.assertTrue(AG.twin_prune_blocked_by_selection(bad), repr(bad))

if __name__ == '__main__':
    unittest.main()
