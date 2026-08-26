"""
PROVEN NEGATIVE: Plex's AgentKit has no Track type. Do not try this again.

WHAT WAS ATTEMPTED
    Every track in an incipit library carries a com.plexapp.agents.incipit guid,
    but the identifier was declared only for Agent.Artist and Agent.Album, so
    every track-level getAgent lookup failed and logged "Unable to find metadata
    agent provider for identifier". The obvious fix is to declare the identifier
    for tracks as well, by subclassing Agent.Track.

WHAT HAPPENED, live on .99 at v1.3.214 on 2026-08-26

    File ".../Contents/Code/__init__.py", line 5245, in <module>
        class AudiobookTrack(Agent.Track):
    AttributeError: 'AgentKit' object has no attribute 'Track'
    CRITICAL (core:615) - Exception starting plug-in

    Agent.Artist and Agent.Album register at class-definition time, so BOTH were
    already accepted when the third class raised. The result is the nastiest
    shape available: the plugin answers its prefs endpoint and declares two
    working agents, while "Exception starting plug-in" means it never started.
    /system/agents?mediaType=10 simply stays empty. It looks half-alive.

WHY THE TEST SUITE DID NOT CATCH IT -- the part worth remembering
    tests/plexenv.py fakes the framework globals. To make the new class import,
    Agent.Track was ADDED to that fake. 743 tests then passed against a bundle
    that could not start. **The harness modelled a type the real sandbox does not
    have**, which is the same blind spot as the py3-harness bytes trap: a fake
    that is more generous than the thing it fakes proves nothing. plexenv must
    only ever mirror what AgentKit actually exposes.

THE STANDING RULE
    The track-level getAgent errors cannot be fixed from the agent side. They are
    harmless -- nothing downstream fails, and the count is just how many tracks
    got touched. "Explained and left alone" is the end state, now for a measured
    reason rather than an assumed one.
"""

import ast
import os
import unittest

CODE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'Contents', 'Code')


def agent_subclass_bases(source):
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if (isinstance(base, ast.Attribute)
                        and getattr(base.value, 'id', None) == 'Agent'):
                    out.append((node.name, base.attr))
    return out


class AgentKitHasNoTrack(unittest.TestCase):
    """Static, because the runtime cost of getting this wrong is a dead plugin."""

    def sources(self):
        return [os.path.join(CODE_DIR, n) for n in sorted(os.listdir(CODE_DIR))
                if n.endswith('.py')]

    def test_nothing_subclasses_Agent_Track(self):
        offenders = []
        for path in self.sources():
            with open(path) as handle:
                for cls, attr in agent_subclass_bases(handle.read()):
                    if attr == 'Track':
                        offenders.append((os.path.basename(path), cls))
        self.assertEqual(
            offenders, [],
            "Agent.Track does not exist -- 'AgentKit' object has no attribute "
            "'Track'. Declaring it raises at import and takes the WHOLE plugin "
            "down with 'Exception starting plug-in', while Artist and Album "
            "still register so it looks half-alive. Proven live 2026-08-26. "
            "Offenders: %r" % (offenders,))

    def test_the_agent_types_we_DO_declare_are_the_two_that_exist(self):
        found = set()
        for path in self.sources():
            with open(path) as handle:
                found.update(attr for _, attr in agent_subclass_bases(handle.read()))
        self.assertEqual(found, {'Artist', 'Album'},
                         'the bundle declares an agent type outside the pair AgentKit '
                         'is known to expose: %r' % (sorted(found),))

    def test_the_guard_can_actually_see_an_offender(self):
        """Otherwise both assertions above pass vacuously on an empty parse."""
        bad = 'class AudiobookTrack(Agent.Track):\n    pass\n'
        self.assertEqual(agent_subclass_bases(bad), [('AudiobookTrack', 'Track')])

    def test_the_fake_framework_does_not_invent_a_Track_type(self):
        """The harness gap that let v1.3.214 ship. plexenv must not be generous."""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'plexenv.py')) as handle:
            body = handle.read()
        self.assertNotIn(
            "Track=type('Track'", body,
            'plexenv fakes an Agent.Track that AgentKit does not have; a bundle '
            'that cannot start would pass the whole suite again.')


if __name__ == '__main__':
    unittest.main()
