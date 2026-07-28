# Import internal tools
from logging import Logging
from region_tools import RegionTool
from search_tools import MULTI_AUTHOR_RE, name_key, quote_param
import re
import struct
import StringIO
import urllib

# Setup logger
log = Logging()

# A subtitle that is really just a series/format descriptor rather than a genuine
# subtitle. Providers (esp. Hardcover) stash the series in the subtitle field, so
# appending it clutters the display title ("Night Watch: Discworld Novel 26") and
# can even contradict the series position used for sorting. Matches "Book 29",
# "Discworld Novel 26", "(Xanth #3)", or "...Novel" at the end ("A Discworld
# Novel"); a real subtitle like "A Novel of the Cosmere" ends in a word, not
# "Novel", so it is kept. (No leading-underscore name — the Plex sandbox forbids
# it.)
SERIES_SUBTITLE_RE = re.compile(
    r'\b(?:book|novel|volume|vol|part|episode)\s+\d+\b|#\s*\d+|\bnovel\s*$',
    re.IGNORECASE
)

# A trailing series descriptor a provider baked into the TITLE itself, e.g.
# "Changes: The Dresden Files, Book 12" -> "Changes". Each pattern requires an
# explicit number marker ("Book N" / "#N"), so a plain subtitle ("Changes: A
# Novel", "The Law: A Dresden Files Novel") has no number and is never stripped
# -- only an unambiguous series+number tail. Mirrors the incipit-api
# SERIES_SUFFIX so the DISPLAY title matches how the book was matched.
SERIES_TITLE_SUFFIX_RE = [
    # An ASCII HYPHEN counts as a series delimiter only when whitespace
    # precedes it: with the bare [\-] class a MID-WORD hyphen qualified, and
    # 'Harry Potter and the Half-Blood Prince, Book 6' stripped from the '-'
    # on, displaying 'Harry Potter and the Half' through two rebuilds
    # (2026-07-27). Em and en dashes never appear mid-word, so requiring the
    # space for them too was over-tight and left the series name in the title
    # ('Wintersteel—Cradle, Book 8' -> 'Wintersteel—Cradle', 2026-07-28).
    # Colons keep matching unspaced ('Wintersteel: Cradle, Book 8').
    re.compile(r'(?:\s*[:–—]|\s+-)\s*[^:]*?\bbook\s+\d+\s*$', re.IGNORECASE),
    re.compile(r'\s*\([^)]*#\s*\d+\s*\)\s*$'),
    re.compile(r'\s*,\s*book\s+\d+\s*$', re.IGNORECASE),
]

# A "subtitle" that is really marketing/promo copy, not a genuine subtitle.
# Hardcover in particular stashes blurb text in the subtitle field ("from the
# Bestselling Series That Inspired BBC's the Watch", "A Fun-Filled Adventure in
# the Magical Land of Xanth"), which bloats the album title. A real subtitle is
# short ("A Novel", "A Jack Ryan Novel"); a long one, or one with promo phrasing,
# is dropped so the display title stays the clean book name.
MARKETING_SUBTITLE_RE = re.compile(
    r'\bbestsell|\binspired\b|\bnow a\b|\bmajor (?:motion|tv|television)\b|'
    r'#\s*1\b|\baward[- ]winning\b|\bnew york times\b|\bsunday times\b',
    re.IGNORECASE
)
# A real subtitle is brief; beyond this it is almost always a descriptive blurb.
MARKETING_SUBTITLE_MAX_WORDS = 6


def is_marketing_subtitle(subtitle):
    """
        True when a provider "subtitle" is promotional/descriptive blurb rather
        than a genuine subtitle, so it should NOT be appended to the title.
    """
    if not subtitle:
        return False
    if len(subtitle.split()) > MARKETING_SUBTITLE_MAX_WORDS:
        return True
    return bool(MARKETING_SUBTITLE_RE.search(subtitle))


def strip_trailing_series(title):
    """
        Strip a trailing "<Series>, Book N" / "(<Series> #N)" that a provider
        baked into the book title, so display + sort titles show just the book
        name. Number-marked only, so real subtitles are safe. Never empties the
        title (guards a pathological all-series title).
    """
    if not title:
        return title
    stripped = title
    for pat in SERIES_TITLE_SUFFIX_RE:
        stripped = pat.sub('', stripped)
    stripped = stripped.strip()
    return stripped or title


# A leading article on the SERIES name splits one series across the shelf: the
# same Spellmonger series came back as "Spellmonger" for book 13 and "The
# Spellmonger" for book 17, so sixteen books filed under S and one under T.
# Libraries sort past a leading article anyway, so strip it from the SORT KEY
# only -- the series shown on the book stays exactly as the provider gave it.
SERIES_SORT_ARTICLE_RE = re.compile(r'^\s*(?:the|a|an)\s+', re.IGNORECASE)
SERIES_KEY_STRIP_RE = re.compile(r'[^a-z0-9]+')


def series_key(name):
    """
        Comparison key for two spellings of one series name.

        Folds the leading article and all punctuation/spacing, so "The
        Spellmonger" and "Spellmonger" -- the split this bundle already
        normalises for sort titles -- compare equal here too.
    """
    folded = SERIES_SORT_ARTICLE_RE.sub('', name or '').lower()
    return SERIES_KEY_STRIP_RE.sub('', folded)


# A book folder that leads with a track/series number: "27 - Cube Route",
# "17 Harpy Thyme", "1. The Gunslinger", "03_Title". Capped at 3 digits and a
# real separator required, so a year-shaped folder ("1984") is not mistaken for
# a number and a number glued to a word ("27Cube") never matches.
# The decimal part is captured, not truncated: a novella folder "3.5 - The Road
# To Sevendor" used to yield "Book 3" -- colliding with the real book 3 and
# shelving the novella on top of it. The fraction is optional and must be
# digits, so a year-shaped folder ("1984") is still refused.
FOLDER_NUMBER_RE = re.compile(r'^\s*(\d{1,3}(?:\.\d{1,2})?)\s*[-._\s]\s*\S')


# Generational/honorific tails that survive a comma split but name nobody.
NAME_SUFFIX_KEYS = ('jr', 'sr', 'ii', 'iii', 'iv', 'phd', 'md', 'esq')


def author_name_variants(name):
    """
        Every spelling of a credited-author string that a folder could use: the
        whole string, plus each co-author inside it.

        Providers disagree about how to credit a co-written book. Audible lists
        the authors separately, but Apple returns ONE combined string
        ("TheFirstDefier & JF Brink") -- which can never equal a folder segment,
        so the <Author>/<Series>/<NN - Book> anchor below refused to match and
        the folder fallback bailed out. That bail lands on exactly the records
        that need the fallback most: an Apple record carries no seriesPrimary at
        all, so the folder is the ONLY source of series/volume. Measured live on
        "Defiance of the Fall, Book 10", which ended up with no series in its
        sort title and fell out of its own series on the shelf, while every
        Audible-matched sibling sorted correctly.

        Widening the anchor cannot mis-tag a differently-organised library: the
        names added are all genuinely credited authors, and the numbered-folder
        and series-folder checks still have to pass.
    """
    out = []
    for part in [name] + MULTI_AUTHOR_RE.split(name):
        cleaned = name_key(part)
        # A generational suffix is not an author. "Martin Luther King, Jr."
        # split on the comma and contributed 'jr', which is neither a credited
        # name nor something a folder is called -- contradicting the claim
        # below and offering a one-token key that could anchor by accident.
        if cleaned in NAME_SUFFIX_KEYS:
            continue
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def series_from_path_segments(segments, author_names):
    """
        Derive (series_name, number) for the common audiobook layout
        <Author>/<Series>/<NN> - <Title>/<file> from a file's path segments, or
        (None, None) when the layout doesn't clearly match.

        Deliberately defensive: the GRANDPARENT of the file (the folder above
        the numbered book folder's parent) must equal a credited author, which
        anchors this exact layout. Anything else -- a flat "<Author>/<NN> -
        Title>" tree, a numbered series folder, a book folder with no leading
        number, or a shallow path -- yields (None, None) so libraries that use a
        different convention are never mis-tagged.

        `author_names` is a list of folded credited-author keys (name_key),
        so a folder's spacing and punctuation never decide the anchor. The
        FOLDER is expanded the same way, because the mismatch runs in both
        directions: a provider may credit "TheFirstDefier & JF Brink" against a
        <TheFirstDefier> folder, and a library may equally file <TheFirstDefier
        & JF Brink> against two separately-credited authors. Widening only the
        provider side fixed the first and left the second bailing on exactly
        the records the fallback exists for.
    """
    clean = [s.strip() for s in segments if s and s.strip()]
    # Need <author>/<series>/<book folder>/<file>.
    if len(clean) < 4:
        return (None, None)
    book_folder = clean[-2]
    series_folder = clean[-3]
    author_folder = clean[-4]
    number = FOLDER_NUMBER_RE.match(book_folder)
    if not number:
        return (None, None)
    # Anchor: the folder two levels above the file must be the author -- in
    # ANY of the spellings either side uses for a co-written credit.
    if not author_names:
        return (None, None)
    folder_keys = author_name_variants(author_folder)
    if not [k for k in folder_keys if k in author_names]:
        return (None, None)
    # The series folder must be a real name, not a number or the author again.
    if re.match(r'^\d+$', series_folder) or name_key(series_folder) in author_names:
        return (None, None)
    return (series_folder, number.group(1))


class UpdateTool:
    def __init__(self, content_type, force, lang, media, metadata, prefs):
        self.content_type = content_type
        self.force = force
        self.lang = lang
        self.media = media
        self.metadata = metadata
        self.prefs = prefs
        self.region = self.extract_region_from_id()

    def build_url(self):
        """
            Builds the URL for the API request.
        """
        # Get the current region
        self.region_override = self.get_preferred_region()
        # For an author, pass the known name so the API can still fetch a portrait
        # and bio from Goodreads when the Audible ASIN is dead or region-locked --
        # there is then no page to scrape, and without a name nothing can be looked
        # up. A wrong/empty name is harmless: the API's name gate declines to fill
        # rather than attaching the wrong person.
        # The name is read from MEDIA, not metadata: at update time Plex passes
        # the tagged artist name on `media.title` while `metadata.title` is the
        # field the agent FILLS IN -- it is still blank here, so keying off it
        # silently sent no name at all (measured live: every agent request arrived
        # as /authors/<asin>?region=us). Fall back to metadata.title for any path
        # that has already populated it.
        query = None
        if self.content_type == 'authors':
            name = None
            try:
                name = self.media.title
            except Exception:
                name = None
            if not name and self.metadata:
                name = self.metadata.title
            if name:
                query = 'name=' + quote_param(name)
        # Set the region helper
        region_helper = RegionTool(
            region=self.region_override, content_type=self.content_type,
            id=self.extract_asin_from_id(), query=query)

        if query:
            update_url = region_helper.get_id_url_with_query()
        else:
            update_url = region_helper.get_id_url()
        log.debug('Update URL: ' + update_url)
        return update_url

    def cleanup_html(self):
        """
            Cleans up HTML in either the description or synopsis.
        """
        html_tags = '<[^<]+?>'

        # Clean up HTML in the description
        if self.content_type == 'authors':
            # First handle special cases
            self.description = self.replace_html_special(self.description)
            self.description = re.sub(
                html_tags, '', self.description)
        # Clean up HTML in the synopsis
        if self.content_type == 'books':
            # First handle special cases
            self.synopsis = self.replace_html_special(self.synopsis)
            self.synopsis = re.sub(
                html_tags, '', self.synopsis)

    def replace_html_special(self, input_html):
        """
            Replaces HTML lists with a bullet point.
            Replaces HTML paragraphs with a newline.
            Replaces HTML line breaks with a newline.
        """
        return (
            input_html.replace("<ul>", "")
            .replace("</ul>", "\n")
            .replace("<ol>", "")
            .replace("</ol>", "\n")
            .replace("<li>", " • ")
            .replace("</li>", "\n")
            .replace("<br />", "")
            .replace("<p>", "")
            .replace("</p>", "\n")
            .strip()
        )

    def collect_metadata_to_log(self):
        """
            Collects the metadata to log.
        """
        # Start an array with common metadata
        data_to_log = [{'ASIN': self.metadata.id}]

        # Determine which metadata to log
        if self.content_type == 'books':
            data_to_log.extend(
                [
                    {'Book poster URL': self.thumb},
                    {'Book publisher': self.metadata.studio},
                    {'Book release date': str(
                        self.metadata.originally_available_at)},
                    {'Book sort title': self.metadata.title_sort},
                    {'Book summary': self.metadata.summary},
                    {'Book title': self.metadata.title},
                ]
            )
        elif self.content_type == 'authors':
            data_to_log.extend(
                [
                    {'Author bio': self.metadata.summary},
                    {'Author name': self.metadata.title},
                    {'Author poster URL': self.thumb},
                    {'Author sort name': self.metadata.title_sort},
                    {'Similar Authors': ', '.join([author['name']
                                                   for author in self.similar] if self.similar else [])},
                ]
            )

        return data_to_log

    def collect_metadata_arrs_to_log(self):
        """
            Collects the metadata arrays to log.
        """
        # Start an array with common metadata
        multi_arr = [{'Genres': self.metadata.genres}]

        # Book metadata
        book_multi_arr = [
            {'Moods (Authors)': self.metadata.moods},
            {'Styles (Narrators)': self.metadata.styles},
        ]

        # Determine which metadata to log ('books' — the album helper's actual
        # content_type; the old 'book' comparison was never true, so moods and
        # styles never appeared in any log).
        if self.content_type == 'books':
            multi_arr.extend(book_multi_arr)

        return multi_arr

    def extract_asin_from_id(self):
        """
            Extracts the ASIN from the ID.
        """
        # Get the ASIN from the ID
        asin = self.metadata.id.split('_')[0]
        log.debug('Extracted ASIN from ID: ' + asin)
        return asin

    def extract_region_from_id(self):
        """
            Extracts the region from the ASIN and sets the region.
        """
        # Get the region from the ASIN
        try:
            region = self.metadata.id.split('_')[1]
            log.debug('Extracted region from ASIN: ' + region)
        except IndexError:
            log.info('No region found in ID, using default region.')
            region = self.get_preferred_region()
            # Save the region to the ID
            self.metadata.id = self.metadata.id + '_' + region
        # Set region and ASIN
        return region

    def get_preferred_region(self):
        """
            Get the preferred region from class or preferences.
        """
        try:
            region = self.region
        except AttributeError:
            region = self.prefs['region']
        log.info('Preferred region: ' + region)
        return region

    def log_update_metadata(self):
        """
            Writes metadata information to log.
        """
        # Send a separator to the log
        log.separator(
            msg=(
                'FINALIZED: ' + self.metadata.title +
                ', ID: ' + self.metadata.id
            ),
            log_level="info"
        )

        # Full field dump at DEBUG: it includes the entire book summary, and
        # update fires once per TRACK — at info a multi-part book logged its
        # summary dozens of times, tens of MB per scan, rotating real
        # WARN/ERROR lines out of Plex's fixed-size plugin logs. The FINALIZED
        # separator above stays the one-line INFO outcome.
        data_to_log = self.collect_metadata_to_log()
        log.metadata(data_to_log, log_level="debug")

        # Collect metadata arrays to log
        multi_arr = self.collect_metadata_arrs_to_log()
        log.metadata_arrs(multi_arr, log_level="debug")

        log.separator(log_level="info")


class AlbumUpdateTool(UpdateTool):
    def parse_api_response(self, response):
        """
            Parses keys from API into helper variables if they exist.
        """
        self.set_empty_variables()

        if 'authors' in response:
            self.author = response['authors']
        if 'releaseDate' in response:
            self.date = response['releaseDate']
        if 'genres' in response:
            self.genres = response['genres']
        if 'narrators' in response:
            self.narrator = response['narrators']
        if 'rating' in response:
            self.rating = response['rating']
        if 'seriesPrimary' in response:
            self.series = response['seriesPrimary']['name']
            if 'position' in response['seriesPrimary']:
                self.volume = self.volume_prefix(
                    response['seriesPrimary']['position']
                )
        if 'seriesSecondary' in response:
            self.series2 = response['seriesSecondary']['name']
            if 'position' in response['seriesSecondary']:
                self.volume2 = self.volume_prefix(
                    response['seriesSecondary']['position']
                )
        if 'publisherName' in response:
            self.studio = response['publisherName']
        if 'summary' in response:
            self.synopsis = response['summary']
        if 'image' in response:
            self.thumb = response['image']
        if 'imageSquare' in response and response['imageSquare']:
            # Plex music art is square. Prefer the native square cover (Apple)
            # as the default poster, keeping the original as a secondary option
            # rather than dropping it.
            self.thumb_secondary = self.thumb
            self.thumb = response['imageSquare']
        if 'similar' in response:
            self.similar = response['similar']
        if 'subtitle' in response:
            self.subtitle = response['subtitle']
        if 'title' in response:
            # Strip a series descriptor a provider baked into the title
            # ("Changes: The Dresden Files, Book 12" -> "Changes") so both the
            # display and sort titles show just the book name.
            self.title = strip_trailing_series(response['title'])

    def set_metadata_date(self):
        """
            Sets the date.
        """
        if self.date is not None:
            if not self.metadata.originally_available_at or self.force:
                self.metadata.originally_available_at = self.date

    def set_empty_variables(self):
        """
            Sets empty variables.

            Every field parse_api_response fills conditionally must be reset
            here, because the metadata setters read them unconditionally. Audible
            records always carry a summary/publisher/author, but multi-provider
            (Hardcover/OpenLibrary) records often don't — a missing one used to
            crash the update with AttributeError (e.g. no 'synopsis').
        """
        # List-typed fields default to [] (not None) so the tag helpers can
        # iterate them safely even when a provider record omits them.
        self.author = []
        self.date = None
        self.genres = []
        self.narrator = []
        self.rating = None
        self.series = ''
        self.series2 = ''
        self.similar = []
        self.studio = ''
        self.subtitle = ''
        self.synopsis = ''
        self.thumb = ''
        # The original (usually portrait) cover, kept as a secondary poster when
        # a native square cover is used as the default.
        self.thumb_secondary = ''
        self.title = ''
        self.volume = ''
        self.volume2 = ''

    def set_metadata_rating(self):
        """
            Sets the rating.
        """
        # We always want to refresh the rating. Providers rate on a 0-5 scale,
        # doubled to Plex's 0-10; clamp to [0, 10] so a stray already-0-10 value
        # from a provider can't produce an out-of-range rating. Rating is set
        # LAST in compile_metadata, so a malformed value crashing here used to
        # leave a half-applied update — skip it instead.
        if self.rating:
            try:
                self.metadata.rating = max(0.0, min(float(self.rating) * 2, 10.0))
            except (TypeError, ValueError):
                log.warn('Skipping unparseable rating: %r' % (self.rating,))

    def set_metadata_summary(self):
        """
            Sets the summary.
        """
        # self.synopsis gate: a sparse Hardcover/OpenLibrary record (no summary)
        # must not BLANK an existing summary on a force refresh — "the API
        # answered" is not "the API answered completely".
        if self.synopsis and (not self.metadata.summary or self.force):
            self.cleanup_html()
            self.metadata.summary = self.synopsis

    def set_metadata_studio(self):
        """
            Sets the studio.
        """
        # self.studio gate: same sparse-record wipe protection as the summary.
        if self.studio and (not self.metadata.studio or self.force):
            self.metadata.studio = self.studio

    def album_file_path(self):
        """
            Best-effort absolute path of a file in this album, for folder-layout
            parsing. The SEARCH media carries an (encoded) `filename`; the UPDATE
            media instead exposes tracks -> items -> parts, each part carrying a
            `file` path. Try both. Returns None if nothing is found; never raises.
            (No getattr/hasattr -- the Plex sandbox forbids them.)
        """
        # 1. Search media carries an encoded filename directly.
        try:
            if self.media.filename:
                return self.media.filename
        except Exception:
            pass
        # 2. Update media has no filename; walk (tracks | children) -> items ->
        # parts -> file (confirmed live: the UPDATE media is a MediaTree exposing
        # tracks[].items[].parts[].file).
        for source_name in ('tracks', 'children'):
            try:
                container = (
                    self.media.tracks if source_name == 'tracks'
                    else self.media.children
                )
            except Exception:
                continue
            try:
                node_iter = container.values()
            except Exception:
                node_iter = container
            for node in (node_iter or []):
                # A track node exposes .items -> .parts; some trees put .parts
                # directly on the node. Try items first, then the node itself.
                item_lists = []
                try:
                    if node.items:
                        item_lists.append(node.items)
                except Exception:
                    pass
                item_lists.append([node])
                for items in item_lists:
                    for item in (items or []):
                        try:
                            parts = item.parts
                        except Exception:
                            parts = None
                        for part in (parts or []):
                            try:
                                if part.file:
                                    return part.file
                            except Exception:
                                continue
        log.debug('incipit series-from-path: no file path in update media')
        return None

    def folder_series_wins(self):
        """
            Whether the FOLDER should override a provider that supplied BOTH a
            series name and a position.

            Global pref first, then a per-author allow-list. The list exists
            because folder quality varies WITHIN one library, so this cannot be
            a single switch:

              * Katherine Addison's Chronicles of Osreth came back from one
                provider under TWO names -- the parent for books 1 and 3, a
                "...: The Cemeteries of Amalo Trilogy" sub-series for the two in
                between, whose numbering restarts at 1. The shelf ended up with
                two Book 1s. Her folders have it right: 1, 1.1, 2, 3, 4.
              * Glen Cook's twelve books sort correctly today PRECISELY because
                the provider wins: his folders carry three different series
                names and reuse numbers (two "1 -", two "2 -"). Turning the
                global pref on would fix Addison by fragmenting Cook.

            Matched on the folded name key, so spacing, punctuation and accents
            in the pref do not decide it -- same treatment the folder anchor
            gets, and the same shape as authors_prefer_hardcover.
        """
        if bool(self.prefs['series_from_folder_wins']):
            return True
        listed = (self.prefs['series_from_folder_authors'] or '').strip()
        if not listed:
            return False
        wanted = []
        for entry in listed.split(','):
            key = name_key(entry)
            if key and key not in wanted:
                wanted.append(key)
        if not wanted:
            return False
        for person in (self.author or []):
            person_name = person.get('name') if isinstance(person, dict) else None
            if not person_name:
                continue
            for variant in author_name_variants(person_name):
                if variant in wanted:
                    return True
        return False

    def derive_series_from_path(self):
        """
            Fill a MISSING series (name + book number) from the on-disk folder
            layout <Author>/<Series>/<NN> - <Title>/, and strip a bare trailing
            "(<Series>)" a provider baked into the title (e.g. "Cube Route
            (Xanth)" -> "Cube Route"). The folder layout must clearly match (see
            series_from_path_segments), so a library using a different
            convention is left exactly as it was.

            With `series_from_folder_wins` the folder also OVERRIDES a series the
            provider did supply. That exists because providers disagree with each
            OTHER across a series while a library's own folders do not: Katherine
            Addison's books came back as "Chronicles of Osreth, Book 1" for two
            different books, and one volume as "Cemeteries of Amalo" while its
            own sibling said "Chronicles of Osreth" -- the nested sub-series is
            real, and each record picks whichever it prefers. The folder tree is
            the one source that is consistent within a library, and the operator
            controls it. Default off: a library whose folders are less reliable
            than its providers wants the old behaviour.
        """
        # Both parts are needed, because the sort title only includes a series
        # when it also has a volume: a record carrying "Spellmonger" with no
        # book number sorted as a bare title and fell out of its own series on
        # the shelf. So keep going when either half is missing, and fill only
        # the half the provider left empty -- unless the operator has asked for
        # the folder to win outright, in which case run even with both present.
        folder_wins = self.folder_series_wins()
        if self.series and self.volume and not folder_wins:
            return
        raw = self.album_file_path()
        if not raw:
            return
        # Only unquote the source that is actually URL-encoded. album_file_path
        # returns media.filename (encoded) when it exists and otherwise
        # part.file, which is a REAL filesystem path -- and this runs on the
        # UPDATE media, where filename does not exist, so it was unquoting real
        # paths unconditionally. That silently rewrites any directory holding a
        # literal '%' followed by two same-case hex digits ("50%2F50" ->
        # "50/50", "Mix%ab" -> "Mix\xab"), so the folder segments below are
        # parsed from a path that does not exist and the series/volume come out
        # wrong or empty. Same test album_file_path itself uses to choose.
        encoded = False
        try:
            encoded = bool(self.media.filename)
        except Exception:
            encoded = False
        path = raw
        if encoded:
            try:
                path = urllib.unquote(raw)
            except Exception as e:
                log.error('incipit series-from-path failed: %s', e)
                return
        # part.file is a real (unicode) path; media.filename is url-encoded str.
        # Decode str->unicode where needed; ignore if already unicode/py3 str.
        try:
            path = path.decode('utf8')
        except Exception:
            pass
        author_names = []
        for person in (self.author or []):
            name = person.get('name') if isinstance(person, dict) else None
            if name:
                # Variants, not the bare string: a provider that credits both
                # co-authors in ONE field would otherwise never match the
                # single-author folder (see author_name_variants).
                for variant in author_name_variants(name):
                    if variant not in author_names:
                        author_names.append(variant)
        series_name, number = series_from_path_segments(
            path.split('/'), author_names
        )
        if not series_name:
            log.debug(
                'incipit series-from-path: path did not match layout; path="%s" '
                'authors=%s', path, author_names
            )
            return
        # The provider's own series name, captured BEFORE the assignment below
        # overwrites it. Without this the log line that exists to surface a
        # disagreement could only print the folder's name -- i.e. it could not
        # show what it replaced, which was the one thing it was for.
        provider_series = self.series
        # A provider series NAME with no POSITION can never build a sort title:
        # set_metadata_sort_title needs both halves, so such a book sorts as a
        # bare title and drops out of its own series on the shelf. Keeping that
        # unusable name while refusing the folder's usable name+number pair is
        # the worst of both. So when the provider numbered nothing, take the
        # folder's PAIR -- the name AND the number together.
        #
        # Live case: "Arcanum Unbounded" is credited to "The Mistborn Saga"
        # with no position, while the library files it at <Brandon
        # Sanderson>/<The Cosmere>/18. Neither half of the provider's answer can
        # sort it; the folder's can.
        #
        # Bounded to books that TODAY have no sort series at all: a provider
        # that supplied a position returns at the top of this method, so it
        # never reaches here -- which is what keeps a sub-series like
        # "Elantris, Book 2" out of its parent Cosmere shelf numbering.
        unnumbered_provider_series = bool(provider_series) and not self.volume
        # ALWAYS true by the time we get here: the early return above only lets
        # through cases where the provider lacked a series, lacked a position,
        # or the operator asked for the folder to win. It is spelled out rather
        # than dropped because the three reasons are what the log distinguishes.
        #
        # A previous version carried a `series_agrees` cross-check here, meant
        # to stop the folder's NUMBER being grafted onto a series name the
        # provider supplied. That case cannot arise now -- whenever we take the
        # number we have also taken the folder's NAME, so the pair is coherent
        # by construction -- and leaving the check in place made it dead code
        # that read as live protection, with two log branches that could never
        # print. Restore it only alongside a path that keeps the provider's
        # name, and compare against provider_series, not self.series.
        derived_series = folder_wins or not provider_series or unnumbered_provider_series
        if derived_series:
            self.series = series_name
        if folder_wins or not self.volume:
            self.volume = self.volume_prefix(number)
        # Now that the series name is known, strip a bare "(<Series>)" the
        # provider left in the title so the display + sort titles are clean.
        # Only when the folder supplied the series -- a provider that gave its
        # own series name never baked THIS one into the title.
        if derived_series and self.title:
            pattern = r'\s*\(\s*' + re.escape(series_name) + r'\s*\)\s*$'
            stripped = re.sub(pattern, '', self.title, flags=re.IGNORECASE).strip()
            if stripped:
                self.title = stripped
        # Report what actually happened, naming the series that was REPLACED
        # when there was one.
        if unnumbered_provider_series and not folder_wins:
            if series_key(series_name) == series_key(provider_series):
                log.warn(
                    'incipit album: provider series "%s" had no book number -- '
                    'took the folder number "%s" for "%s"',
                    provider_series, self.volume, self.title
                )
            else:
                log.warn(
                    'incipit album: provider series "%s" had no book number -- '
                    'REPLACED it with the folder pair "%s, %s" for "%s"',
                    provider_series, self.series, self.volume, self.title
                )
        else:
            log.warn(
                'incipit album: series from the folder path -- "%s, %s" for "%s"',
                self.series, self.volume, self.title
            )

    def set_metadata_sort_title(self):
        """
            Sets the sort title.
        """
        # Add series/volume to sort title where possible.
        series_with_volume = ''
        if self.series and self.volume:
            # NOTE: do NOT zero-pad the volume here. Plex orders these numerically
            # on its own -- a 1.3.21 change padded to "Book 0010" on the theory
            # that title_sort is a plain lexical sort, but the library was already
            # shelving correctly and the padding only made the sort title look
            # wrong. Reverted in 1.3.22; don't reintroduce it without first
            # confirming a real mis-ordering in Plex.
            # strip() both halves: this composer is the LAST thing between a
            # dirty source and a SAVED sort title. Goodreads series 131836 is
            # literally titled "Six of Crows " (librarian trailing space);
            # concatenated raw it produced "Six of Crows , Book 2" -- a shelf
            # split that re-matching could not fix, since the same string was
            # re-derived every time. The API trims its side too (af9fb5c);
            # this guards every other source (providers, folder names).
            series_with_volume = (
                SERIES_SORT_ARTICLE_RE.sub('', self.series).strip()
                + ', ' + self.volume.strip()
            )
        # Only include subtitle in sort if not in a series
        if not self.volume:
            self.title = self.metadata.title
        # Fall back to the existing display title when the record carried none,
        # so a sparse record with series/volume but no title doesn't rebuild the
        # sort title as bare "Series, Book N" with the book name dropped.
        if not self.title:
            self.title = self.metadata.title
        new_sort = ' - '.join(filter(None, [series_with_volume, self.title]))
        # Never blank an existing sort title with an empty rebuild.
        if new_sort and (not self.metadata.title_sort or self.force):
            self.metadata.title_sort = new_sort

    def set_metadata_tags(self):
        """
            Set tags of artist
        """
        # Create tagger.
        tagger = TagTool(self, self.prefs)

        # NOTE: no wholesale clear here. clear-on-force wiped moods/styles a
        # sparse Hardcover/OpenLibrary record couldn't repopulate (moods is a
        # FLAT field mixing author + "Series:" entries, so clearing it for one
        # category destroyed the other). Each add_* below now owns its own
        # clear, gated on having replacement data (the add_genres pattern), so a
        # missing category can never wipe a present one.

        # Genres.
        tagger.add_genres()
        # Narrators.
        tagger.add_narrators_to_styles()
        # Moods (authors + series). Decide ONCE, before any add, whether to
        # populate: only on force or when the field is empty. A multi-part book
        # is update()'d once per track; without this gate the agent re-added
        # moods on every part, and Plex logged each as "something changed" ->
        # an expensive per-track tags write, piling up SQLite transaction
        # contention (a contributor to the refresh lockup on 200+ part books).
        # Never CLEARS, so a sparse record still can't wipe existing moods and
        # force still refreshes them additively.
        populate_moods = self.force or not self.metadata.moods
        if populate_moods:
            if self.prefs['store_author_as_mood']:
                tagger.add_authors_to_moods()
            tagger.add_series_to_moods()
        # Similar.
        tagger.add_similar()

    def set_metadata_title(self):
        """
            Sets the title.
        """
        # If the `simplify_title` option is selected, don't append subtitle
        # and remove extra endings on the title
        if self.prefs['simplify_title']:
            album_title = self.simplify_title()
        elif (
            self.subtitle
            and not SERIES_SUBTITLE_RE.search(self.subtitle)
            and not is_marketing_subtitle(self.subtitle)
        ):
            # Append a genuine subtitle, but not a series descriptor like
            # "Discworld Novel 26" / "A Discworld Novel" / "(Xanth #3)", and not
            # marketing blurb a provider stashed in the subtitle field ("from the
            # Bestselling Series That Inspired BBC's the Watch").
            album_title = self.title + ': ' + self.subtitle
        else:
            album_title = self.title
        album_title = self.strip_trailing_by_contributor(album_title)
        # On a first match Plex seeds metadata.title with the raw ALBUM tag, so
        # an "only set when empty" guard leaves rip junk ("15 Triss", "Origin
        # (Unabridged)") as the displayed title forever while sort title,
        # summary, etc. (unseeded, so empty) all update. Overwrite when the
        # current title is empty, still that scanner seed (== the tagged album),
        # or on force — but leave a title the user changed to something else
        # alone (Plex's own edit-lock protects those server-side too).
        current_title = self.metadata.title
        tagged_title = self.media.title if self.media else None
        # album_title gate: a record with no title must never blank the field.
        if album_title and (
            not current_title
            or self.force
            or (tagged_title and current_title == tagged_title)
        ):
            self.metadata.title = album_title

    def strip_trailing_by_contributor(self, title):
        """
            Remove a trailing " by <Name>" from a title, but ONLY when <Name>
            matches a credited author or narrator of this book. Audible sometimes
            appends the ghostwriter ("Code Red: A Mitch Rapp Novel by Kyle Mills").
            Matching against real contributors keeps legit titles like "Death by
            Black Hole" or "Murder by the Book" intact.
        """
        matched = re.search(r'\s+by\s+(.+?)\s*$', title, flags=re.IGNORECASE)
        if not matched:
            return title
        trailing = matched.group(1).strip().lower()
        names = []
        for person in (self.author or []):
            name = person.get('name') if isinstance(person, dict) else None
            if name:
                names.append(name.strip().lower())
        for person in (self.narrator or []):
            name = person.get('name') if isinstance(person, dict) else None
            if name:
                names.append(name.strip().lower())
        if trailing in names:
            return title[:matched.start()].strip()
        return title

    def simplify_title(self):
        """
            Remove extra description text from the title
        """
        # If the title ends with a series part, remove it
        # works for "Book 1" and "Book One"
        album_title = re.sub(
            r", book [\w\s-]+\s*$", "", self.title, flags=re.IGNORECASE)
        # If the title ends with "unabridged"/"abridged", with or without parenthesis
        # remove them; case insensitive
        album_title = re.sub(r" *\(?(un)?abridged\)?$", "",
                             album_title, flags=re.IGNORECASE)
        # Trim any leading/trailing spaces just in case
        album_title = album_title.strip()

        return album_title

    def volume_prefix(self, string):
        """
            Prefixes volume number with 'Book' if it doesn't exist.
        """
        # incipit-api (Hardcover/OpenLibrary) can return a numeric series
        # position; coerce to str so the regex/concatenation don't raise. A
        # missing/None position yields no volume rather than "Book None".
        if string is None:
            return ''
        string = str(string)
        if not string.strip():
            return ''
        book_regex = '(Book ?(\d*\.)?\d+[+-]?[\d]?)'
        if not re.match(book_regex, string):
            prefixed_string = ('Book ' + string)
            return prefixed_string
        return string


class ArtistUpdateTool(UpdateTool):
    def measure_image(self, image_url):
        """
            Read a JPEG's (height, width) from the first bytes, or None on any
            failure (non-JPEG, unreachable, truncated).
        """
        try:
            # HTTP.Request (framework fetcher) for a bounded timeout — Python 2's
            # urllib.urlopen has none, so one stalled Amazon connection used to
            # hang an update worker forever. cacheTime=0: do NOT cache the image
            # bytes — a transient CDN error page returned with status 200 would
            # otherwise be cached for the default week and keep measurement
            # failing (shipping an uncropped portrait) until it expired.
            image_file_dl_contents = str(
                HTTP.Request(image_url, timeout=45, cacheTime=0))

            # Only trust an actual JPEG: a text/HTML error body would otherwise be
            # walked by the SOFn scanner below and mis-measured.
            if image_file_dl_contents[:2] != '\xff\xd8':
                return None

            image_file = StringIO.StringIO(image_file_dl_contents)

            head = image_file.read(24)
            if len(head) != 24:
                image_file.close()
                return None

            image_file.seek(0)  # Read 0xff next
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf:
                image_file.seek(size, 1)
                byte = image_file.read(1)
                while ord(byte) == 0xff:
                    byte = image_file.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', image_file.read(2))[0] - 2
            # We are at a SOFn block
            image_file.seek(1, 1)  # Skip `precision' byte.
            height, width = struct.unpack('>HH', image_file.read(4))
            image_file.close()
            return (height, width)
        except Exception as err:
            log.error('Could not measure image %s: %s' % (image_url, err))
            return None

    def get_square_image(self, image_url):
        """
            Crop an Audible (Amazon) author photo to a square via Amazon's
            crop-in-URL syntax: portrait centered at the top, landscape at the
            horizontal center. Non-Amazon URLs (e.g. Hardcover) don't support
            that syntax, so they are returned unchanged.
        """
        if 'amazon' not in image_url:
            return image_url

        dimensions = self.measure_image(image_url)
        if not dimensions:
            return image_url
        height, width = dimensions

        if (height > width):
            # Return a portrait image centered at the top
            w_str = str(width)
            return image_url.replace(
                '.jpg',
                '.__01_SX'+w_str+'_CR0,0,'+w_str+','+w_str+'__.jpg'
            )

        if (width > height):
            # Return a landscape image centered at the horizontal middle
            h_str = str(height)
            padding = str((width - height) / 2)
            return image_url.replace(
                '.jpg',
                '.__01_SY'+h_str+'_CR'+padding+',0,'+h_str+','+h_str+'__.jpg'
            )

        return image_url

    def parse_api_response(self, response):
        """
            Parses keys from API into helper variables if they exist.
        """
        self.set_empty_variables()

        if 'description' in response:
            self.description = response['description']
        if 'genres' in response:
            self.genres = response['genres']
        if 'name' in response:
            self.name = response['name']
        if 'image' in response:
            # The API already picks the best author image (preferring Hardcover's
            # curated portrait over Audible's — which is often the book cover, not
            # a photo), so use it as the default poster.
            squared_image = self.get_square_image(response['image'])
            log.debug('Author image: ' + squared_image)
            self.thumb = squared_image
        if 'imageAlt' in response and response['imageAlt']:
            # Offer the alternate (Audible) image as a secondary poster option.
            self.thumb_secondary = response['imageAlt']
        if 'similar' in response:
            self.similar = response['similar']

    def set_metadata_description(self):
        """
            Set description of artist
        """
        # self.description gate: an author record with no bio (common) must not
        # blank an existing bio on a force refresh.
        if self.description and (not self.metadata.summary or self.force):
            self.cleanup_html()
            self.metadata.summary = self.description

    def set_empty_variables(self):
        """
            Sets empty variables. name/description are read unconditionally by
            the metadata setters, and description is often absent (author with no
            bio), so both must be initialised or the update raises AttributeError.
        """
        self.date = None
        self.description = ''
        self.genres = []
        self.name = ''
        self.similar = []
        self.thumb = ''
        # The alternate (Audible) author image, offered as a secondary poster.
        self.thumb_secondary = ''

    def set_metadata_sort_title(self):
        """
            Set sort title of artist
        """
        if not self.metadata.title_sort or self.force:
            # re.UNICODE: without it Py2's \w is ASCII-only, so a single-word
            # name carrying any accented letter ("Voltaire" is fine, "Colette"
            # is fine, but "Molière"/"Borges"-style non-ASCII forms are not)
            # fails the single-word guard and gets fed to the surname split,
            # which then mangles a mononym into "e, Molièr".
            single_word_name = re.match(r'\A[\w-]+\Z', self.name, re.UNICODE)
            if self.prefs['sort_author_by_last_name'] and not single_word_name:
                # The separators before the surname and the suffix are literal
                # SPACES: an unescaped "." there matched any character, so a
                # name with no space still "split" on an arbitrary letter.
                split_author_surname = re.match(
                    r'^(.+?)\s([^\s,]+)(,?\s(?:[JS]r\.?|III?|IV))?$',
                    self.name,
                    re.UNICODE,
                )
                if split_author_surname:
                    self.metadata.title_sort = ', '.join(
                        filter(
                            None,
                            [
                                (split_author_surname.group(2) + ', ' +
                                    split_author_surname.group(1)),
                                split_author_surname.group(3)
                            ]
                        )
                    )
                else:
                    # The name didn't fit "First Last [suffix]" (e.g. empty or
                    # an unusual form). Fall back to the plain name/title rather
                    # than crash on .group() of a None match.
                    self.metadata.title_sort = self.name or self.metadata.title
            else:
                self.metadata.title_sort = self.metadata.title

    def set_metadata_tags(self):
        """
            Set tags of artist
        """
        # Create tagger.
        tagger = TagTool(self, self.prefs)
        # Genres.
        tagger.add_genres()
        # Similar.
        tagger.add_similar()

    def set_metadata_title(self):
        """
            Set title of artist
        """
        # self.name gate: a record with no name must never blank the artist.
        if self.name and (not self.metadata.title or self.force):
            self.metadata.title = self.name


class TagTool:
    def __init__(self, helper, Prefs):
        self.helper = helper
        self.prefs = Prefs

    def add_genres(self):
        """
            Add genre(s) to Plex where available and depending on preference.
        """
        if not self.prefs['keep_existing_genres'] and self.helper.genres:
            if not self.helper.metadata.genres or self.helper.force:
                self.helper.metadata.genres.clear()
                for genre in self.helper.genres:
                    if genre['name']:
                        self.helper.metadata.genres.add(genre['name'])

    def add_narrators_to_styles(self):
        """
            Adds narrators to styles.
        """
        # Gate the clear on HAVING narrators (the add_genres pattern): a force
        # refresh against a narrator-less record must not wipe existing styles
        # with nothing to put back.
        if not self.helper.narrator:
            return
        if not self.helper.metadata.styles or self.helper.force:
            self.helper.metadata.styles.clear()
            for narrator in self.helper.narrator:
                self.helper.metadata.styles.add(narrator['name'].strip())

    def add_authors_to_moods(self):
        """
            Adds authors to moods, except for cases in contibutors list.

            Additive (dedup via the moods set): moods is a flat field shared
            with "Series:" entries, so it is never cleared here — a sparse
            record can't wipe author moods, and a re-match's stale author mood
            lingering is the same accepted trade-off as the summary/studio guards.
        """
        contributor_regex = '.+?(?= -)'
        for author in (self.helper.author or []):
            if not re.match(contributor_regex, author['name']):
                self.helper.metadata.moods.add(author['name'].strip())

    def add_series_to_moods(self):
        """
            Adds book series' to moods, since collections are not supported
        """
        if self.helper.series:
            self.helper.metadata.moods.add("Series: " + self.helper.series)
        if self.helper.series2:
            self.helper.metadata.moods.add("Series: " + self.helper.series2)

    def add_similar(self):
        """
            Adds similar items.
        """
        if self.helper.similar:
            for item in self.helper.similar:
                self.helper.metadata.similar.add(item['name'])

