from datetime import date
import json
import re
# Import internal tools
from logging import Logging
from region_tools import RegionTool
import urllib

# Setup logger
log = Logging()

asin_regex = re.compile(r'(?=.\d)[A-Z\d]{10}')
region_regex = re.compile(r'(?<=\[)[A-Za-z]{2}(?=\])')

# Plex library section root paths, fetched once. Used to derive the author from
# a file path (<root>/<Author>/...) when the ALBUMARTIST tag is missing or bad.
# NB: the Plex RestrictedPython sandbox forbids names starting with "_".
LIBRARY_ROOTS_CACHE = None

# Co-author separators for reducing a multi-author artist to its authors:
# comma, ampersand, semicolon, slash, or the word "and". Whitespace around
# "and"/"&" keeps it from splitting inside a name (e.g. "Rand", "Anderson").
MULTI_AUTHOR_RE = re.compile(
    r'\s*,\s*|\s+&\s+|\s+and\s+|\s*;\s*|\s*/\s*', re.IGNORECASE
)

# Trailing part-index tokens a ripper appends to each file, in the many
# conventions seen in the wild: "Title (264)", "Title [7]", "Title - 12",
# "Title 01", "Title - pt03", "Title Ch01", "Disk 5 - Track 01". Stripped from
# the resolved TRACK title so every part of a multi-part book collapses to ONE
# search URL (a cache hit) instead of one network round-trip per part — the
# dominant cost of a cold scan over a multi-part-heavy collection. Only a
# trailing number that is bracketed, dash/underscore-separated, marker-prefixed
# (pt/ch/track/disc/cd), or a leading-zero index is removed, so a real numeric
# title tail ("Xanth 24", "Fahrenheit 451", "1984") is left intact.
PART_INDEX_RES = [
    re.compile(r'\s*[\(\[]\s*\d{1,4}\s*[\)\]]\s*$'),
    re.compile(r'\s*[-_]\s*(?:pt|part|ch|chapter|track|trk|disc|disk|cd)\s*\.?\s*\d{1,4}\s*$', re.IGNORECASE),
    re.compile(r'\s+(?:pt|part|ch|chapter|track|trk|disc|disk|cd)\s*\.?\s*\d{1,4}\s*$', re.IGNORECASE),
    re.compile(r'\s*[-_]\s*\d{1,4}\s*$'),
    re.compile(r'\s+0\d{1,2}\s*$'),
]


def strip_part_index(title):
    """Remove a trailing per-file part index (see PART_INDEX_RES)."""
    for pat in PART_INDEX_RES:
        title = pat.sub('', title)
    return title.strip()


# A "YYYY - " release-year prefix and trailing "[Series N]"/"(2013)"/"[ ]"
# groups on a book-folder name, stripped to recover a clean TITLE from the
# folder when the album tag is missing. Mid-string parens/brackets are kept
# (only a run of trailing groups is removed).
FOLDER_TITLE_YEAR_PREFIX_RE = re.compile(r'^\s*(?:19|20)\d{2}\s*[-_.]\s*')
FOLDER_TITLE_TRAILING_RE = re.compile(r'(?:\s*[\[\(][^\]\)]*[\]\)]\s*)+$')


def is_missing_album(album):
    """True when the album tag is absent or Plex's "[Unknown Album]" placeholder."""
    if not album:
        return True
    return album.strip().strip('[]').strip().lower() == 'unknown album'


def folder_title_from_path(path):
    """
        Recover a book title from the *book folder* -- the immediate parent
        directory of an audio file -- for use when the album tag is missing.
        The audiobook convention folders each book as
        "<...>/YYYY - Title [Series N]/<part files>", so the parent segment,
        with its release-year prefix and trailing [series]/(year) groups
        stripped, is the title. Returns None when a title can't be recovered
        (caller then keeps whatever tag it has).
    """
    try:
        segments = [seg for seg in path.split('/') if seg.strip()]
        # Need at least <book folder>/<file>: the last segment is the file, its
        # parent is the book folder.
        if len(segments) < 2:
            return None
        folder = segments[-2]
        folder = FOLDER_TITLE_YEAR_PREFIX_RE.sub('', folder)
        folder = FOLDER_TITLE_TRAILING_RE.sub('', folder)
        folder = folder.strip(' -_.\t')
        if len(folder) >= 2:
            return folder
    except Exception:
        pass
    return None


def get_library_roots():
    """
        The server's library section root paths, fetched once and cached.
        Returns [] on any failure (callers degrade to the tag-derived author).
    """
    global LIBRARY_ROOTS_CACHE
    if LIBRARY_ROOTS_CACHE is not None:
        return LIBRARY_ROOTS_CACHE
    # Short-circuited: the sandbox blocks the server's HTTP interface at search
    # AND update time ("not permitted", proven live), so this fetch can never
    # succeed — it only cost a one-time 20s stall per plugin process before
    # caching [] anyway. Callers already degrade to the tag-derived author.
    # (A root-free reimplementation of author_from_path — confirming candidates
    # against path segments like parent_author_in_path does — is the way to
    # revive the feature if ever wanted.)
    LIBRARY_ROOTS_CACHE = []
    return LIBRARY_ROOTS_CACHE


class SearchTool:
    def __init__(self, content_type, lang, manual, media, prefs, results):
        self.content_type = content_type
        self.lang = lang
        self.manual = manual
        self.media = media
        self.prefs = prefs
        self.results = results
        # The full (pre-collapse) multi-author artist string, preserved by
        # get_primary_author so author_candidates() can try each author in turn.
        self.multi_author_source = None
        # Memoized book title for the search (see resolve_search_title). None
        # until first resolved; a recovered-from-folder title logs once.
        self.resolved_title = None

    def build_url(self, query):
        """
            Generates the URL string with search paramaters for API call.
        """
        # Pre-process title. If ASIN is found, return the URL
        pre_process = self.pre_process_title()
        if pre_process:
            return pre_process

        # Setup region helper to get search URL
        region_helper = RegionTool(
            content_type=self.content_type, query=query, region=self.region_override)

        # Book search: use the multi-provider incipit-api ONLY when the operator
        # has configured api_base_url; otherwise keep the standard Audible catalog
        # search so a default install behaves like stock. Author search always
        # goes to the audnexus-style API (public by default).
        if self.content_type == 'books':
            if self.prefs['api_base_url']:
                search_url = region_helper.get_search_url()
            else:
                search_url = region_helper.get_api_search_url()
        else:
            search_url = region_helper.get_search_url()
        self.log_search_url(search_url)
        return search_url

    def check_for_asin(self):
        """
            Checks filename (for books) and/or search query for ASIN to quick match.
        """
        # Check filename for ASIN if content type is books
        if self.media.filename and self.content_type == 'books':
            # Pre-assign: if the decode below raises (non-UTF-8 filename), the
            # except logs and execution continues to the `if` — an unassigned
            # local there was a NameError that killed the whole search.
            filename_unquoted = None
            filename_search_asin = None
            try:
                # Provide a plain filename for ASIN search
                filename_unquoted = urllib.unquote(
                    self.media.filename).decode('utf8')
                filename_search_asin = self.search_asin(filename_unquoted)
            except Exception as e:
                log.error('Error checking filename for ASIN: %s', e)

            if filename_search_asin:
                log.info('ASIN found in filename')
                self.check_for_region(filename_unquoted)
                return filename_search_asin.group(0) + '_' + self.region_override

        # Check search query for ASIN
        # Default to album and use artist if no album
        manual_asin = self.media.album if self.media.album else self.media.artist
        manual_search_asin = self.search_asin(manual_asin)

        if manual_search_asin:
            log.info('ASIN found in manual search')
            self.check_for_region(manual_asin)
            return manual_search_asin.group(0) + '_' + self.region_override

    # Check for region override
    def check_for_region(self, search_title):
        """
            Overrides the search with a region.
        """
        match_region = self.search_region(search_title)
        if match_region:
            log.info('Region found in title')
            self.region_override = match_region.group(0)
        else:
            self.region_override = self.prefs['region']
        log.info('Region Override: %s', self.region_override)

    def clear_contributor_text(self, string):
        contributor_regex = '.+?(?= -)'
        if re.match(contributor_regex, string):
            return re.match(contributor_regex, string).group(0)
        return string

    def clear_series_text(self, string):
        """
            Strips a trailing series qualifier in parentheses from an author
            name, e.g. "Davis Ashura (Instrument of Omens)" -> "Davis Ashura".

            Some rips tag ALBUMARTIST with the series appended, so Plex creates a
            separate, unmatched artist per series even though the real author
            already exists. Removing the qualifier lets that phantom artist match
            the real author instead of needing a manual artist match first.

            Deliberately conservative: only a single trailing "(...)" is removed,
            and only when a non-trivial name remains, so ordinary author names
            (and names with no trailing parenthesis) are never altered.
        """
        if not string:
            return string
        stripped = re.sub(r'\s*\([^()]{1,60}\)\s*$', '', string).strip()
        # Guard: if stripping leaves too little to be a name, keep the original.
        if len(stripped) < 2:
            return string
        return stripped

    def log_search_url(self, search_url):
        """
            Logs the search URL.
        """
        log.debug('Search URL: %s', search_url)

    def override_with_asin(self, match_asin, region=None):
        """
            Overrides the search with an ASIN.
        """
        log.debug('Overriding' + ' ' + self.content_type +
                  ' ' + 'search with ASIN')
        asin = match_asin.group(0)
        # Param uses keyword for book and nothing for author
        type_param = '&keywords=' if self.content_type == 'books' else ''
        # Wrap the param for url use
        url_param = type_param + urllib.quote(asin)

        # Setup region helper to get search URL
        self.region_override = region if region else self.prefs['region']
        region_helper = RegionTool(
            content_type=self.content_type, query=url_param, region=self.region_override)

        # Books use api search authors use audnexus search
        if self.content_type == 'books':
            search_url = region_helper.get_api_search_url()
        else:
            # Set ID to ASIN
            region_helper.id = asin
            search_url = region_helper.get_id_url()

        self.log_search_url(search_url)
        return search_url

    def is_typed_search(self):
        """
            True only for a query the user actually TYPED into Fix Match's
            Search Options. Both Fix Match flows arrive with manual=True;
            measured live (the 1.3.60 probe): the instant candidate list on
            dialog OPEN carries the item's own metadata (artist set,
            media.name=None), while a typed query puts the typed text in
            media.name (and clears artist). The typed text is the WHOLE query
            -- no sidecar, no injected author, no duration/ASIN/trackTitle.
            The auto-fired list is a re-run of the automatic match and keeps
            full context, so it scores like a scan.
        """
        try:
            return bool(self.manual and self.media.name)
        except Exception:
            # If media.name is ever unreadable, treat any manual search as
            # typed -- honoring user input is the safer failure mode.
            return bool(self.manual)

    def sidecar(self):
        """
            The Audiobookshelf-style metadata.json next to the book, as a dict, or
            None. This is machine-written, authoritative metadata (asin, title,
            authors, language) -- far better than the often-scrambled file tags.
            Read via Core.storage.load (the Elevated-policy reader the cover code
            uses). Gated on prefer_sidecar_metadata; every failure is caught, so a
            missing sidecar or a sealed sandbox just yields None and matching falls
            back to the tags. Memoized per search (None is a valid cached result).
        """
        try:
            return self.sidecar_cache
        except AttributeError:
            pass
        result = None
        try:
            if self.prefs['prefer_sidecar_metadata'] and self.media.filename:
                path = urllib.unquote(self.media.filename).decode('utf8')
                if '/' in path:
                    raw = Core.storage.load(path.rsplit('/', 1)[0] + '/metadata.json')
                    if raw:
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            result = data
        except Exception as e:
            log.error('incipit sidecar: read/parse failed (%s)', e)
        self.sidecar_cache = result
        return result

    def pre_process_title(self):
        """
            Pre-processes the title to remove any contributor text.
        """
        log.debug('Pre-processing title')
        # Setup some basic things
        search_title = self.media.album if self.content_type == 'books' else self.media.artist
        asin_search_title = self.media.artist

        # Region override
        self.check_for_region(search_title)

        # Normalize name
        if self.content_type == 'books':
            asin_search_title = self.normalizedName

        # ASIN override (an ASIN literally in the title/artist text). NOTE: the
        # sidecar ASIN is deliberately NOT routed here -- a hard override does an
        # Audible-catalog keyword search that fails on new ASINs and short-circuits
        # with no fallback. The sidecar ASIN is sent as an &asin= HINT on the normal
        # title/author search instead (see incipit_extra_args), which the API pins.
        match_asin = self.search_asin(asin_search_title)
        if match_asin:
            log.debug('ASIN found in title')
            return self.override_with_asin(match_asin, self.region_override)

    def search_asin(self, input):
        """
            Searches for ASIN in a string.
        """
        if input:
            return re.search(asin_regex, input)

    def search_region(self, input):
        """
            Searches for region in a string.
        """
        if input:
            return re.search(region_regex, input)

    def validate_author_name(self):
        """
            Checks a list of known bad author names.
            If matched, author name is set to None to prevent
            it being used in search query.
        """
        if self.content_type == 'authors':
            self.get_primary_author()

        strings_to_check = [
            "[Unknown Artist]"
        ]
        for test_name in strings_to_check:
            if self.media.artist == test_name:
                self.media.artist = None
                log.info(
                    "Artist name seems to be bad, "
                    "not using it in search."
                )
                break


class AlbumSearchTool(SearchTool):
    def resolve_search_title(self):
        """
            The book title to search on. Normally the album tag, but when that
            tag is absent or Plex's "[Unknown Album]" placeholder, recover the
            title from the book folder (see folder_title_from_path) so a book
            with no album tag still matches instead of searching for the literal
            "[Unknown Album]". Gated on the title_from_folder_when_missing pref.
            Memoized so the recovery logs once. Falls back to the track-title
            tag, then whatever album value we have.
        """
        if self.resolved_title is not None:
            return self.resolved_title

        # Sidecar metadata.json title is authoritative over the (often scrambled)
        # album tag. Kept WITH any "[Series N]" suffix -- the API normalizer strips
        # it, and the structure helps the API rank the audio edition correctly.
        # NOT on a TYPED search: a Search Options query is the USER typing a
        # correction, and the sidecar overriding it made Fix Match silently
        # ignore whatever was typed whenever a metadata.json existed. The
        # dialog's auto-fired list (is_typed_search False) keeps sidecar-first,
        # like the automatic scan it re-runs.
        # isinstance guard: a malformed sidecar with a non-string title (a list,
        # a number) must fall through to the tags, not crash the search.
        if not self.is_typed_search():
            sc = self.sidecar()
            if sc:
                sc_title = sc.get('title')
                if isinstance(sc_title, (str, unicode)) and sc_title.strip():
                    self.resolved_title = sc_title
                    log.info('incipit title: using metadata.json title "%s"', sc_title)
                    return self.resolved_title

        album = self.media.album
        if not is_missing_album(album):
            self.resolved_title = album
            return album

        # Album tag missing/"[Unknown Album]": try to recover a title from the
        # book folder, else fall back to the track-title tag (never the literal
        # placeholder -- searching "[Unknown Album]" matches nothing).
        recovered = None
        if self.prefs['title_from_folder_when_missing'] and self.media.filename:
            try:
                path = urllib.unquote(self.media.filename).decode('utf8')
                recovered = folder_title_from_path(path)
            except Exception as e:
                log.error('incipit resolve_search_title failed: %s', e)
        if recovered:
            # warn-level so this recovery is visible at the default log level:
            # it means the album tag is missing and was worked around -- a rare,
            # actionable tagging problem worth surfacing.
            log.warn(
                'incipit title: album tag is missing/"%s"; recovered title '
                '"%s" from the book folder', album, recovered
            )
            title = recovered
        else:
            title = self.media.title or album or ''

        self.resolved_title = title
        return title

    def build_search_args(self):
        """
            Builds the search arguments for the API call.
        """
        # Probe (debug): what Plex sent for this search. Measured 1.3.60: the
        # Fix Match dialog's instant list arrives manual=True with the item's
        # metadata (artist set, name=None); a typed Search Options query puts
        # the TYPED TEXT in media.name and clears artist. is_typed_search()
        # builds on exactly this -- re-measure here if a Plex update ever
        # changes the shape.
        try:
            log.debug(
                'incipit search context: manual=%r artist=%r album=%r '
                'title=%r name=%r',
                self.manual, self.media.artist, self.media.album,
                self.media.title, self.media.name
            )
        except Exception as e:
            log.error('incipit search context probe failed: %s', e)
        # First, normalize the name
        self.normalize_name()

        if self.prefs['api_base_url']:
            # incipit-api: always search by title. The API scores candidates on
            # title (+ author when we have it) and filters on relevance, so a
            # bare title is a valid query — unlike Audible's catalog search we
            # must never drop to a `keywords`-only param with no title.
            #
            # Send the title with its STRUCTURE intact (parens/colon/#/brackets)
            # and let the API's validated normalizer strip the series suffix.
            # normalize_name() deletes those markers but keeps the words
            # ("Steel World (Undying Mercenaries #1)" -> "Steel World Undying
            # Mercenaries 1"), which defeats the API's suffix stripping and
            # mis-ranks a book-level record with the same messy title ABOVE the
            # real audio edition. StripDiacritics keeps punctuation quote-safe.
            raw_title = String.StripDiacritics(self.resolve_search_title() or self.normalizedName)
            # Sidecar values arrive as unicode (json); Py2 urllib.quote raises on a
            # unicode with codepoints > 127, so encode to utf8 bytes first (a byte
            # str -- the tag path -- is left as-is). A native-script metadata.json
            # title/author would otherwise crash the whole search.
            if not isinstance(raw_title, str):
                raw_title = raw_title.encode('utf8')
            query = 'title=' + urllib.quote(raw_title)
            author = self.resolve_author()
            if author:
                if not isinstance(author, str):
                    author = author.encode('utf8')
                query += '&author=' + urllib.quote(author)
            # Extra signals the API can use: duration (the veto), a filename
            # ASIN, and the first track title (a fallback when the ALBUM tag is
            # a bare series+number).
            query += self.incipit_extra_args()
            return query

        # Audible catalog path.
        album_param = 'title=' + urllib.quote(self.normalizedName)
        # Fix match/manual search doesn't provide author
        if self.media.artist:
            artist_param = '&author=' + urllib.quote(self.media.artist)
        else:
            # Use keyword search to supplement missing author
            album_param = 'keywords=' + urllib.quote(self.normalizedName)
            artist_param = ''
        return album_param + artist_param

    def resolve_author(self):
        """
            The album author for the incipit-api query. Album searches almost
            never carry media.artist, but the parent artist (already matched)
            IS the author, and the framework exposes it on parent_metadata.
            Passing it lets the API score + disambiguate on author instead of
            returning every title collision (e.g. the many unrelated books
            named "Luck of the Draw").
        """
        # TYPED Fix Match search: send NO author at all. The Search Options
        # dialog has no author field, so any author here would be INJECTED
        # context (the parent artist / the folder) -- and an injected author
        # caps a cross-author rescue below the acceptance floor, which is the
        # exact search Fix Match exists for (observed live: a typed "Project
        # Hail Mary" under the Brian Jacques artist could not surface Andy
        # Weir's book). Authorless title scoring has its own ceiling and is
        # the correct typed behavior. The dialog's auto-fired list keeps the
        # full author context below, so it scores like the scan it re-runs.
        if self.is_typed_search():
            return None

        # Sidecar metadata.json author(s) are authoritative over a scrambled or
        # narrator-as-artist ALBUMARTIST tag. Joined so the API's multi-author
        # split can match any of them. (Typed searches never reach here.)
        sc = self.sidecar()
        if sc:
            # Tolerate the format variants seen in the wild instead of
            # assuming a list of plain strings: Audiobookshelf/OPF exports
            # can store authors as [{"name": ...}] (a dict per author) or as
            # one bare string -- the bare string would otherwise be iterated
            # CHARACTER BY CHARACTER into "J, o, h, n" garbage, and a dict
            # entry would crash the join.
            sc_authors = sc.get('authors')
            if isinstance(sc_authors, (str, unicode)):
                sc_authors = [sc_authors]
            names = []
            for a in (sc_authors or []):
                if isinstance(a, dict):
                    a = a.get('name')
                if a and isinstance(a, (str, unicode)):
                    names.append(a)
            if names:
                joined = ', '.join(names)
                log.info('incipit author: using metadata.json author(s) "%s"', joined)
                return self.clean_search_author(joined)

        author = self.media.artist

        # When the ALBUMARTIST tag is a NARRATOR (differs from the matched parent
        # artist) but the real author is confirmed on disk (a folder in the file
        # path), prefer the parent author so a narrator-tagged book auto-matches
        # instead of scoring low on the narrator name. Mirrors the artist-recovery
        # path and shares its pref; only fires when the parent author actually
        # appears in the file path, so it can NEVER override a correctly tagged
        # album. get_library_roots is blocked at search time, so confirm against
        # media.filename segments directly rather than deriving from the root.
        if self.prefs['match_artist_from_folder']:
            parent_author = self.parent_author_in_path(author)
            if parent_author:
                # warn-level so this correction is visible at the default log
                # level (WARN): it means the ALBUMARTIST tag is wrong and was
                # worked around -- a rare, actionable event, unlike routine info.
                log.warn(
                    'incipit album: tag author "%s" is not the on-disk author; '
                    'using matched parent author "%s"', author, parent_author
                )
                author = parent_author

        if not author:
            try:
                parent = self.media.parent_metadata
                if parent and parent.title:
                    author = parent.title
                    log.debug('incipit author from parent: %s' % author)
            except Exception as e:
                log.error('incipit author resolve failed: %s', e)

        # Fall back to the library folder when the tag-derived author is missing
        # or clearly not a name (a bare number/year from a mis-scan, e.g. an
        # album foldered "2025 - Title" scanned as artist "2025"). Under the
        # <library-root>/<Author>/... convention the author is the first path
        # segment after the root, regardless of any series subfolder depth.
        if not author or re.match(r'^\d{1,4}$', author.strip()):
            folder_author = self.author_from_path()
            if folder_author:
                log.debug('incipit author from folder: %s' % folder_author)
                return self.clean_search_author(folder_author)

        return self.clean_search_author(author or '')

    def parent_author_in_path(self, tag_author):
        """
            The matched parent artist's name IF it differs from the ALBUMARTIST
            tag AND is a folder in this file's path -- i.e. the tag is a narrator
            (or other non-author) and the parent is the real author, confirmed on
            disk. Returns None otherwise, so a correctly tagged album (tag ==
            parent) is never changed and a wrong parent name that isn't in the
            path is never used.
        """
        try:
            parent = self.media.parent_metadata
            if not parent or not parent.title:
                return None
            parent_title = parent.title
            if (
                tag_author
                and parent_title.strip().lower() == tag_author.strip().lower()
            ):
                return None
            if not self.media.filename:
                return None
            path = urllib.unquote(self.media.filename).decode('utf8')
            segments = [
                seg.strip().lower() for seg in path.split('/') if seg.strip()
            ]
            if parent_title.strip().lower() in segments:
                return parent_title
        except Exception as e:
            log.error('incipit parent_author_in_path failed: %s', e)
        return None

    def clean_search_author(self, author):
        """
            Strip a trailing "(Series)" qualifier from the book-search author so
            the API scores on the real author ("Terry Pratchett (Discworld)" ->
            "Terry Pratchett"), mirroring the artist-match path. Without this the
            "(Discworld)" tanks author-similarity and drags the whole match below
            the auto-match threshold.
        """
        if author and self.prefs['strip_series_from_author']:
            return self.clear_series_text(author)
        return author or ''

    def author_from_path(self):
        """
            The first path segment under a Plex library root — the author, by
            the <root>/<Author>/... audiobook convention. Returns None if the
            file path or the library roots can't be resolved (safe: the caller
            then keeps the tag-derived author).
        """
        try:
            if not self.media.filename:
                return None
            path = urllib.unquote(self.media.filename).decode('utf8')
            for root in get_library_roots():
                prefix = root if root.endswith('/') else root + '/'
                if path.startswith(prefix):
                    segment = path[len(prefix):].split('/')[0].strip()
                    if segment:
                        return segment
        except Exception as e:
            log.error('incipit author_from_path failed: %s', e)
        return None

    def incipit_extra_args(self):
        """
            Builds the extra query params for the incipit-api search, and logs
            the media object so we can confirm how duration/tracks are exposed.
        """
        # TYPED Fix Match search: the typed title is the WHOLE query. Every
        # extra this function adds is automatic-scan context describing the
        # CURRENT file -- i.e. the identity the user is trying to ESCAPE. The
        # damage is concrete: the file's DURATION vetoes every edition of a
        # different-length book below the floor (the rescue returns nothing),
        # the filename/sidecar ASIN re-pins the match being corrected, and the
        # TRACK TITLE leak was observed live -- a typed "Project Hail Mary"
        # returned "Pearls of Lutra" because the widening pass searched the
        # track title. The dialog's auto-fired list (is_typed_search False)
        # keeps all of it: it re-runs the automatic match, so duration
        # corroboration and the ASIN pin score it like a scan (100, not 85).
        if self.is_typed_search():
            return ''
        extra = ''
        # Probe: log the media attributes we can reach. getattr() and dir() are
        # BOTH blocked in the Plex plugin sandbox, so read attrs directly (these
        # four are the same ones pre_search_logging reads successfully).
        try:
            log.debug(
                'incipit media: artist=%s album=%s title=%s name=%s' % (
                    self.media.artist,
                    self.media.album,
                    self.media.title,
                    self.media.name,
                )
            )
        except Exception as e:
            log.error('incipit media log failed: %s', e)

        # Album duration (ms) = sum of the track part durations. In the legacy
        # album media object `tracks` is a dict keyed by track index, so iterate
        # its values (iterating the dict itself yields string keys). Each track
        # exposes items -> parts, each part carrying its own duration; any
        # missing link raises and is caught, leaving duration None.
        duration = None
        try:
            tracks = self.media.tracks
            try:
                track_iter = tracks.values()
            except Exception:
                track_iter = tracks
            total = 0
            for track in (track_iter or []):
                for item in (track.items or []):
                    for part in (item.parts or []):
                        # Sum each part independently: one malformed duration
                        # (a non-integer string) must skip only that part, not
                        # abort the whole album sum -- which would drop the
                        # duration (None) and silently lose the duration veto,
                        # the main wrong-edition guard.
                        try:
                            if part.duration:
                                # Parts expose duration as a string here.
                                total += int(part.duration)
                        except Exception:
                            continue
            if total:
                duration = total
        except Exception as e:
            log.error('incipit duration probe failed: %s', e)
        log.debug('incipit duration resolved: %s' % str(duration))
        # Plex reports -1 for a not-yet-analyzed file; only send a real runtime.
        if duration and duration > 0:
            extra += '&duration=' + urllib.quote(str(duration))

        # ASIN hint: the filename (Audiobookshelf/seanap tag) if present, else the
        # metadata.json sidecar. Sent as &asin= so the incipit-api PINS it
        # (definitive) when it's among the search results, but still falls back to
        # title/author scoring when it isn't -- unlike a hard ASIN override, which
        # short-circuits and fails outright when the ASIN lookup is empty (proven
        # live: a brand-new Podium ASIN returned no Audible-catalog results).
        # (Typed searches never reach this function -- see the early return --
        # so the hint only ever rides automatic scans and the dialog's
        # auto-fired list, where pinning the known identity is exactly right.)
        asin_hint = None
        try:
            if self.media.filename:
                fn = urllib.unquote(self.media.filename).decode('utf8')
                asin_match = self.search_asin(fn)
                if asin_match:
                    asin_hint = asin_match.group(0)
        except Exception as e:
            log.error('incipit asin probe failed: %s', e)
        if not asin_hint:
            sc = self.sidecar()
            if sc:
                sc_asin = sc.get('asin')
                if isinstance(sc_asin, (str, unicode)):
                    sc_match = self.search_asin(sc_asin.upper())
                    if sc_match:
                        asin_hint = sc_match.group(0)
        if asin_hint:
            extra += '&asin=' + urllib.quote(asin_hint)
            log.info('incipit asin hint: %s', asin_hint)

        # First track title — fallback when the album tag has no real title.
        track_title = None
        for accessor in (
            lambda: self.media.tracks[0].title,
            lambda: self.media.children[0].title,
        ):
            try:
                value = accessor()
                if value:
                    track_title = value
                    break
            except Exception:
                continue
        if track_title:
            track_title = strip_part_index(track_title)
        log.debug('incipit track title resolved: %s' % str(track_title))
        if track_title and track_title != self.normalizedName:
            extra += '&trackTitle=' + urllib.quote(track_title)

        return extra

    def check_if_preorder(self, book_date):
        """
            Checks if the book is a preorder.
            If so, it is excluded from the search.
        """
        current_date = (date.today())
        if book_date > current_date:
            log.info("Excluding pre-order book")
            return True

    def name_to_initials(self, input_name):
        """
            Converts a name to initials.
            Shorten input_name by splitting on whitespaces
            Only the surname stays as whole, the rest gets truncated
            and merged with dots.
            Example: 'Arthur Conan Doyle' -> 'A.C.Doyle'
            Example: 'J K Rowling' -> 'J.K.Rowling'
            Example: 'J. R. R. Tolkien' -> 'J.R.R.Tolkien'
        """

        # Remove quotation marks
        input_name = input_name.replace('"', '')

        # Split name into parts
        name_parts = self.clear_contributor_text(input_name).split()

        # Check if prename and surname exist, otherwise exit
        if len(name_parts) < 2:
            return input_name

        new_name = ""
        # Truncate prenames
        for part in name_parts[:-1]:
            try:
                # Try to get first letter of prename and add dot
                new_name += part[0] + "." if part[1] != "." else part
            except IndexError:
                # If there is only one letter, add dot and return
                new_name += part + "." if part != "." else part
        # Add surname
        new_name += name_parts[-1]

        return new_name

    def normalize_name(self):
        """
            Normalizes the album name by removing
            unwanted characters and words.
        """
        # Get name from either album or title. resolve_search_title() recovers a
        # title from the book folder when the album tag is missing/"[Unknown
        # Album]", so a book with no album tag still normalizes to a real title.
        input_name = self.resolve_search_title() or self.media.title
        log.debug('Input Name: %s', input_name)

        # Remove Diacritics
        name = String.StripDiacritics(input_name)
        # Remove brackets and text inside
        name = re.sub(r'\[[^"]*\]', '', name)
        # Remove unwanted characters
        name = re.sub(r'[^\w\s]', '', name)
        # Remove unwanted words
        name = re.sub(r'\b(official|audiobook|unabridged|abridged)\b',
                      '', name, flags=re.IGNORECASE)
        # Remove unwanted whitespaces
        name = re.sub(r'\s+', ' ', name)
        # Remove leading and trailing whitespaces
        name = name.strip()
        # Set class variable
        self.normalizedName = name
        log.debug('Normalized Name: %s', self.normalizedName)

        return name

    def parse_api_response(self, api_response):
        """
            Collects keys used for each item from API response, for Plex search
            results. The incipit-api returns a flat list of scored candidates;
            the Audible catalog returns {"products": [...]}. Branch on the shape.
        """
        if isinstance(api_response, list):
            return self.parse_incipit_candidates(api_response)
        return self.parse_audible_products(api_response)

    def parse_audible_products(self, api_response):
        """
            Maps Audible catalog products to Plex search-result dicts.
        """
        search_results = []
        for item in api_response['products']:
            # Only append results which have valid keys
            if item.viewkeys() >= {
                "asin",
                "authors",
                "language",
                "narrators",
                "release_date",
                "title"
            }:
                search_results.append(
                    {
                        'asin': item['asin'] + '_' + self.region_override,
                        'author': item['authors'],
                        'date': item['release_date'],
                        'language': item['language'],
                        'narrator': item['narrators'],
                        'region': self.region_override,
                        'title': item['title'],
                    }
                )
        return search_results

    def parse_incipit_candidates(self, candidates):
        """
            Maps incipit-api scored candidates (already past the server-side
            duration veto and confidence floor) to Plex search-result dicts.
            Authors and narrators arrive as name strings; the scorer indexes
            [0]['name'] on each, so empties are backfilled to avoid a crash
            (e.g. a Xanth book that has no narrator).
        """
        search_results = []
        for c in candidates:
            try:
                authors = [{'name': a} for a in c.get('authors', []) if a]
                if not authors:
                    authors = [{'name': self.media.artist or ''}]
                narrators = [{'name': n} for n in c.get('narrators', []) if n]
                if not narrators:
                    narrators = [{'name': ''}]
                search_results.append(
                    {
                        'asin': c['id'] + '_' + self.region_override,
                        'author': authors,
                        'date': '',
                        'language': 'english',
                        'narrator': narrators,
                        'region': self.region_override,
                        'title': c.get('title', ''),
                        # The API's 0-1 confidence (normalized title + author +
                        # duration). Carried through so we score on it rather
                        # than re-deriving a cruder score locally.
                        'confidence': c.get('confidence'),
                    }
                )
            except Exception as e:
                log.error('incipit candidate parse failed: %s', e)
        return search_results

    def pre_search_logging(self):
        """
            Logs basic metadata before search.
        """
        log.separator(msg='ALBUM SEARCH', log_level="info")
        # Log basic metadata
        data_to_log = [
            {'ID': self.media.parent_metadata.id},
            {'Title': self.media.title},
            {'Name': self.media.name},
            {'Album': self.media.album},
            {'Artist': self.media.artist},
        ]
        log.metadata(data_to_log)
        log.separator(log_level="info")

        # Handle a couple of edge cases where
        # album search will give bad results.
        if self.media.album is None and not self.manual:
            if self.media.title:
                log.warn('Using track title since album title is missing.')
                self.media.album = self.media.title
                return True
            log.info('Album Title is NULL on an automatic search.  Returning')
            return None
        if self.media.album == '[Unknown Album]' and not self.manual:
            log.info(
                'Album Title is [Unknown Album]'
                ' on an automatic search.  Returning'
            )
            return None

        if self.manual:
            # If this is a custom search,
            # use the user-entered name instead of the scanner hint.
            if self.media.name:
                log.info(
                    'Custom album search for: ' + self.media.name
                )
                self.media.album = self.media.name
        return True


class ArtistSearchTool(SearchTool):
    def build_search_args(self):
        """
            Builds the search query for the API.
        """
        # Reduce a multi-author artist ("Stephen King, Joe Hill") to its primary
        # author before searching, mirroring the album path. Otherwise the whole
        # comma-joined string is sent as one name and matches no author.
        if self.media.artist:
            self.handle_multi_artist()
        modified_artist_name = self.cleanup_author_name(self.media.artist)
        query = 'name=' + urllib.quote(modified_artist_name)
        # Set param
        return query

    def author_candidates(self):
        """
            Ordered author names parsed from a multi-author artist tag, each
            cleaned of contributor/series text. The search tries them in order,
            so when the first name is actually the narrator (e.g. "Jefferson
            Mays, Daniel Abraham, Ty Franck" or "John Bellairs/George Guidall")
            and matches nothing, the real author is tried next.
        """
        # Split the FULL author string (preserved before handle_multi_artist
        # collapsed media.artist to the primary), falling back to the current
        # artist if get_primary_author never ran.
        source = self.multi_author_source or self.media.artist or ''
        candidates = []
        for part in MULTI_AUTHOR_RE.split(source):
            cleaned = self.clear_contributor_text(part)
            if self.prefs['strip_series_from_author']:
                cleaned = self.clear_series_text(cleaned)
            cleaned = cleaned.strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        return candidates

    def cleanup_author_name(self, name):
        """
            Cleans up the author name by removing
            unwanted characters and words.
        """
        log.debug('Artist name before cleanup: ' + name)

        # Remove brackets and text inside
        name = re.sub(r'\[[^"]*\]', '', name)
        # Remove certain strings, such as titles
        str_to_remove = [
            'Dr.',
            'EdD',
            'Prof.',
            'Professor',
        ]
        str_to_remove_regex = re.compile(
            '|'.join(map(re.escape, str_to_remove))
        )
        name = str_to_remove_regex.sub('', name)
        # Remove periods between leading initials ("A. E." -> "A E"), keeping the
        # FULL remaining surname. group(2) used to be a single \w+, which dropped
        # every surname word after the first — "A. E. van Vogt" -> "A E van",
        # "W. E. B. Du Bois" -> "W E B Du". Capture the whole remainder instead.
        initials_regex = "^((?:[A-Z]\.\s?)*[A-Z]\.)(?!\S)\s*(.+)$"
        initials_matched = re.search(initials_regex, name)
        if initials_matched:
            log.debug('Found initials to clean')
            cleaned_initials = (
                initials_matched.group(1)
                .replace(' ', '')
                .replace('.', ' ')
            )
            name = re.sub(
                r'\s+', ' ',
                cleaned_initials + ' ' + initials_matched.group(2)
            ).strip()

        log.debug('Artist name after cleanup: ' + name)
        return name

    def find_non_contributor(self, author_array):
        """
            Finds the first author in the list
            that is not a contributor.
        """
        # Go through list of artists until we find a non contributor
        for i, r in enumerate(author_array):
            if self.clear_contributor_text(r) != r:
                log.debug('Author #' + str(i+1) + ' is a contributor')
                # If all authors are contributors use the first
                if i == len(author_array) - 1:
                    log.debug(
                        'All authors are contributors, using the first one'
                    )
                    self.media.artist = self.clear_contributor_text(
                        author_array[0]
                    )
                    return
                continue
            log.info(
                'Merging multi-author "' +
                self.media.artist +
                '" into top-level author "' +
                r + '"'
            )
            self.media.artist = r
            return

    def handle_multi_artist(self):
        """
            Handles multi-artist lists.
        """
        author_array = [
            a.strip()
            for a in MULTI_AUTHOR_RE.split(self.media.artist)
            if a.strip()
        ]
        if len(author_array) > 1:
            self.find_non_contributor(author_array)
        else:
            if (
                self.clear_contributor_text(self.media.artist)
                !=
                self.media.artist
            ):
                log.debug('Stripped contributor tag from author')
                self.media.artist = self.clear_contributor_text(
                    self.media.artist
                )

        # Strip a trailing "(Series)" qualifier so a phantom "Author (Series)"
        # artist matches the real author. Opt-out via the pref for anyone who
        # would rather leave such artists unmatched.
        if self.prefs['strip_series_from_author']:
            series_cleaned = self.clear_series_text(self.media.artist)
            if series_cleaned != self.media.artist:
                log.info(
                    'Stripped series qualifier from author: "%s" -> "%s"',
                    self.media.artist,
                    series_cleaned
                )
                self.media.artist = series_cleaned

    def artist_path(self):
        """The decoded file path for this artist's album, or None. The artist
           search media carries media.filename (confirmed live)."""
        try:
            if self.media.filename:
                return urllib.unquote(self.media.filename).decode('utf8')
        except Exception as e:
            log.error('incipit artist_path failed: %s', e)
        return None

    def artist_album_title(self):
        """The album/book title to search for. media.album / media.name carry it
           on the artist search; fall back to the file's basename."""
        for getter in (lambda: self.media.album, lambda: self.media.name):
            try:
                val = getter()
            except Exception:
                val = None
            if val:
                return val
        path = self.artist_path()
        if path:
            base = path.rsplit('/', 1)[-1]
            base = re.sub(r'\.[^.]+$', '', base).strip()
            if base:
                return base
        return None

    def artist_duration(self):
        """Album duration in ms, or None. Lets the book search pick the right
           edition; optional -- path confirmation is what guards correctness."""
        try:
            d = self.media.duration
            if d and int(d) > 0:
                return int(d)
        except Exception:
            pass
        return None

    def book_search_url(self):
        """The incipit-api /books URL for this artist's album (title [+ duration]).
           None when there is no configured API base or no title to search."""
        if not self.prefs['api_base_url']:
            return None
        title = self.artist_album_title()
        if not title:
            return None
        region = self.region_override or self.prefs['region']
        query = 'title=' + urllib.quote(title.encode('utf8'))
        duration = self.artist_duration()
        if duration:
            query += '&duration=' + urllib.quote(str(duration))
        return RegionTool(
            content_type='books', query=query, region=region
        ).get_search_url()

    def author_confirmed_in_path(self, book_results):
        """From book-search JSON, return the first author name that is ALSO a
           folder in this file's path. The recovered author must be both a real
           book author for this title AND present on disk as this book's folder,
           so a wrong name (a same-title book by another author) can never win.
           Returns None when nothing is confirmed."""
        path = self.artist_path()
        if not path:
            return None
        segments = [seg.strip().lower() for seg in path.split('/') if seg.strip()]
        results = book_results if isinstance(book_results, list) else [book_results]
        for candidate in (results or []):
            try:
                for author in (candidate.get('authors', []) or []):
                    if author and author.strip().lower() in segments:
                        return author
            except Exception:
                continue
        return None

    def get_primary_author(self):
        """
            Checks for combined authors
            If matched, author name is set to None to prevent
            it being used in search query.
        """
        self.set_media_artist()

        # We need an author name to continue
        if not self.media.artist:
            return

        # Preserve the full author string BEFORE handle_multi_artist collapses it
        # to the primary, so author_candidates() can retry each author in order
        # (e.g. narrator-first tags like "Jefferson Mays, Daniel Abraham, ...").
        self.multi_author_source = self.media.artist

        # Handle multi-artist
        self.handle_multi_artist()

    def parse_api_response(self, api_response):
        """
            Collects keys used for each item from API response,
            for Plex search results.
        """
        search_results = []
        for item in api_response:
            # Only append results which have valid keys
            if item.viewkeys() >= {
                "asin",
                "name",
            }:
                search_results.append(
                    {
                        'asin': item['asin'],
                        'name': item['name'],
                    }
                )
        return search_results

    def set_media_artist(self):
        """
            Fall back to the title ONLY when no artist is set.

            A manual "Fix Match" puts the user's typed name in media.artist,
            while the framework also loads the stored artist tree into
            media.title. Overwriting artist with title unconditionally throws
            away the user's correction — e.g. an artist mis-scanned as "2025"
            (from a "2025 - Title" folder) could never be re-matched to its real
            author because every search fell back to "2025".
        """
        if self.media.artist:
            return
        if self.media.title:
            self.media.artist = self.media.title
        else:
            log.error("No artist to validate")


class ScoreTool:
    # Starting value for score before deductions are taken.
    INITIAL_SCORE = 100
    # Any score lower than this will be ignored.
    IGNORE_SCORE = 45

    def __init__(
        self,
        helper,
        index,
        info,
        locale,
        levenshtein_distance,
        result_dict,
        year=None
    ):
        self.calculate_score = levenshtein_distance
        self.helper = helper
        self.index = index
        self.info = info
        self.english_locale = locale
        self.result_dict = result_dict
        self.year = year

    def reduce_string(self, string):
        """
            Reduces a string to lowercase and removes
            punctuation and spaces.
        """
        normalized = string \
            .lower() \
            .replace('-', '') \
            .replace(' ', '') \
            .replace('.', '') \
            .replace(',', '')
        return normalized

    def run_score_author(self):
        """
            Scores an author result.
        """
        self.asin = self.result_dict['asin']
        self.author = self.result_dict['name']
        self.authors_concat = self.author
        self.date = None
        self.language = None
        self.narrator = None
        self.region = None
        self.title = None
        return self.score_result()

    def run_score_book(self):
        """
            Scores a book result.
        """
        self.asin = self.result_dict['asin']
        self.authors_concat = ', '.join(
            author['name'] for author in self.result_dict['author']
        )
        self.author = self.result_dict['author'][0]['name']
        self.date = self.result_dict['date']
        self.language = self.result_dict['language'].title()
        self.narrator = self.result_dict['narrator'][0]['name']
        self.region = self.result_dict['region']
        self.title = self.result_dict['title']
        return self.score_result()

    def sum_scores(self, numberlist):
        """
            Sums a list of numbers.
        """
        # Because builtin sum() isn't available
        return reduce(
            lambda x, y: x + y, numberlist, 0
        )

    def score_create_result(self, score):
        """
            Creates a result dict for the score.
            Logs the score and the data used to calculate it.
        """
        data_to_log = []
        plex_score_dict = {}

        # Go through all the keys for the result and log as we go
        if self.asin:
            plex_score_dict['id'] = self.asin
            data_to_log.append({'ASIN is': self.asin})
        # Read unconditionally by the album search's display loop, so the key
        # must always exist. OpenLibrary editions frequently have no resolved
        # author, unlike Audible where it is always present.
        plex_score_dict['author'] = self.author or ''
        if self.author:
            data_to_log.append({'Author is': self.author})
        if self.date:
            plex_score_dict['date'] = self.date
            data_to_log.append({'Date is': self.date})
        # The album search's display loop reads r['narrator'] unconditionally, so
        # the key must always exist — Hardcover/OpenLibrary books frequently have
        # no narrator, unlike Audible where it is always present.
        plex_score_dict['narrator'] = self.narrator or ''
        if self.narrator:
            data_to_log.append({'Narrator is': self.narrator})
        if self.region:
            plex_score_dict['region'] = self.region
            data_to_log.append({'Region is': self.region})
        if score:
            plex_score_dict['score'] = score
            data_to_log.append({'Score is': str(score)})
        if self.title:
            plex_score_dict['title'] = self.title
            data_to_log.append({'Title is': self.title})
        # Likewise read unconditionally by the display loop; '' when the
        # candidate carries no parseable date (many provider records don't).
        plex_score_dict['year'] = self.year or ''

        # DEBUG: candidate dumps fire per candidate per TRACK — the dominant
        # search-side log volume during a scan.
        log.metadata(data_to_log, log_level="debug")
        return plex_score_dict

    def score_result(self):
        """
            Scores a result.
        """
        # incipit-api already scored and ranked these candidates on the
        # NORMALIZED title (series suffix stripped) + author + duration. Trust
        # that confidence directly instead of re-deriving a Levenshtein score
        # from the RAW album tag, which over-penalizes series/folder-formatted
        # names ("King of Duels: The Wandering Inn, Book 16" vs "King of Duels",
        # "The Pilot [The Last Horizon 4]" vs "The Pilot").
        incipit_conf = self.result_dict.get('confidence')
        if incipit_conf is not None:
            # Keep the API's best-first order; index nudges ties downward.
            score = int(round(incipit_conf * 100)) - self.index
            log.debug("Result #" + str(self.index + 1))
            plex_score_dict = self.score_create_result(score)
            if score >= self.IGNORE_SCORE:
                self.info.append(plex_score_dict)
            else:
                log.info(
                    '# Score is below ignore boundary (%s)... Skipping!',
                    self.IGNORE_SCORE
                )
            return

        # Array to hold score points for processing
        all_scores = []

        # Album name score
        if self.title:
            title_score = self.score_album(self.title)
            if title_score:
                all_scores.append(title_score)
        # Author name score
        if self.authors_concat:
            author_score = self.score_author(self.authors_concat)
            if author_score:
                all_scores.append(author_score)
        # Library language score
        if self.language:
            lang_score = self.score_language(self.language)
            if lang_score:
                all_scores.append(lang_score)

        # Subtract difference from initial score
        # Subtract index to use Audible relevance as weight
        score = self.INITIAL_SCORE - self.sum_scores(all_scores) - self.index

        log.debug("Result #" + str(self.index + 1))

        # Create result dict
        plex_score_dict = self.score_create_result(score)

        if score >= self.IGNORE_SCORE:
            self.info.append(plex_score_dict)
        else:
            log.info(
                '# Score is below ignore boundary (%s)... Skipping!',
                self.IGNORE_SCORE
            )

    def score_album(self, title):
        """
            Compare the input album similarity to the search result album.
            Score is calculated with LevenshteinDistance
        """
        scorebase1 = self.helper.media.album
        if not scorebase1:
            log.error('No album title found in file metadata')
            return 50
        scorebase2 = title.encode('utf-8')
        album_score = self.calculate_score(
            self.reduce_string(scorebase1),
            self.reduce_string(scorebase2)
        ) * 2
        log.debug("Score deduction from album: " + str(album_score))
        return album_score

    def score_author(self, author):
        """
            Compare the input author similarity to the search result author.
            Score is calculated with LevenshteinDistance
        """
        if self.helper.media.artist:
            scorebase3 = self.helper.media.artist
            scorebase4 = author
            author_score = self.calculate_score(
                self.reduce_string(scorebase3),
                self.reduce_string(scorebase4)
            ) * 10
            log.debug("Score deduction from author: " + str(author_score))
            return author_score

        log.warn('No artist found in file metadata')
        return 20

    def score_language(self, language):
        """
            Compare the library language to search results
            and knock off 2 points if they don't match.
        """
        lang_dict = {
            self.english_locale: 'English',
            'de': 'German',
            'es': 'Spanish',
            'fr': 'French',
            'it': 'Italian',
            'ja': 'Japanese',
        }

        if language != lang_dict[self.helper.lang]:
            log.debug(
                'Audible language: %s; Library language: %s',
                language,
                lang_dict[self.helper.lang]
            )
            log.debug("Book is not library language, deduct 2 points")
            return 2
        return 0
