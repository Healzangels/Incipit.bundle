"""
A thin provider record must never BLANK metadata that is already there.

WHY THIS EXISTS
    The 2026-07-28 mutation sweep found 27 survivors in update_tools alone,
    all of the same shape: every sparse-record wipe guard could be deleted
    with a fully green suite. Dropping `self.synopsis and`, `self.studio and`,
    `self.description and`, `self.name and` or `album_title and` lets a
    Hardcover/OpenLibrary record that simply lacks a field overwrite the
    operator's existing summary, studio, author bio or author NAME with
    empty string -- on a forced refresh, which is the one action an operator
    takes to FIX something.

    "The API answered" is not "the API answered completely". These tests
    drive the real setters against a metadata object that already has values.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plexenv  # noqa: E402

MODULES = plexenv.load()
UT = MODULES['update_tools']


class FakeMetadata(object):
    def __init__(self, **kw):
        self.summary = kw.get('summary', '')
        self.studio = kw.get('studio', '')
        self.title = kw.get('title', '')
        self.title_sort = kw.get('title_sort', '')


def album_tool(metadata, synopsis=None, studio=None, force=True):
    tool = UT.AlbumUpdateTool.__new__(UT.AlbumUpdateTool)
    tool.metadata = metadata
    tool.force = force
    tool.content_type = 'books'
    tool.synopsis = synopsis
    tool.studio = studio
    return tool


def artist_tool(metadata, description=None, name=None, force=True):
    tool = UT.ArtistUpdateTool.__new__(UT.ArtistUpdateTool)
    tool.metadata = metadata
    tool.force = force
    tool.content_type = 'authors'
    tool.description = description
    tool.name = name
    return tool


class TestSummaryIsNotBlanked(unittest.TestCase):
    def test_a_record_with_no_summary_leaves_the_existing_one(self):
        md = FakeMetadata(summary='An existing summary the operator can read.')
        album_tool(md, synopsis=None).set_metadata_summary()
        self.assertEqual(md.summary, 'An existing summary the operator can read.')

    def test_an_empty_string_summary_also_leaves_it(self):
        md = FakeMetadata(summary='An existing summary.')
        album_tool(md, synopsis='').set_metadata_summary()
        self.assertEqual(md.summary, 'An existing summary.')

    def test_a_real_summary_still_overwrites_on_force(self):
        md = FakeMetadata(summary='Old.')
        album_tool(md, synopsis='New and better.', force=True).set_metadata_summary()
        self.assertEqual(md.summary, 'New and better.')

    def test_a_real_summary_fills_an_empty_field_without_force(self):
        md = FakeMetadata(summary='')
        album_tool(md, synopsis='First summary.', force=False).set_metadata_summary()
        self.assertEqual(md.summary, 'First summary.')


class TestStudioIsNotBlanked(unittest.TestCase):
    def test_a_record_with_no_studio_leaves_the_existing_one(self):
        md = FakeMetadata(studio='Recorded Books')
        album_tool(md, studio=None).set_metadata_studio()
        self.assertEqual(md.studio, 'Recorded Books')

    def test_a_real_studio_still_overwrites_on_force(self):
        md = FakeMetadata(studio='Old Publisher')
        album_tool(md, studio='Tantor Audio').set_metadata_studio()
        self.assertEqual(md.studio, 'Tantor Audio')


class TestAuthorFieldsAreNotBlanked(unittest.TestCase):
    def test_a_record_with_no_bio_leaves_the_existing_one(self):
        md = FakeMetadata(summary='A hand-checked author biography.')
        artist_tool(md, description=None).set_metadata_description()
        self.assertEqual(md.summary, 'A hand-checked author biography.')

    def test_a_record_with_no_name_never_blanks_the_author_name(self):
        # The worst of the class: an author whose display name becomes ''.
        md = FakeMetadata(title='Terry Pratchett')
        artist_tool(md, name=None).set_metadata_title()
        self.assertEqual(md.title, 'Terry Pratchett')

    def test_an_empty_name_never_blanks_the_author_name(self):
        md = FakeMetadata(title='Terry Pratchett')
        artist_tool(md, name='').set_metadata_title()
        self.assertEqual(md.title, 'Terry Pratchett')

    def test_a_real_name_still_lands(self):
        md = FakeMetadata(title='')
        artist_tool(md, name='Susanna Clarke').set_metadata_title()
        self.assertEqual(md.title, 'Susanna Clarke')


if __name__ == '__main__':
    unittest.main()
