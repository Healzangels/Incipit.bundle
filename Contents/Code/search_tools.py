from datetime import date
import json
import re
# Import internal tools
from logging import Logging
from region_tools import RegionTool, available_regions
import urllib

# Setup logger
log = Logging()

# The FILENAME probe requires the B0 prefix: a shape-only match let a
# 10-character run inside an ISBN-13 ("9780593399") quick-match at score 100,
# skipping the whole pipeline and pinning the album to a record that 404s. The
# sidecar path was hardened for exactly this; this one never was (2026-07-28).
asin_regex = re.compile(r'B0[A-Z\d]{8}')
# The regions the API will accept, taken from the one table that defines them
# rather than a second hand-maintained list.
#
# `available_regions` IS the membership test -- no derived collection. The
# first cut built a frozenset here, and `frozenset` is one of the sandbox's
# blocked builtins, so the NameError fired at IMPORT and took the entire
# plugin down: no matching at all, and the only evidence was a CRITICAL
# "Exception starting plug-in" in the agent log (live, 2026-07-28). Dict
# membership needs no builtin and is the same O(1) test.
KNOWN_REGIONS = available_regions
# A TYPED search is the most explicit identity a user can give, so it keeps
# the historical shape-only form.
typed_asin_regex = re.compile(r'(?=.\d)[A-Z\d]{10}')
region_regex = re.compile(r'(?<=\[)[A-Za-z]{2}(?=\])')
# THE name-comparison key for the whole bundle. Lives here because both
# __init__ and update_tools already import from this module, and neither can
# import the other without a cycle -- so this is the one place all three can
# share, and there is no longer a second spelling to drift from.
#
# \W with re.UNICODE, NOT the older [^a-z0-9]: that class DELETED accented
# letters rather than folding them, so "Jose Saramago" (an ASCII folder on an
# SMB share) and "Jos\xe9 Saramago" (the tag) keyed to 'josesaramago' vs
# 'jossaramago' and never matched -- silently disabling folder_author_confirmed,
# the root-free swap-correction gate. A fully non-Latin name folded to '' and
# was permanently dead there.
NAME_KEY_STRIP_RE = re.compile(r'[\W_]+', re.UNICODE)


def name_key(value):
    """
        Punctuation/space/case-insensitive key for comparing two person names,
        so "J.K. Rowling", "J. K. Rowling" and "JK Rowling" all compare equal,
        and so do "Jose Saramago" and its accented spelling.

        StripDiacritics FOLDS rather than deletes, and the word-char strip keeps unicode
        word characters, so a Cyrillic or CJK name yields a real key instead of
        collapsing to ''.

        The `folded.strip()` test is load-bearing: StripDiacritics is the
        NFKD -> encode('ASCII','ignore') idiom, so a MULTI-WORD non-Latin name
        folds to nothing but its spaces -- a two-word Cyrillic name becomes ' ', which is TRUTHY.
        A bare `if folded:` therefore replaced the name with a space and returned
        '', and every caller that treats '' as "no name" bailed. A single-token
        name folds to '' (falsy) and kept the original, which is why the hole
        stayed hidden.
    """
    if not value:
        return ''
    try:
        folded = String.StripDiacritics(value)
        if folded and folded.strip():
            value = folded
    except Exception:
        pass
    try:
        return NAME_KEY_STRIP_RE.sub('', value.lower())
    except Exception:
        return ''


def quote_param(value):
    """
        urllib.quote for any value that may arrive as framework/json unicode.

        Py2's quote looks its safe-map up BY BYTE, so a unicode argument with a
        codepoint > 127 raises KeyError and takes the whole search down -- a
        crash reachable from any non-ASCII author or title (a Danish series
        name, an accented author). A byte str (the tag path) is passed through
        unchanged. Every query parameter goes through here so the guard can
        never again be present on one search path and missing on another.
    """
    if value is None:
        return ''
    if not isinstance(value, str):
        value = value.encode('utf8')
    return urllib.quote(value)

# Plex library section root paths, fetched once. Used to derive the author from
# a file path (<root>/<Author>/...) when the ALBUMARTIST tag is missing or bad.
# NB: the Plex RestrictedPython sandbox forbids names starting with "_".
LIBRARY_ROOTS_CACHE = None

# Co-author separators for reducing a multi-author artist to its authors:
# comma, ampersand, semicolon, slash, or the word "and". Whitespace around
# "and"/"&" keeps it from splitting inside a name (e.g. "Rand", "Anderson").
#
# The PATTERN is kept as a string because a second consumer (NFO_CREDIT_SPLIT_RE,
# down in the nfo section) has to embed it: nfo_people hand-rolled its own
# `value.split(',')` instead, which both missed "Terry Pratchett & Neil Gaiman"
# -- one joined name -- and cut role notes in half. One spelling, two regexes.
MULTI_AUTHOR_PATTERN = r'\s*,\s*|\s+&\s+|\s+and\s+|\s*;\s*|\s*/\s*'
MULTI_AUTHOR_RE = re.compile(MULTI_AUTHOR_PATTERN, re.IGNORECASE)


# Alternate cover art carried from SEARCH to UPDATE.
#
# The api attaches `coverAlternates` to a SEARCH candidate: dedupe builds them
# from the editions it merged, so they exist only where the whole candidate set
# is visible. Plex then discards the search results and asks `/books/{id}` for
# the chosen one, and that route does not run dedupe -- so without this bridge
# the alternates are gone by the time posters are offered.
#
# Same shape as the other cross-call memos in __init__ (verdict_memo,
# recent_work_memo): a module-level dict, cleared by process lifetime, keyed by
# the ONLY thing both calls share -- the id Plex carries forward.
ALTERNATE_COVER_MEMO = {}


def alternate_cover_key(book_id):
    """
        The memo key for a book id.

        Search emits the BARE asin; update sees "<asin>_<region>" (see
        update_tools, which splits on '_' for exactly this reason). Normalising
        both ends means the memo actually hits on the path it exists for --
        without it every recall would miss and the bridge would be dead code
        that still looked wired.
    """
    try:
        return book_id.split('_')[0]
    except Exception:
        return None


def remember_alternate_covers(book_id, urls):
    """Record a candidate's alternate covers for the later update call."""
    key = alternate_cover_key(book_id)
    if not key or not urls:
        return
    kept = []
    for url in urls:
        try:
            if url and url.strip() and url not in kept:
                kept.append(url)
        except Exception:
            continue
    if kept:
        ALTERNATE_COVER_MEMO[key] = kept


def recall_alternate_covers(book_id):
    """The alternate covers recorded for this id at search time, or []."""
    key = alternate_cover_key(book_id)
    if not key:
        return []
    return ALTERNATE_COVER_MEMO.get(key) or []


def clear_series_text(string):
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

        MODULE-LEVEL (with SearchTool.clear_series_text delegating to it, so
        every existing caller is unchanged) because nfo_people needs the same
        trailing-parenthetical rule and cannot reach a SearchTool method.
        name_key is the precedent.
    """
    if not string:
        return string
    stripped = re.sub(r'\s*\([^()]{1,60}\)\s*$', '', string).strip()
    # Guard: if stripping leaves too little to be a name, keep the original.
    if len(stripped) < 2:
        return string
    return stripped

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
# Synthetic incipit record ids the API can resolve via /books/{id}. B0 ASINs
# stay in the sidecar's `asin` field with its own guards; this is only for
# records that have no ASIN at all.
# The separator is REQUIRED and underscores are NOT allowed in the body: the
# quick-match id is joined as `id + '_' + region` and the update splits on the
# FIRST underscore, so `hardcover_12345` would resolve to namespace
# "hardcover", region "12345" -- a permanent 404 applied at score 100. A
# separator-less `overdrive9406208` is equally unresolvable provider-side.
SIDECAR_INCIPIT_ID_RE = re.compile(
    r'^(?:openlibrary|hardcover|overdrive)-[A-Za-z0-9-]+$')
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
        # A sidecar `incipit_id` is the operator's hand-written record pin for
        # a recording no catalog carries (the 2010: Odyssey Two class: an NLS
        # talking book whose only honest record is a narrator-less OpenLibrary
        # work row -- confidence scoring can never safely auto-apply one). It
        # rides the same deterministic quick-match lane as an embedded ASIN,
        # outranking a filename ASIN because a human wrote it, and like the
        # filename ASIN it never overrides a TYPED search (the user actively
        # correcting identity).
        if self.content_type == 'books' and not self.is_typed_search():
            incipit_id = self.sidecar_incipit_id()
            if incipit_id:
                log.info('incipit id pin found in sidecar: %s', incipit_id)
                # The region marker lives in the PATH ("[uk]"), never in a
                # synthetic id -- passing the id here silently defaulted every
                # pinned book to the operator's global region and baked that
                # into metadata.id permanently (2026-07-28 review).
                region_source = incipit_id
                if self.media.filename:
                    try:
                        region_source = urllib.unquote(self.media.filename)
                        try:
                            region_source = region_source.decode('utf8')
                        except Exception:
                            # py2 str decodes; a py3 str has no .decode and is
                            # already text. The marker is ASCII either way.
                            pass
                    except Exception as e:
                        log.error('incipit id pin: region read failed (%s)', e)
                self.check_for_region(region_source)
                return incipit_id + '_' + self.region_override

        # Check filename for ASIN if content type is books.
        # NOT on a TYPED Fix Match search: this quick match runs BEFORE
        # build_search_args, so an ungated filename ASIN re-pinned exactly the
        # identity the user is correcting and the typed query never executed --
        # the one context leak the is_typed_search gates missed. The TYPED-text
        # ASIN branch below stays: typing an ASIN into Search Options is the
        # most explicit identity a user can give.
        if (
            self.media.filename
            and self.content_type == 'books'
            and not self.is_typed_search()
        ):
            # Pre-assign: if the decode below raises (non-UTF-8 filename), the
            # except logs and execution continues to the `if` — an unassigned
            # local there was a NameError that killed the whole search.
            filename_unquoted = None
            filename_search_asin = None
            try:
                # Provide a plain filename for ASIN search
                filename_unquoted = urllib.unquote(self.media.filename)
                try:
                    filename_unquoted = filename_unquoted.decode('utf8')
                except Exception:
                    # py2 str decodes; a py3 str has no .decode and is already
                    # text. An ASIN and a region marker are ASCII either way.
                    pass
                filename_search_asin = self.search_asin(filename_unquoted)
            except Exception as e:
                log.error('Error checking filename for ASIN: %s', e)

            if filename_search_asin:
                log.info('ASIN found in filename')
                self.check_for_region(filename_unquoted)
                return filename_search_asin.group(0) + '_' + self.region_override

        # Check a TYPED query for an ASIN.
        #
        # GATED on is_typed_search(). The shape-only regex this uses
        # (r'(?=.\d)[A-Z\d]{10}') is deliberately loose because a human typing
        # into Search Options is naming an identity on purpose. This branch fed
        # it `self.media.album` -- the album TAG -- on every AUTOMATIC scan,
        # ungated, so a bare ISBN-13 in a tag quick-matched a 10-char slice at
        # score 100: search() returns immediately, so no fan-out, no scoring,
        # no duration veto, and Plex auto-applies it. The resulting
        # /books/9780593399 404s forever and only a TYPED Fix Match clears it.
        # v1.3.154 hardened the FILENAME probe for exactly this; this sibling
        # was left open.
        #
        # Nothing is lost on the automatic path: a real ASIN in the album tag is
        # still caught by pre_process_title -> search_asin(), which uses the
        # B0-anchored regex.
        #
        # Read media.name FIRST: a typed query puts the typed text there
        # (the auto-fired candidate list on dialog open carries name=None and
        # the item's own metadata instead), so album/artist are the fallback.
        if self.is_typed_search():
            # DEFENSIVELY, through the same getter idiom artist_album_title
            # uses. is_typed_search() wraps its OWN media.name read in
            # try/except and returns bool(self.manual) -- i.e. TRUE -- when the
            # read raises, so it hands control to this branch precisely in the
            # case where a bare `self.media.name` raises again. Reproduced:
            # manual=True plus a media object whose .name raises gave
            # "check_for_asin RAISED AttributeError", out through
            # AudiobookArtist.search()'s very first statement, which has no
            # enclosing try. New exposure -- the previous code never read .name.
            manual_asin = None
            for getter in (lambda: self.media.name,
                           lambda: self.media.album,
                           lambda: self.media.artist):
                try:
                    manual_asin = getter()
                except Exception:
                    manual_asin = None
                if manual_asin:
                    break
            manual_search_asin = self.search_asin(manual_asin, typed=True)

            if manual_search_asin:
                log.info('ASIN found in manual search')
                self.check_for_region(manual_asin)
                return manual_search_asin.group(0) + '_' + self.region_override

    # Check for region override
    def check_for_region(self, search_title):
        """
            Overrides the search with a region, but only with a region we can
            actually ask for.

            The marker regex matches ANY bracketed two letters, so `[CD]`,
            `[HQ]`, `[EN]` and the natural uppercase `[UK]` all became the
            region. The API validates against a lowercase enum and hard-400s
            anything else -- and an answered 4xx from our own host is treated
            as permanent -- so the search died with only "No results found",
            while the value was joined into metadata.id and made every later
            lookup 400 forever. On the stock Audible path the same value is an
            unguarded dict index (KeyError). Five reviewers found this
            independently on 2026-07-28; verified live.
        """
        match_region = self.search_region(search_title)
        candidate = match_region.group(0).lower() if match_region else None
        if candidate and candidate in KNOWN_REGIONS:
            log.info('Region found in title')
            self.region_override = candidate
        else:
            if candidate:
                log.info(
                    'incipit region: ignoring bracketed token [%s] -- not a '
                    'region we can request', candidate)
            self.region_override = self.prefs['region']
        log.info('Region Override: %s', self.region_override)

    def clear_contributor_text(self, string):
        contributor_regex = '.+?(?= -)'
        if re.match(contributor_regex, string):
            return re.match(contributor_regex, string).group(0)
        return string

    def clear_series_text(self, string):
        """The module-level rule (see clear_series_text), reachable as a method."""
        return clear_series_text(string)

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
        url_param = type_param + quote_param(asin)

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

    def sidecar_incipit_id(self):
        """
            The operator's explicit record pin from the sidecar, or None.

            Unlike every other sidecar field this is never machine-written:
            Audiobookshelf has no such key, so its presence means a human said
            "this file IS this record". Accepts only synthetic incipit
            namespaces (openlibrary-*/hardcover-*/overdrive-*) -- a B0 ASIN
            belongs in the `asin` field, and anything else (URLs, ISBNs,
            typos) is logged and ignored so a malformed pin degrades to a
            normal search rather than a bogus quick match.
        """
        sidecar = self.sidecar()
        if not sidecar:
            return None
        value = sidecar.get('incipit_id')
        if not isinstance(value, (str, unicode)):
            return None
        value = value.strip()
        if SIDECAR_INCIPIT_ID_RE.match(value):
            return value
        if value:
            log.info(
                'incipit id pin: ignoring unrecognized sidecar incipit_id %s',
                value)
        return None

    def sidecar_names(self, value):
        """
            A sidecar people field as a plain list of names.

            Tolerates the format variants seen in the wild rather than assuming
            a list of strings: an Audiobookshelf/OPF export can store one bare
            string (which would otherwise be iterated CHARACTER BY CHARACTER
            into "J, o, h, n"), a list of {"name": ...} dicts, or the singular
            dict on its own -- and in Py2 iterating that dict yields its KEYS,
            so the literal string "name" would be accepted as a person.
        """
        if isinstance(value, (str, unicode)) or isinstance(value, dict):
            value = [value]
        out = []
        for item in (value or []):
            if isinstance(item, dict):
                item = item.get('name')
            if item and isinstance(item, (str, unicode)):
                out.append(item)
        return out

    def names_include_folder_author(self, names, folder):
        """True if any of `names` is the folder's author (either containing)."""
        folder_folded = name_key(folder)
        if not folder_folded:
            return False
        for candidate in names:
            candidate_folded = name_key(candidate)
            if candidate_folded and (
                candidate_folded in folder_folded or folder_folded in candidate_folded
            ):
                return True
        return False

    def sidecar_people(self):
        """
            (authors, narrators) from the sidecar, with a SWAP corrected.

            Some exports write the two fields the wrong way round. Measured
            across this library's 1508 sidecars: 7 are swapped -- every UK
            Harry Potter book, carrying authors=["Stephen Fry"] and
            narrators=["J.K. Rowling"] -- and the agent faithfully searched
            `author=Stephen Fry`, because that is what the file said.

            The FOLDER author arbitrates, since <root>/<Author>/... is the one
            piece of this layout nobody mis-writes. Only a strict disagreement
            flips the fields: the narrators must name the folder author AND the
            authors must not. That guard is doing real work -- 69 sidecars in
            this library have the author narrating their own book (Redwall's
            GraphicAudio editions, Paolini, Baldacci, Elizabeth Gilbert), and
            every one of them names the folder author in BOTH fields, so none
            is touched. Measured false positives across the library: zero.

            Recovering the fields beats discarding them: the swapped sidecar
            still holds the true narrator, which is the signal that separates
            three same-title editions of a Harry Potter book.
        """
        try:
            return self.sidecar_people_cache
        except AttributeError:
            pass
        authors = []
        narrators = []
        sc = self.sidecar()
        if sc:
            authors = self.sidecar_names(sc.get('authors') or sc.get('author'))
            narrators = self.sidecar_names(sc.get('narrators') or sc.get('narrator'))
            folder = self.folder_author_confirmed()
            if folder and narrators and authors:
                authors_ok = self.names_include_folder_author(authors, folder)
                narrators_ok = self.names_include_folder_author(narrators, folder)
                if narrators_ok and not authors_ok:
                    log.warn(
                        'incipit sidecar: authors/narrators look SWAPPED for "%s" '
                        '(authors=%s, narrators=%s); the folder author arbitrates',
                        folder, ', '.join(authors), ', '.join(narrators)
                    )
                    swapped = authors
                    authors = narrators
                    narrators = swapped
        self.sidecar_people_cache = (authors, narrators)
        return self.sidecar_people_cache

    def sidecar_series(self):
        """
            (name, position) from the sidecar's series field, or (None, None).

            Local, machine-written and more authoritative than parsing the
            folder path -- which is what produced "Pocket Potters, Book 1" for
            a Harry Potter book whose own sidecar says "Harry Potter". Handles
            the same shape variants as the people fields, plus the "Name #3"
            and "Name, Book 3" spellings providers use.
        """
        sc = self.sidecar()
        if not sc:
            return (None, None)
        raw = sc.get('series') or sc.get('seriesName')
        names = self.sidecar_names(raw)
        if not names:
            return (None, None)
        entry = names[0]
        position = None
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            for key in ('sequence', 'position', 'number'):
                value = raw[0].get(key)
                if value is not None and str(value).strip():
                    position = str(value).strip()
                    break
        if position is None:
            match = re.search(r'(?:#|,\s*book\s+)(\d+(?:\.\d+)?)\s*$', entry, re.IGNORECASE)
            if match:
                position = match.group(1)
                entry = entry[:match.start()].strip().rstrip(',').strip()
        return (entry or None, position)

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

    def search_asin(self, input, typed=False):
        """
            Searches for an ASIN in a string.

            `typed=True` keeps the historical shape-only pattern for text the
            user typed into Search Options -- the most explicit identity they
            can give. Filenames get the B0-anchored form, because a shape-only
            match there pinned albums to ISBN substrings (2026-07-28).
        """
        if input:
            return re.search(typed_asin_regex if typed else asin_regex, input)

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
    def sidecar_title(self):
        """
            The metadata.json title, or None. NOT on a TYPED search: a Search
            Options query is the USER typing a correction, and the sidecar
            overriding it made Fix Match silently ignore whatever was typed
            whenever a metadata.json existed. The isinstance guard keeps a
            malformed sidecar (a list, a number) from crashing the search.
        """
        if self.is_typed_search():
            return None
        sc = self.sidecar()
        if not sc:
            return None
        sc_title = sc.get('title')
        if isinstance(sc_title, (str, unicode)) and sc_title.strip():
            return sc_title
        return None

    def quick_match_display(self, quick_match_id):
        """
            (name, year) for the quick-match Fix Match row. ASIN rows keep
            their historical shape (raw ASIN + upstream's dummy 1969 --
            harmless there because the /books/{asin} record's releaseDate
            overwrites it on update). A sidecar incipit_id pin row must not:
            the raw id read like a mismatch in the dialog, and the pinned
            OL/Hardcover record can lack a releaseDate, letting the dummy
            1969 land on the album card as 1969-12-31 (seen live on
            2010: Odyssey Two).
        """
        if quick_match_id.startswith('B0'):
            return quick_match_id, 1969
        title = self.sidecar_title()
        if not title and self.media.album:
            title = self.media.album
        return title or quick_match_id, None

    def folder_title(self):
        """
            A title recovered from the book folder, or None. Gated on the
            Always on: it only reads the folder when there is a filename to
            read it from, and the caller only asks when the tag is unusable.
        """
        if not self.media.filename:
            return None
        try:
            path = urllib.unquote(self.media.filename).decode('utf8')
            return folder_title_from_path(path)
        except Exception as e:
            log.error('incipit folder title recovery failed: %s', e)
            return None

    def resolve_search_title(self):
        """
            The book title to search on. Normally the album tag, but when that
            tag is absent or Plex's "[Unknown Album]" placeholder, recover the
            title from the book folder (see folder_title_from_path) so a book
            with no album tag still matches instead of searching for the literal
            "[Unknown Album]".
            Memoized so the recovery logs once. Falls back to the track-title
            tag, then whatever album value we have.
        """
        if self.resolved_title is not None:
            return self.resolved_title

        # Sidecar metadata.json title is authoritative over the (often scrambled)
        # album tag. Kept WITH any "[Series N]" suffix -- the API normalizer strips
        # it, and the structure helps the API rank the audio edition correctly.
        sc_title = self.sidecar_title()
        if sc_title:
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
        recovered = self.folder_title()
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
            query = 'title=' + quote_param(raw_title)
            author = self.resolve_author()
            if author:
                query += '&author=' + quote_param(author)
            # Extra signals the API can use: duration (the veto), a filename
            # ASIN, and the first track title (a fallback when the ALBUM tag is
            # a bare series+number).
            query += self.incipit_extra_args()
            if self.is_typed_search():
                # Telemetry only, never scoring: a typed query is authorless by
                # design, and unmarked it lands in the API's riskyAuthorless
                # bucket -- the counter that watches the AUTOMATIC
                # false-positive class -- making a manual-correction session
                # read as a quality regression.
                query += '&manual=1'
            return query

        # Audible catalog path.
        album_param = 'title=' + quote_param(self.normalizedName)
        # Fix match/manual search doesn't provide author
        if self.media.artist:
            artist_param = '&author=' + quote_param(self.media.artist)
        else:
            # Use keyword search to supplement missing author
            album_param = 'keywords=' + quote_param(self.normalizedName)
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
            # Through sidecar_people so the shape-tolerance and the SWAP
            # correction live in ONE place -- the narrator query below reads
            # the same corrected pair, and the two cannot drift apart.
            names = self.sidecar_people()[0]
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
        if author:
            return self.clear_series_text(author)
        return author or ''

    def folder_author_confirmed(self):
        """
            The author folder for this file, resolved ROOT-FREE.

            author_from_path() derives the author by stripping a get_library_roots()
            prefix -- but that call is BLOCKED at search time (see the note in
            resolve_author), so it returns None on every scan. That is exactly why
            the swap correction below never fired for the swapped UK Harry Potter
            sidecars: its arbiter was always None, so `if folder and ...` skipped,
            and the agent searched author=Stephen Fry / narrator=J.K. Rowling.

            Confirm the ALBUMARTIST against the path segments instead (the pattern
            parent_author_in_path uses on media.filename, which IS readable): when
            media.artist names a folder in this file's path it IS the
            <root>/<Author>/ segment. A NARRATOR-tagged ALBUMARTIST -- which never
            appears as an author folder -- fails the confirmation and returns None,
            so this can only ever supply a TRUE author, never mistake a narrator
            for one. Falls back to the root-based path for any context where the
            roots do resolve.
        """
        fp = self.author_from_path()
        if fp:
            return fp
        try:
            artist = self.media.artist
            if not artist or not self.media.filename:
                return None
            path = urllib.unquote(self.media.filename)
            # py2 unquote returns bytes needing the decode; a str already
            # decoded (the py3 test harness) must not be decoded again -- the
            # AttributeError was silently disabling this whole leg under test.
            if not isinstance(path, unicode):
                path = path.decode('utf8')
            akey = name_key(artist)
            # HONORIFIC-TOLERANT, mirroring the min(plain, without_titles)
            # shape score_author uses. name_key does not strip honorifics, so a
            # strict comparison measured `artist='Sir Arthur Conan Doyle'` ->
            # None against the folder "Arthur Conan Doyle" (the plain name
            # confirms), and this arbiter is what enables the sidecar
            # author/narrator swap correction -- so every honorific-bearing
            # ALBUMARTIST had it silently disabled. The plain comparison is
            # tried FIRST and unchanged, so no existing confirmation moves.
            akey_bare = name_key(strip_courtesy_title(artist))
            if not akey and not akey_bare:
                return None
            for seg in path.split('/'):
                seg = seg.strip()
                if not seg:
                    continue
                if akey and name_key(seg) == akey:
                    return artist
                if akey_bare and name_key(strip_courtesy_title(seg)) == akey_bare:
                    return artist
        except Exception as e:
            log.error('incipit folder_author_confirmed failed: %s', e)
        return None

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

        # Album duration (ms) = sum of the track part durations -- but ONLY when
        # EVERY part reports a real one. In the legacy album media object `tracks`
        # is a dict keyed by track index, so iterate its values (iterating the dict
        # itself yields string keys). Each track exposes items -> parts, each part
        # carrying its own duration; any missing link raises and is caught, leaving
        # duration None.
        #
        # Completeness matters on a MULTI-FILE book mid-analysis: Plex returns a
        # real duration for the files it has analyzed and nothing (or its -1
        # sentinel) for the rest. Summing only the analyzed parts yields a too-SHORT
        # total, which then reads as a >5% (or >25%) runtime mismatch against the
        # correct edition -- turning the duration veto, the main wrong-edition
        # guard, ONTO the right match. A partial sum is worse than none, so if any
        # part is missing or non-positive, withhold duration entirely and fall back
        # to the safe title+author path (which cannot auto-apply on its own).
        duration = None
        try:
            tracks = self.media.tracks
            try:
                track_iter = tracks.values()
            except Exception:
                track_iter = tracks
            total = 0
            complete = True
            for track in (track_iter or []):
                for item in (track.items or []):
                    for part in (item.parts or []):
                        # Parts expose duration as a string; Plex reports -1 (or
                        # nothing) for a not-yet-analyzed file. A malformed value
                        # counts as missing, not zero, so it marks the sum partial.
                        part_ms = 0
                        try:
                            if part.duration:
                                part_ms = int(part.duration)
                        except Exception:
                            part_ms = 0
                        if part_ms > 0:
                            total += part_ms
                        else:
                            complete = False
            # Only trust the sum when no part was missing -- a partial (too-short)
            # total would wrongly veto the correct edition.
            if total and complete:
                duration = total
        except Exception as e:
            log.error('incipit duration probe failed: %s', e)
        log.debug('incipit duration resolved: %s' % str(duration))
        # Plex reports -1 for a not-yet-analyzed file; only send a real runtime.
        if duration and duration > 0:
            extra += '&duration=' + quote_param(str(duration))

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
                    # Require the B0 prefix here, unlike the filename probe.
                    # asin_regex is a SHAPE test ([A-Z\d]{10}) and a print
                    # ISBN-10 satisfies it, so an "asin" field written by an
                    # Audiobookshelf/OPF export can carry 1250771463 -- and
                    # this hint is PINNED as definitive by the API, which
                    # would match the print edition over the audio one.
                    # parse_incipit_candidates guards provider rows the same
                    # way and for the same stated reason. A missing hint only
                    # degrades to a normal title+author search, so refusing an
                    # ambiguous identifier is the cheap side of the trade.
                    if sc_match and sc_match.group(0).startswith('B0'):
                        asin_hint = sc_match.group(0)
                    elif sc_match:
                        log.info(
                            'incipit asin hint: ignoring non-Audible sidecar '
                            'identifier %s (not B0-prefixed)', sc_match.group(0)
                        )
        if asin_hint:
            extra += '&asin=' + quote_param(asin_hint)
            log.info('incipit asin hint: %s', asin_hint)

        # ISBN, sent ALONGSIDE the ASIN rather than instead of it. A sidecar
        # ASIN can be DEAD while the sidecar's ISBN is the live identifier:
        # "The Lost Stories Collection" pins B08WF9JR2P (resolves to nothing
        # anywhere) next to isbn 9780593399439 -- whose ISBN-10 form is the id
        # in the book's own audible.com URL. Publishers routinely register
        # audio editions under the print ISBN-10 rather than a B0 ASIN. The
        # API uses this ONLY as a fallback pin identity when the ASIN above is
        # absent or resolves to nothing, so sending both never displaces a
        # working ASIN. Light shape check only (10/13 significant chars); the
        # API owns the ISBN-13 -> ISBN-10 conversion and check-digit math.
        try:
            sc = self.sidecar()
            if sc:
                sc_isbn = sc.get('isbn')
                if isinstance(sc_isbn, (str, unicode)):
                    # re.sub, NOT a per-character loop: the sandbox guards
                    # iteration via _getiter_, which calls __iter__ -- and py2
                    # unicode strings have no __iter__ (legacy __getitem__
                    # protocol), so looping over this string's characters dies
                    # with "'unicode' object has no attribute '__iter__'" in
                    # Plex while passing py2, py3 AND the test harness.
                    # Measured live 2026-07-26 on every scan; guarded by
                    # test_isbn_extraction_does_not_iterate_the_string.
                    kept = re.sub(r'[^0-9Xx]', '', sc_isbn)
                    if len(kept) in (10, 13):
                        extra += '&isbn=' + quote_param(kept.upper())
                        log.info('incipit isbn hint: %s', kept.upper())
        except Exception as e:
            log.error('incipit isbn probe failed: %s', e)

        # Narrator, from the sidecar (swap-corrected). For a popular book the
        # providers return several editions with IDENTICAL title and author --
        # Harry Potter and the Chamber of Secrets comes back as Jim Dale,
        # Stephen Fry and a Full-Cast edition, all scoring the same -- and the
        # narrator is the only field that says which one is on disk. Sent as a
        # ranking signal, never a filter: the API must not discard a correct
        # book because a narrator string disagreed.
        narrators = self.sidecar_people()[1]
        if narrators:
            extra += '&narrator=' + quote_param(', '.join(narrators))
            log.info('incipit narrator hint: %s', ', '.join(narrators))

        # Series, straight from the sidecar. Local, machine-written, and it
        # beats deriving one from the folder path -- which is what produced
        # "Pocket Potters, Book 1" for a book whose sidecar plainly says
        # "Harry Potter".
        series_name, series_position = self.sidecar_series()
        if series_name:
            extra += '&series=' + quote_param(series_name)
            if series_position:
                extra += '&seriesPosition=' + quote_param(series_position)
            log.info(
                'incipit series hint: %s%s', series_name,
                (' #' + series_position) if series_position else ''
            )

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
        # Suppress the widening signal when the track title adds nothing new.
        #
        # The test used to be `track_title != normalizedName` alone, which held
        # while normalizedName came from the ALBUM tag: a junk album tag and a
        # junk track title were equal, so nothing was sent. Once the sidecar
        # began supplying the title, normalizedName became the GOOD title, the
        # two stopped matching, and the junk track title started riding along as
        # a widening signal -- on exactly the badly-tagged books the sidecar
        # exists to rescue, and reopening a leak this file has closed twice.
        # Comparing against the ALBUM TAG as well keeps a track title that
        # merely repeats the scanner's hint out of the query, whatever the
        # resolved title turned out to be.
        album_tag = self.media.album or ''
        redundant = (
            track_title == self.normalizedName
            or track_title == strip_part_index(album_tag)
        )
        if track_title and not redundant:
            extra += '&trackTitle=' + quote_param(track_title)

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
                # Prefer the row's Audible ASIN over the provider-native id as
                # the Plex match ID: the update then fetches the canonical
                # /books/{asin} record instead of re-querying the provider's
                # edition. Proven consequence of using c['id'] blindly: the
                # PINNED Dungeon Crawler Carl row was Hardcover's edition
                # 32126720 -- which maps the live ASIN to their FRENCH-subtitled
                # edition record -- so the correctly-ranked, correctly-pinned
                # pick still updated to a French album title. B0-prefixed only:
                # provider rows can carry a print ISBN in the asin field (looks
                # identical to a 10-char ASIN), and a digit-only identifier must
                # keep the provider-id path that is known to resolve.
                #
                # DO NOT "fix" this back to c['id'] for non-Audible rows. It
                # looks wrong -- the API documents `id` as the data-fetch key
                # and warns the asin may 404 -- but re-measured 2026-07-22 the
                # pinned DCC row is STILL hardcover-edition-32126720 at
                # confidence 1.0, and fetching it still yields the French
                # subtitle. Language demotion cannot save it either: Hardcover
                # reports that edition as lang "en", i.e. mislabeled at source.
                # The 404 this preference used to cause (a delisted ASIN froze
                # the item's metadata) is now handled server-side -- the API
                # rescues PRODUCT_DELISTED through the provider that carries
                # the ASIN -- so the reason to revert is gone, but the reason
                # to keep it is not.
                # isinstance, not bare truthiness: a provider row can carry a
                # print ISBN in the asin field serialized as a JSON NUMBER, and
                # a number is truthy, so `or ''` does not rescue it and
                # .startswith raises AttributeError. That lands in the
                # per-candidate except below, which drops the row entirely --
                # a candidate that matched fine before this preference existed
                # would silently vanish from the results.
                identifier = c.get('asin') or ''
                # (str, unicode) rather than basestring: the sandbox's builtin
                # blocklist is irregular and basestring has no precedent here,
                # while this exact tuple is already proven at the sidecar sites.
                if not (isinstance(identifier, (str, unicode))
                        and identifier.startswith('B0')):
                    identifier = c['id']
                search_results.append(
                    {
                        'asin': identifier + '_' + self.region_override,
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
        if is_missing_album(self.media.album) and not self.manual:
            # The sidecar/folder recovery gets first refusal. These two upstream
            # gates ran BEFORE it -- "[Unknown Album]" aborted the search
            # outright and a NULL album was overwritten with the per-track title
            # (which then looks "present" to resolve_search_title) -- so on an
            # automatic scan, the only path the recovery exists for, it could
            # never fire. Assigning the recovered title here also collapses a
            # multi-part book to ONE search URL instead of one per part.
            recovered = self.sidecar_title() or self.folder_title()
            if recovered:
                self.media.album = recovered
                return True
            # The enclosing gate already established the album is missing, so
            # re-testing `is None` here only NARROWS it -- and wrongly. When
            # this block was widened from `album is None` to is_missing_album()
            # this line kept the old predicate, so an album that is missing but
            # not literally None (an empty tag, or a bare "Unknown Album"
            # without the brackets) fell past the fallback and aborted the
            # search outright. Release matched those on the track title.
            if self.media.title:
                log.warn('Using track title since album title is missing.')
                self.media.album = self.media.title
                return True
            log.info(
                'Album title is missing/"[Unknown Album]" and nothing could be '
                'recovered on an automatic search.  Returning'
            )
            return None

        if self.is_typed_search():
            # A typed Search Options query: use the user-entered name instead of
            # the scanner hint. Same predicate as the sidecar/author/extra-args
            # gates, so "what counts as typed" has exactly one definition.
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
        query = 'name=' + quote_param(modified_artist_name)
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
        # Remove a leading courtesy title / trailing credential, through the
        # file's ONE honorific rule.
        #
        # This used to be its own list -- ['Dr.', 'EdD', 'Prof.', 'Professor']
        # -- substituted out with no word boundary and no minimum remainder, so
        # it destroyed exactly the names COURTESY_TITLES' comment cites as the
        # reason to be careful: "Dr. Seuss" left as " Seuss" (the query was
        # literally name=%20Seuss) and "Professor Elemental" as " Elemental".
        # strip_courtesy_title splits on whitespace, so the boundary is
        # structural rather than regex-shaped, and it refuses to reduce a
        # two-token credit at all.
        name = strip_courtesy_title(name)
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

        # The bracket strip above leaves whatever whitespace surrounded the
        # bracket, and this value goes straight into `name=` on the outbound
        # query. A leading space is not cosmetic there -- it is %20 in the URL.
        name = re.sub(r'\s+', ' ', name).strip()
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
        # artist matches the real author. Unconditional: it only rewrites a name
        # that CARRIES such a qualifier, and leaving one in place merely tanks
        # author similarity -- there is no library for which keeping it is better.
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
           search media carries media.filename (confirmed live). decode() is
           py2-only (the harness's unquote returns py3 str), so fall back to
           the raw value -- path comparisons work on it either way.
           ValueError, not the Unicode error names: py2 unicode.decode
           round-trips through an implicit ASCII encode, so a non-ASCII path
           raises the ENCODE error, and neither Unicode name has a sandbox
           whitelist precedent (a missing name in a lazily-evaluated except
           tuple is a NameError right when the fallback should fire). Both
           directions subclass ValueError, which is proven whitelisted."""
        try:
            if self.media.filename:
                raw = urllib.unquote(self.media.filename)
                try:
                    return raw.decode('utf8')
                except (AttributeError, ValueError):
                    return raw
        except Exception as e:
            log.error('incipit artist_path failed: %s', e)
        return None

    def artist_album_title(self):
        """The album/book title to search for: the SIDECAR title first, then
           media.album / media.name, then the file's basename.

           Sidecar first because the tag can be rip-tool junk: measured live on
           The Hand of Oberon (2026-07-26), whose album tag is
           'coa_04_The Hand of Oberon Unabridged' -- the recovery book search
           on it returns ZERO rows, while the sidecar's title plus the file
           duration answers the right book at confidence 1.0 with exactly the
           author the recovery needs. The sidecar is machine-written truth and
           already preferred everywhere else a title matters; this was the one
           consumer still reading the raw tag first."""
        try:
            sc = self.sidecar()
            if sc:
                sc_title = sc.get('title')
                if isinstance(sc_title, (str, unicode)) and sc_title.strip():
                    return sc_title.strip()
        except Exception as e:
            log.error('incipit artist_album_title sidecar read failed: %s', e)
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
        query = 'title=' + quote_param(title)
        duration = self.artist_duration()
        if duration:
            query += '&duration=' + quote_param(str(duration))
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


# THE honorific list for this file -- one list, used by every surface that has
# to see past a courtesy title. There used to be two, and they disagreed:
# ArtistSearchTool.cleanup_author_name carried its own ['Dr.', 'EdD', 'Prof.',
# 'Professor'] and substituted it out with NO word boundary and no minimum
# remainder, so the outbound artist query for "Dr. Seuss" was literally
# `name=%20Seuss` and "Professor Elemental" became " Elemental" -- while this
# list's own comment, 260 lines below it, said '"Dr." is excluded because Dr.
# Seuss exists'.
#
# The protection lives in the STRIPPER, not in the list: strip_courtesy_title
# requires at least two tokens to REMAIN, so a two-token credit ("Dr. Seuss",
# "Lord Dunsany", "Professor Elemental", "Mr. Men") is untouchable by any entry
# here. That is what makes it safe to carry the academic titles the old list
# needed. Still deliberately short -- and score_author compares these away on
# BOTH sides, so an entry can only ever remove a penalty, never make two
# different authors match.
COURTESY_TITLES = (
    'sir', 'dame', 'lord', 'lady', 'rev', 'reverend', 'father', 'sister',
    'dr', 'prof', 'professor',
)

# Post-nominal credentials that FOLLOW a credited name ("Jane Doe, EdD"). A
# separate tuple because the POSITION is the rule, not the word; 'EdD' is the
# one the old cleanup_author_name list carried, and dropping it silently would
# be a behaviour change. Same minimum-remainder protection.
POST_NOMINALS = ('edd',)


def strip_courtesy_title(name):
    """
        `name` without a leading courtesy title or trailing post-nominal.

        Requires at least two tokens to REMAIN, so a one-word credit is never
        emptied, and matches on the reduced token (trailing '.'/',' and case are
        irrelevant). Returns the input unchanged when nothing applies.
    """
    try:
        parts = (name or '').strip().split()
    except Exception:
        return name
    # Three tokens minimum, so at least two remain: stripping "Sir Pratchett"
    # down to a single token is too thin to compare safely.
    if len(parts) < 3:
        return name
    changed = False
    if parts[0].lower().rstrip('.') in COURTESY_TITLES:
        parts = parts[1:]
        changed = True
    if len(parts) > 2 and parts[-1].lower().strip('.,') in POST_NOMINALS:
        parts = parts[:-1]
        changed = True
    if not changed:
        return name
    # A credential is usually offset by a comma ("Jane Doe, EdD"); removing the
    # word must not leave the comma dangling on the surname.
    return ' '.join(parts).rstrip(' ,')


# ---------------------------------------------------------------------------
# NFO sidecars
#
# The ripper's own description of the file, produced by the SOURCE rather than
# by the importer. Where it disagrees with a Chaptarr-written metadata.json the
# nfo is the better witness: the json records what the importer MATCHED, and a
# sidecar can be self-consistently wrong for a whole edition in a way no field
# cross-check catches.
#
# Layout, measured across 259 real files (186 use it): three "Header\n====="
# sections -- General Information, Media Information, Book Description -- with
# aligned "Key:   value" pairs. Only the FIRST section describes the book;
# Media Information is encoder trivia and the description is prose full of
# colons, so both are skipped rather than parsed.
# ---------------------------------------------------------------------------

# A genre that says nothing. The field is present in 173 files and is often
# literally the medium.
NFO_JUNK_GENRES = ('audiobook', 'audiobooks', 'unknown', 'general')

# Splitting a credited-name field: the file's CANONICAL co-author separators
# (MULTI_AUTHOR_PATTERN), except never a separator that sits inside a
# parenthetical. The `\([^()]*\)` alternative comes FIRST, so re's
# leftmost-first alternation swallows a whole role note before its comma can be
# read as a separator.
#
# The old `value.split(',')` split before the role note was considered, and a
# role note routinely carries a comma. Measured with the shipped function:
#   "Arthur Conan Doyle, Stephen Fry (introductions, notes)"
#     -> ['Arthur Conan Doyle', 'Stephen Fry (introductions', 'notes)']
#   "Terry Pratchett, Neil Gaiman (foreword, 2006)"
#     -> ['Terry Pratchett', 'Neil Gaiman (foreword', '2006)']
# Two bogus names per field, one of them with an unbalanced paren, both handed
# straight to score_author -- the exact skew nfo_people exists to prevent.
NFO_CREDIT_SPLIT_RE = re.compile(
    r'(\([^()]*\))|(?:' + MULTI_AUTHOR_PATTERN + r')', re.IGNORECASE)

NFO_SECTION = re.compile(r'^([A-Za-z][A-Za-z ]{2,40})\s*\n=+\s*$', re.M)
NFO_FIELD = re.compile(r'^\s{0,4}([A-Za-z][A-Za-z0-9 ().\-/]{1,30}):\s{2,}(.+?)\s*$')
NFO_MARKETPLACE = re.compile(r'^Audible\.((?:co\.)?[a-z.]+)\s+Release$', re.I)


def nfo_duration_ms(value):
    """Milliseconds from "9 hours, 53 minutes, 19 seconds", or None."""
    if not value:
        return None
    hours = re.search(r'(\d+)\s*hour', value, re.I)
    minutes = re.search(r'(\d+)\s*min', value, re.I)
    seconds = re.search(r'(\d+)\s*sec', value, re.I)
    if not (hours or minutes):
        return None
    total = 0
    if hours:
        total += int(hours.group(1)) * 3600
    if minutes:
        total += int(minutes.group(1)) * 60
    if seconds:
        total += int(seconds.group(1))
    return total * 1000 if total else None


def split_credits(value):
    """
        A credited-name field split into entries on the canonical co-author
        separators, with parentheticals held together (NFO_CREDIT_SPLIT_RE).
    """
    text = value or ''
    parts = []
    cut = 0
    for found in NFO_CREDIT_SPLIT_RE.finditer(text):
        # group(1) is a parenthetical: it belongs to the entry being built, so
        # it is NOT a cut point.
        if found.group(1):
            continue
        parts.append(text[cut:found.start()])
        cut = found.end()
    parts.append(text[cut:])
    return [part.strip() for part in parts if part.strip()]


def nfo_people(value):
    """
        Split a credited-name field into names, DROPPING any entry that carries
        a parenthetical role note.

        "Arthur Conan Doyle, Stephen Fry (introductions)" yields Doyle alone.
        Fry is a real contributor but not an author of the work, and sending
        him as one skews the author score against the correct match -- the
        exact failure the courtesy-title fix (v1.3.175) addressed from the
        other direction. Stripping only the parentheses would keep the name and
        cause precisely that.

        Both halves of the rule are the file's OWN, not a second spelling: the
        split is MULTI_AUTHOR_PATTERN (so "Terry Pratchett & Neil Gaiman" is two
        people, not one joined name) made paren-aware, and "does this entry
        carry a trailing parenthetical" is clear_series_text's question -- if
        that rule finds something to strip, this entry is a role credit and the
        whole entry goes.
    """
    out = []
    for part in split_credits(value):
        if clear_series_text(part) != part:
            continue
        if part.lower() in ('and',):
            continue
        out.append(part)
    return out


# A trailing part number on a multi-file rip: "... Book 13 - 001.mp3".
NFO_PART_SUFFIX = re.compile(r'\s*[-_]\s*\d{1,3}\s*$')


def nfo_candidate_paths(media_path):
    """
        Paths where this book's .nfo plausibly lives, best guess first.

        DERIVED, not listed. Core.storage exposes load/save; reaching for a
        directory-listing API this plugin has never used means an unavailable
        one fails at call time, and the sandbox is unforgiving. Measured across
        193 real folders holding both an nfo and audio: 165 (85%) name the nfo
        exactly like the audio file, 26 are multi-file rips whose audio is the
        nfo name plus a part number, and 2 use the folder name.

        Kept short deliberately -- every miss is a real SMB round-trip, and this
        runs per search.
    """
    if not media_path:
        return []
    # UNQUOTE + DECODE, exactly as sidecar() does to the same value. Plex hands
    # media.filename over percent-encoded, so without this every book whose path
    # contains a space (%20) or a non-ASCII character probed a path that cannot
    # exist -- i.e. the feature would miss most of the library the moment it is
    # wired up. The isinstance guard is folder_author_confirmed's: py2 unquote
    # returns bytes needing the decode, a py3 str is already text.
    try:
        media_path = urllib.unquote(media_path)
        if not isinstance(media_path, unicode):
            media_path = media_path.decode('utf8')
    except Exception as e:
        log.error('incipit nfo: could not decode the media path (%s)', e)
        return []
    if '/' not in media_path:
        return []
    folder, _, filename = media_path.rpartition('/')
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
    names = [stem]
    trimmed = NFO_PART_SUFFIX.sub('', stem)
    if trimmed and trimmed != stem:
        names.append(trimmed)
    leaf = folder.rsplit('/', 1)[-1]
    if leaf and leaf not in names:
        names.append(leaf)
    out = []
    for name in names:
        candidate = folder + '/' + name + '.nfo'
        if candidate not in out:
            out.append(candidate)
    return out


def nfo_text(raw):
    """
        The nfo payload as UNICODE, or None.

        parse_nfo promises "the SAME keys as the metadata.json sidecar, so
        every existing consumer reads it unchanged" -- but sidecar() builds its
        values out of json.loads, which yields UNICODE, while an nfo arrives
        from Core.storage.load as a py2 byte str. Hand a consumer bytes and the
        first non-ASCII credit ("Antoine de Saint-Exupery" with its accent)
        blows up the moment score_album does title.encode('utf-8'). Decoding
        ONCE, at the door, makes every value below unicode by construction.

        THE PY3 TEST HARNESS CANNOT SEE THIS. There `str` IS text and plexenv
        aliases `unicode` to `str`, so the broken and the fixed version are
        indistinguishable; only a live py2 load tells them apart. What the
        harness CAN prove is that a unicode payload survives intact and that
        non-ASCII values come back equal to the source (tests/test_nfo.py says
        which is which).
    """
    if raw is None:
        return None
    if isinstance(raw, unicode):
        return raw
    try:
        return raw.decode('utf-8')
    except Exception:
        pass
    try:
        # Ripper nfos are occasionally cp1252/latin-1. A slightly wrong glyph
        # in a publisher name beats losing the file's duration entirely.
        return raw.decode('latin-1')
    except Exception:
        log.error('incipit nfo: could not decode the nfo payload')
        return None


def parse_nfo(text):
    """
        A ripper .nfo as a dict using the SAME keys as the metadata.json
        sidecar, so every existing consumer reads it unchanged, or None when
        the text is not one of these files.

        Deliberately conservative: unknown keys are dropped rather than passed
        through, because this feeds matching and a stray field is a silent
        skew, not a visible error.

        Values are UNICODE, like the sidecar's (see nfo_text).
    """
    text = nfo_text(text)
    if not text or 'Title:' not in text:
        return None
    # Only the first section describes the BOOK.
    body = text
    marks = [m.start() for m in NFO_SECTION.finditer(text)]
    if len(marks) > 1:
        body = text[:marks[1]]
    raw = {}
    for line in body.split('\n'):
        found = NFO_FIELD.match(line)
        if found:
            raw[found.group(1).strip()] = found.group(2).strip()
    if 'Title' not in raw:
        return None

    out = {}
    out['title'] = raw['Title']
    authors = nfo_people(raw.get('Author'))
    if authors:
        out['authors'] = authors
    narrators = nfo_people(raw.get('Read By'))
    if narrators:
        out['narrators'] = narrators
    if raw.get('Publisher'):
        out['publisher'] = raw['Publisher']
    duration = nfo_duration_ms(raw.get('Duration'))
    if duration:
        out['duration'] = duration
    genre = (raw.get('Genre') or '').strip()
    if genre and genre.lower() not in NFO_JUNK_GENRES:
        out['genre'] = genre
    unabridged = (raw.get('Unabridged') or '').strip().lower()
    if unabridged in ('yes', 'true'):
        out['abridged'] = False
    elif unabridged in ('no', 'false'):
        out['abridged'] = True
    # The marketplace line names the STORE the rip came from, which is a far
    # better region signal than guessing from a bracketed path segment.
    for key in raw:
        market = NFO_MARKETPLACE.match(key.strip())
        if market:
            tld = market.group(1).lower().rstrip('.')
            region = 'us' if tld == 'com' else tld.split('.')[-1]
            if region in available_regions:
                out['region'] = region
            break
    return out


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
            # Stash the api's alternate covers while the whole candidate set is
            # visible. They are built by dedupe from the merged editions, so the
            # per-id item lookup that follows cannot rebuild them.
            try:
                remember_alternate_covers(
                    self.asin, self.result_dict.get('coverAlternates'))
            except Exception:
                pass
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
        # Read unconditionally by the album search's display loop, exactly like
        # author/narrator/year above -- so the key must always exist. A
        # title-less API row (a dead-ASIN catalog stub) crashed the whole
        # listing with KeyError: 'title', blanking every result for the book.
        plex_score_dict['title'] = self.title or ''
        if self.title:
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

            A leading COURTESY TITLE is compared away. Found live 2026-07-31:
            the library held two artists for one man -- "Arthur Conan Doyle"
            (2 albums, photo and bio) and "Sir Arthur Conan Doyle" (1 album,
            neither) -- because Fix Match offered the right author at score 70
            and Plex's auto-apply bar is 80. The arithmetic is exact:
            "sirarthurconandoyle" vs "arthurconandoyle" is a Levenshtein
            distance of 3, times the author weight of 10, so 100 - 30 = 70.
            Three characters of courtesy title sank a correct match.

            Taking the BEST of the stripped and unstripped comparisons means
            this can only ever LOWER a deduction, never raise one: when the
            honorific is genuinely part of the credited name and the provider
            carries it too, the unstripped comparison already scores 0 and
            wins. Two different authors still differ by their actual names, so
            the relaxation cannot make unrelated people match.
        """
        if self.helper.media.artist:
            scorebase3 = self.helper.media.artist
            scorebase4 = author
            plain = self.calculate_score(
                self.reduce_string(scorebase3),
                self.reduce_string(scorebase4)
            )
            without_titles = self.calculate_score(
                self.reduce_string(strip_courtesy_title(scorebase3)),
                self.reduce_string(strip_courtesy_title(scorebase4))
            )
            author_score = min(plain, without_titles) * 10
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
