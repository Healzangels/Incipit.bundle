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
        keys = AG.offer_alternate_covers(h, shown=(PRIMARY, None))
        self.assertEqual(keys, ['http://x/other.jpg'])
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

    def test_WITHOUT_shown_every_acceptable_alternate_is_offered(self):
        # Callers that pass nothing must behave exactly as before this change.
        h = self.helper_offering(
            ['http://x/twin.jpg', 'http://x/other.jpg'],
            {'http://x/twin.jpg': TWIN, 'http://x/other.jpg': OTHER})
        keys = AG.offer_alternate_covers(h)
        self.assertEqual(keys, ['http://x/twin.jpg', 'http://x/other.jpg'])


if __name__ == '__main__':
    unittest.main()
