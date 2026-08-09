"""
The agent notices, by itself, when the AUDIO does not match the RECORD.

WHY THIS EXISTS
    On 2026-08-09 a hand-run sweep of the .99 library compared every album's
    analyzed duration against the runtime of the record it had matched, and
    found 52 albums serving the wrong edition -- including four matched to
    ABRIDGED records at under half the file's length, two files that contained
    the whole book TWICE, and several holding only part of it.

    Every number that sweep needed was already sitting in the agent's hands at
    update() time: Plex's analyzed duration on one side, the API record's
    runtimeLengthMin on the other. Nothing compared them. So the defect could
    only ever be found by someone going looking.

    It now compares them on every refresh and says so at WARN. The agent cannot
    REPAIR this -- Plex only re-points an album via Fix Match, never from
    update() -- so the useful thing it can do is make the defect impossible to
    miss without anyone sweeping anything.

THE THRESHOLDS ARE MEASURED, NOT GUESSED
    The 10% band is where the sweep drew the line: inside it sat every
    legitimate edition difference, outside it sat every real defect. The
    whole-multiple and clean-fraction rules come from the container inspection
    that followed -- Harry Potter 1 at 2.00x had chapters 1-17 twice, and The
    Trouble with Peace at 2.00x repeated chapter 001 at the exact midpoint.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
ST = MODULES['search_tools']


def ms(minutes):
    return int(minutes * 60000)


class TestRuntimeVerdict(unittest.TestCase):
    def test_a_matching_file_is_ok(self):
        self.assertEqual(ST.runtime_verdict(ms(757), 758)[0], 'ok')
        self.assertEqual(ST.runtime_verdict(ms(1646), 1645)[0], 'ok')

    def test_the_real_DUPLICATED_files_from_this_library(self):
        # Harry Potter 1: 997 min of audio for a 498 min book. Container
        # inspection found chapters 1-17 present twice.
        self.assertEqual(ST.runtime_verdict(ms(997), 498)[0], 'duplicated')
        # The Trouble with Peace: chapter 001 repeats at the exact midpoint.
        self.assertEqual(ST.runtime_verdict(ms(2633), 1317)[0], 'duplicated')
        # The Land: Swarm, ~3.9x.
        self.assertEqual(ST.runtime_verdict(ms(3792), 974)[0], 'duplicated')

    def test_the_real_PARTIAL_files_from_this_library(self):
        self.assertEqual(ST.runtime_verdict(ms(599), 1185)[0], 'partial')   # Inkdeath
        self.assertEqual(ST.runtime_verdict(ms(325), 678)[0], 'partial')    # False Gods
        self.assertEqual(ST.runtime_verdict(ms(394), 1172)[0], 'partial')   # Soldiers Live

    def test_the_real_ABRIDGED_mismatches_from_this_library(self):
        # These are the ones that mattered most: a good file matched to an
        # abridged record. Not a clean multiple, so they must land in the
        # generic mismatch bucket rather than being mislabelled a file defect.
        for file_min, rec_min in ((707, 262), (758, 366), (588, 278), (479, 181)):
            kind, _ratio = ST.runtime_verdict(ms(file_min), rec_min)
            self.assertEqual(kind, 'mismatch', '%s vs %s' % (file_min, rec_min))

    def test_silence_is_never_agreement(self):
        # A fresh scan has no duration yet, and a record may carry no runtime.
        # Returning 'ok' for either would report a clean bill of health the
        # agent has not earned.
        self.assertIsNone(ST.runtime_verdict(None, 500))
        self.assertIsNone(ST.runtime_verdict(0, 500))
        self.assertIsNone(ST.runtime_verdict(ms(500), 0))
        self.assertIsNone(ST.runtime_verdict(ms(500), None))
        self.assertIsNone(ST.runtime_verdict('junk', 500))
        self.assertIsNone(ST.runtime_verdict(ms(-5), 500))

    def test_the_tolerance_band_edges(self):
        # Just inside 10% is fine; just outside is a mismatch. A legitimate
        # edition difference (author's note, different intro) lives in here.
        self.assertEqual(ST.runtime_verdict(ms(109), 100)[0], 'ok')
        self.assertEqual(ST.runtime_verdict(ms(91), 100)[0], 'ok')
        self.assertEqual(ST.runtime_verdict(ms(115), 100)[0], 'mismatch')
        self.assertEqual(ST.runtime_verdict(ms(85), 100)[0], 'mismatch')

    def test_a_ratio_is_returned_so_the_log_can_show_it(self):
        kind, ratio = ST.runtime_verdict(ms(997), 498)
        self.assertEqual(kind, 'duplicated')
        self.assertAlmostEqual(ratio, 2.0, places=1)


class FakePart(object):
    def __init__(self, duration):
        self.duration = duration


class FakeItem(object):
    def __init__(self, parts):
        self.parts = parts


class FakeTrack(object):
    def __init__(self, items):
        self.items = items


def media_with(part_durations):
    class FakeMedia(object):
        tracks = [FakeTrack([FakeItem([FakePart(d)])]) for d in part_durations]
    return FakeMedia()


class TestMediaDurationProbe(unittest.TestCase):
    """
        ONE probe, shared by the search hint and the update-time check.

        A second copy is how the two drift, which cost this project three
        separate mirror-drift bugs on 2026-08-08 alone.
    """

    def test_sums_every_part(self):
        self.assertEqual(ST.media_duration_ms(media_with(['60000', '30000'])), 90000)

    def test_a_PARTIAL_analysis_yields_nothing(self):
        # Plex reports -1 for a not-yet-analyzed file. Summing only the analyzed
        # parts gives a too-SHORT total, which would read as a runtime mismatch
        # against the CORRECT edition -- turning the guard onto the right match.
        self.assertIsNone(ST.media_duration_ms(media_with(['60000', '-1'])))
        self.assertIsNone(ST.media_duration_ms(media_with(['60000', None])))
        self.assertIsNone(ST.media_duration_ms(media_with(['60000', 'junk'])))

    def test_a_dict_of_tracks_iterates_by_VALUE(self):
        # The legacy album media object keys tracks by index; iterating the dict
        # itself yields strings and finds no parts at all.
        class FakeMedia(object):
            tracks = {'1': FakeTrack([FakeItem([FakePart('60000')])]),
                      '2': FakeTrack([FakeItem([FakePart('30000')])])}
        self.assertEqual(ST.media_duration_ms(FakeMedia()), 90000)

    def test_a_broken_media_object_returns_None_not_an_exception(self):
        class Exploding(object):
            @property
            def tracks(self):
                raise ValueError('no tracks here')
        self.assertIsNone(ST.media_duration_ms(Exploding()))

    def test_BOTH_callers_use_it_the_search_hint_and_the_update_check(self):
        # A unit test of the probe passes whether or not anyone calls it, which
        # is exactly how a second copy gets written. Pin the wiring at source.
        code = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Contents', 'Code')
        search = open(os.path.join(code, 'search_tools.py')).read()
        update = open(os.path.join(code, 'update_tools.py')).read()
        agent = open(os.path.join(code, '__init__.py')).read()
        self.assertIn('duration = media_duration_ms(self.media)', search)
        self.assertIn('media_duration_ms(self.media)', update)
        self.assertIn('runtime_verdict(', update)
        # ...and the check is actually CALLED from the album update, not merely
        # defined. A diagnostic nobody invokes is the same as no diagnostic.
        self.assertIn('update_helper.report_runtime_mismatch()', agent)

    def test_the_probe_body_exists_only_once(self):
        code = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Contents', 'Code')
        total = 0
        for fn in ('search_tools.py', 'update_tools.py', '__init__.py'):
            total += open(os.path.join(code, fn)).read().count('track_iter = tracks.values()')
        self.assertEqual(total, 1, 'the duration probe has been copied again')


if __name__ == '__main__':
    unittest.main()
