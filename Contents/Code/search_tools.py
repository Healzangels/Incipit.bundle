from datetime import date
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


def get_library_roots():
    """
        The server's library section root paths, fetched once and cached.
        Returns [] on any failure (callers degrade to the tag-derived author).
    """
    global LIBRARY_ROOTS_CACHE
    if LIBRARY_ROOTS_CACHE is not None:
        return LIBRARY_ROOTS_CACHE
    roots = []
    try:
        xml = str(HTTP.Request(
            'http://127.0.0.1:32400/library/sections', cacheTime=0, timeout=20))
        roots = re.findall(r'<Location [^>]*path="([^"]+)"', xml)
    except Exception as e:
        log.error('incipit library roots fetch failed: %s', e)
    LIBRARY_ROOTS_CACHE = roots
    return roots


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

        # ASIN override
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
    def build_search_args(self):
        """
            Builds the search arguments for the API call.
        """
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
            raw_title = self.media.album or self.media.title or self.normalizedName
            query = 'title=' + urllib.quote(String.StripDiacritics(raw_title))
            author = self.resolve_author()
            if author:
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
        author = self.media.artist
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
                        if part.duration:
                            # Parts expose duration as a string in this model.
                            total += int(part.duration)
            if total:
                duration = total
        except Exception as e:
            log.error('incipit duration probe failed: %s', e)
        log.debug('incipit duration resolved: %s' % str(duration))
        # Plex reports -1 for a not-yet-analyzed file; only send a real runtime.
        if duration and duration > 0:
            extra += '&duration=' + urllib.quote(str(duration))

        # Filename ASIN (Audiobookshelf/seanap tag), if present.
        try:
            if self.media.filename:
                fn = urllib.unquote(self.media.filename).decode('utf8')
                asin_match = self.search_asin(fn)
                if asin_match:
                    extra += '&asin=' + urllib.quote(asin_match.group(0))
        except Exception as e:
            log.error('incipit asin probe failed: %s', e)

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
        # Get name from either album or title
        input_name = self.media.album if self.media.album else self.media.title
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

    def folder_author(self):
        """
            Recover the real author from the library folder when the tagged
            artist name matched no author -- typically a NARRATOR mis-tagged as
            the artist (e.g. "Lauren Fortgang"), whose real author only exists on
            disk as the enclosing folder. Map a track file path to the first
            segment under a /library/sections root (the <root>/<Author>/
            convention, same one the album path relies on).

            The ARTIST media does not carry a flat media.filename the way an
            album does, so fall back to walking the album tree. Returns None on
            any failure; an INFO probe logs filename + roots so a scan shows why
            it did or didn't fire. NB: the Plex sandbox bans getattr/hasattr, so
            use only direct attribute access.
        """
        try:
            filename = None
            try:
                filename = self.media.filename
            except Exception:
                filename = None
            if not filename:
                filename = self.first_album_track_path()
            roots = get_library_roots()
            log.info(
                'incipit artist folder probe: filename=%r roots=%r',
                filename, roots
            )
            if not filename:
                return None
            path = urllib.unquote(filename).decode('utf8')
            for root in roots:
                prefix = root if root.endswith('/') else root + '/'
                if path.startswith(prefix):
                    segment = path[len(prefix):].split('/')[0].strip()
                    if segment:
                        return segment
            log.info('incipit artist folder probe: path=%r matched no root', path)
        except Exception as e:
            log.error('incipit artist folder_author failed: %s', e)
        return None

    def first_album_track_path(self):
        """
            A track file path from this artist's album tree, or None. Walks
            media.albums -> <album>.tracks -> <track>.items -> <part>.file using
            the same dict-.values() pattern the album duration probe uses; any
            missing link is swallowed (the sandbox has no getattr/hasattr).
        """
        try:
            albums = self.media.albums
        except Exception:
            return None
        try:
            album_iter = albums.values()
        except Exception:
            album_iter = albums
        for album in (album_iter or []):
            try:
                tracks = album.tracks
                try:
                    track_iter = tracks.values()
                except Exception:
                    track_iter = tracks
                for track in (track_iter or []):
                    for item in (track.items or []):
                        for part in (item.parts or []):
                            if part.file:
                                return part.file
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

        log.metadata(data_to_log, log_level="info")
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
            log.info("Result #" + str(self.index + 1))
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

        log.info("Result #" + str(self.index + 1))

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
