"""
The track agent exists to be REGISTERED, not to write anything.

WHY IT EXISTS
    Plex stamps a com.plexapp.agents.incipit guid onto every track in an incipit
    library, but the identifier was declared only for Agent.Artist and
    Agent.Album. So every track-level getAgent lookup failed by construction:
    "Unable to find metadata agent provider for identifier
    'com.plexapp.agents.incipit'". The count is just how many tracks got touched
    -- 3,849 tracks in section 6 against 3,864 errors in one hour, fifteen apart.

WHY IT MUST NOT WRITE
    These track titles come from the FILE TAGS via localmedia. This agent has no
    track metadata of its own. A track agent that owns the type (primary_provider
    = True) and writes nothing in update() blanks the title of every track in the
    library -- thousands of them, with no way to put them back short of a rescan.

    So the two properties below are the safety contract, and they are pinned
    STATICALLY: a runtime test would need the whole Plex framework, and by the
    time it ran the titles would already be gone.
"""

import ast
import os
import unittest

CODE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'Contents', 'Code', '__init__.py')


def track_agent_class(source=None):
    with open(CODE) as handle:
        tree = ast.parse(source if source is not None else handle.read())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == 'AudiobookTrack':
            return node
    return None


def assigned_value(cls, name):
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    return None


def method(cls, name):
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


class TrackAgentIsRegistrationOnly(unittest.TestCase):
    def test_the_class_exists_and_subclasses_agent_track(self):
        cls = track_agent_class()
        self.assertIsNotNone(cls, 'AudiobookTrack is gone -- the track-level '
                                  'getAgent errors will be back')
        bases = ['.'.join(filter(None, [getattr(b.value, 'id', None), b.attr]))
                 for b in cls.bases if isinstance(b, ast.Attribute)]
        self.assertIn('Agent.Track', bases)

    def test_it_is_NOT_the_primary_provider(self):
        """The one property that stands between this agent and 4,000 blank titles."""
        value = assigned_value(track_agent_class(), 'primary_provider')
        self.assertIsNotNone(value, 'primary_provider is not declared at all')
        self.assertIsInstance(value, ast.Constant)
        self.assertIs(value.value, False,
                      'primary_provider=True makes this agent OWN track metadata. '
                      'It has none, so Plex blanks every track title in the '
                      'library. Do not flip this without capturing every title '
                      'first and diffing them after.')

    def test_update_writes_nothing(self):
        """No assignment to metadata.* anywhere in update()."""
        fn = method(track_agent_class(), 'update')
        self.assertIsNotNone(fn, 'update() is gone')
        writes = [
            ast.dump(t) for node in ast.walk(fn) if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Attribute) and getattr(t.value, 'id', None) == 'metadata'
        ]
        self.assertEqual(writes, [], 'update() assigns to metadata -- this agent '
                                     'has no track data and must never write')

    def test_search_offers_nothing(self):
        """No results.Append(...) -- a track "match" could overwrite a good tag."""
        fn = method(track_agent_class(), 'search')
        self.assertIsNotNone(fn, 'search() is gone')
        appends = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == 'Append']
        self.assertEqual(appends, [], 'search() offers a track result')

    def test_the_guards_can_actually_see_an_offender(self):
        """Without this, every assertion above could be passing vacuously."""
        bad = ('class AudiobookTrack(Agent.Track):\n'
               '    primary_provider = True\n'
               '    def update(self, metadata, media, lang, force):\n'
               '        metadata.title = ""\n'
               '    def search(self, results, media, lang, manual):\n'
               '        results.Append(1)\n')
        cls = track_agent_class(source=bad)
        self.assertIsNotNone(cls)
        self.assertIs(assigned_value(cls, 'primary_provider').value, True)
        fn = method(cls, 'update')
        writes = [t for node in ast.walk(fn) if isinstance(node, ast.Assign)
                  for t in node.targets
                  if isinstance(t, ast.Attribute) and getattr(t.value, 'id', None) == 'metadata']
        self.assertEqual(len(writes), 1, 'the metadata-write guard sees nothing')
        appends = [n for n in ast.walk(method(cls, 'search'))
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == 'Append']
        self.assertEqual(len(appends), 1, 'the search guard sees nothing')


if __name__ == '__main__':
    unittest.main()
