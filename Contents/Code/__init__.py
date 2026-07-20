# Incipit (fork of Audnexus Agent)
# coding: utf-8
import json
import urllib
# Import internal tools
from _version import version
from logging import Logging
from search_tools import AlbumSearchTool, ArtistSearchTool, ScoreTool
from time import sleep
from update_tools import AlbumUpdateTool, ArtistUpdateTool

VERSION_NO = version

# Score required to short-circuit matching and stop searching.
GOOD_SCORE = 98

# Setup logger
log = Logging()


def apply_http_cache_time():
    # API responses are cached for a week by default to spare incipit-api. That
    # also means an API improvement stays invisible to already-scanned items for
    # up to a week. The dev toggle disables caching so a metadata refresh always
    # re-fetches — and clears the existing cache immediately so stale entries
    # (like a pre-fix author image) don't keep replaying.
    if Prefs['dev_disable_http_cache']:
        HTTP.ClearCache()
        HTTP.CacheTime = 0
        log.info('HTTP response caching DISABLED (development mode)')
    else:
        HTTP.CacheTime = CACHE_1WEEK


def search_cache_time():
    # Search responses cache for an hour, unlike ASIN data lookups (a week).
    # Plex fires the SAME album search once per track during a scan — a
    # multi-part book means dozens of identical searches, and with no caching
    # each one is a full network round-trip (~1s), which is what made large
    # initial scans crawl. An hour makes every repeat free within a scan while
    # still surfacing API-side matching improvements the same day. The dev
    # toggle keeps forcing fully fresh searches.
    if Prefs['dev_disable_http_cache']:
        return 0
    return CACHE_1HOUR


def ValidatePrefs():
    log.debug('ValidatePrefs function call')
    # Re-apply on save so flipping the dev toggle takes effect without a restart.
    apply_http_cache_time()


def Start():
    HTTP.ClearCache()
    apply_http_cache_time()
    HTTP.Headers['User-agent'] = (
        'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.2; Trident/4.0;'
        'SLCC2; .NET CLR 2.0.50727; .NET CLR 3.5.30729; .NET CLR 3.0.30729;'
        'Media Center PC 6.0'
    )
    HTTP.Headers['Accept-Encoding'] = 'gzip'
    log.separator(
        msg=(
            "Incipit Audiobooks Agent v" + VERSION_NO
        ),
        log_level="info"
    )


def probe_local_cover(helper):
    """
        DIAGNOSTIC (1.3.23): can this sandbox READ a cover.jpg sitting next to the
        audio file?

        It matters because prefer_local_cover currently cannot work: the local
        cover is offered by Local Media Assets but ours stays selected, i.e. our
        position in the agent chain beats sort_order. If we can read the file
        ourselves we can serve it as OUR poster, and chain order stops deciding
        which cover wins -- letting Incipit stay above LMA (correct titles) while
        still showing the curated local art. Log-only; changes nothing.
    """
    try:
        raw = helper.album_file_path()
    except Exception as e:
        log.error('incipit cover probe: no file path (%s)', e)
        return
    if not raw:
        log.warn('incipit cover probe: no file path available')
        return
    # SEARCH media gives a url-encoded str; UPDATE media a real path.
    path = raw
    try:
        if '%' in raw:
            path = urllib.unquote(raw).decode('utf8')
    except Exception:
        path = raw
    folder = path.rsplit('/', 1)[0] if '/' in path else path
    log.warn('incipit cover probe: folder = %s', folder)
    for name in ('cover.jpg', 'folder.jpg', 'cover.png', 'poster.jpg'):
        candidate = folder + '/' + name
        try:
            data = Core.storage.load(candidate)
            if data:
                log.warn(
                    'incipit cover probe: READ OK %s (%s bytes)', candidate, len(data)
                )
                return
            log.warn('incipit cover probe: empty file %s', candidate)
        except Exception as e:
            log.warn('incipit cover probe: cannot read %s (%s)', candidate, e)


class AudiobookArtist(Agent.Artist):
    name = 'Incipit'
    languages = [
        Locale.Language.English,
        'de',
        'es',
        'fr',
        'it',
        'ja',
    ]
    primary_provider = True
    accepts_from = ['com.plexapp.agents.localmedia']

    prev_search_provider = 0

    def search(self, results, media, lang, manual):
        """
            Search for artist metadata.
        """
        # Instantiate search helper
        search_helper = ArtistSearchTool(
            'authors', lang, manual, media, Prefs, results)

        # Check if we can quick match based on asin
        quick_match_asin = search_helper.check_for_asin()

        if quick_match_asin:
            results.Append(
                MetadataSearchResult(
                    id=quick_match_asin,
                    lang=lang,
                    name=quick_match_asin,
                    score=100,
                    year=1969
                )
            )
            log.info(
                'Using quick match based on asin: '
                '%s' % quick_match_asin
            )
            return

        # Validate author name
        search_helper.validate_author_name()

        # Short circuit search if artist name is bad.
        if not search_helper.media.artist:
            return

        # Try each author in a multi-author tag until one matches. Handles
        # narrator-first tags and slash/"and" co-authors where the real author
        # is not listed first (e.g. "Jefferson Mays, Daniel Abraham, Ty Franck").
        candidates = (
            search_helper.author_candidates() or [search_helper.media.artist]
        )
        result = None
        for candidate in candidates:
            search_helper.media.artist = String.StripDiacritics(candidate)
            result = self.call_search_api(search_helper)
            # None = request failed: abort rather than risk matching the next
            # (wrong) author on a transient blip. [] = genuine miss: try next.
            if result is None:
                break
            if result:
                break

        # Fallback: the tagged artist name matched no author. Most often it is a
        # NARRATOR mis-tagged as the artist (e.g. "Lauren Fortgang"). Ask the book
        # API what this album is and recover its author -- but only trust an
        # author that is ALSO a folder in the file's path, so a wrong name can
        # never win. Runs ONLY on a genuine zero-result, so it can't change a
        # match that already works. Opt out via the match_artist_from_folder pref.
        # Genuine zero-result ONLY ([], not None): a None is a transport blip
        # from the loop above, and firing a second (recovery) search on a blip
        # is wasted work -- and contradicts this block's "genuine zero-result"
        # contract. `result is not None` excludes the blip; `not result` keeps [].
        if result is not None and not result and (
            search_helper.prefs['match_artist_from_folder']
        ):
            recovered_author = self.recover_author_from_book(
                search_helper, candidates
            )
            if recovered_author:
                search_helper.media.artist = String.StripDiacritics(
                    recovered_author
                )
                result = self.call_search_api(search_helper)

        # Write search result status to log
        if not result:
            log.warn(
                'No results found for query "%s"',
                ' / '.join(candidates)
            )
            return
        log.debug(
            'Found %s result(s) for query "%s"',
            len(result),
            search_helper.media.artist
        )

        info = self.process_results(search_helper, result)

        # Output the final results.
        log.separator(log_level="debug")
        log.debug('Final result:')
        for i, r in enumerate(info):
            description = r['author']

            results.Append(
                MetadataSearchResult(
                    id=r['id'],
                    lang=lang,
                    name=description,
                    score=r['score']
                )
            )

            """
                If there are more than one result,
                and this one has a score that is >= GOOD SCORE,
                then ignore the rest of the results
            """
            if not manual and len(info) > 1 and r['score'] >= GOOD_SCORE:
                log.info(
                    '            *** The score for these results are great, '
                    'so we will use them, and ignore the rest. ***'
                )
                break

    def update(self, metadata, media, lang, force):
        """
            Update artist metadata.
        """
        log.separator(
            msg=(
                "UPDATING: " + media.title + (
                    " ID: " + metadata.id
                )
            ),
            log_level="info"
        )

        # Instantiate update helper
        update_helper = ArtistUpdateTool(
            'authors', force, lang, media, metadata, Prefs)

        if not self.call_item_api(update_helper):
            return

        self.compile_metadata(update_helper)

    def recover_author_from_book(self, helper, candidates):
        """
            When a tagged artist matches no author (typically a NARRATOR
            mis-tagged as the artist), ask the book API what this album is and
            return its author -- but ONLY if that author is also a folder in the
            file's path. That double gate (real book author for this title AND
            present on disk) makes a wrong recovery impossible. Returns None when
            nothing is confirmed. The library root can't be read at search time
            (the sandbox blocks the media server's HTTP interface), so the path
            is used for confirmation rather than to derive the author directly.
        """
        book_url = helper.book_search_url()
        if not book_url:
            log.debug('artist recovery: no book search url (no title or API base)')
            return None
        try:
            request = str(make_request(book_url, cache_time=search_cache_time()))
        except Exception as err:
            log.error('artist recovery book search failed: %s', err)
            return None
        book_results = json_decode(request)
        if not book_results:
            return None
        author = helper.author_confirmed_in_path(book_results)
        if not author:
            log.info(
                'artist recovery: no book author for "%s" confirmed in the '
                'file path', ' / '.join(candidates)
            )
            return None
        if author.strip().lower() in [c.strip().lower() for c in candidates]:
            return None
        # warn-level so the recovery is visible at the default log level (WARN):
        # the tagged artist was a narrator/wrong name and the real author was
        # recovered from the book match -- a rare, actionable correction.
        log.warn(
            'No author for tagged artist "%s"; recovered "%s" from the book '
            'match (confirmed in the file path)',
            ' / '.join(candidates), author
        )
        return author

    def call_search_api(self, helper):
        """
            Builds URL then calls API, returns the JSON to helper function.
        """
        query = helper.build_search_args()
        search_url = helper.build_url(query)
        # Return None (not []) on a transport/decode FAILURE so the multi-author
        # retry loop can tell "this request errored" from "this author genuinely
        # had no results" and not fall through to the wrong author on a blip.
        try:
            request = str(make_request(search_url, cache_time=search_cache_time()))
        except Exception as err:
            log.error("Author search request failed: %s", err)
            return None
        response = json_decode(request)
        if response is None:
            return None
        # When using asin match, put it into array
        if isinstance(response, list):
            arr_to_pass = response
        else:
            arr_to_pass = [response]
        results_list = helper.parse_api_response(arr_to_pass)
        return results_list

    def process_results(self, helper, result):
        """
            Process the results from the API call.
        """
        # Walk the found items and gather extended information
        info = []

        log.separator(msg="Search results", log_level="info")
        for index, result_dict in enumerate(result):
            score_helper = ScoreTool(
                helper,
                index,
                info,
                Locale.Language.English,
                Util.LevenshteinDistance,
                result_dict,
            )
            score_helper.run_score_author()

            # Print separators for easy reading
            if index <= len(result):
                log.separator(log_level="info")

        info = sorted(info, key=lambda inf: inf['score'], reverse=True)
        return info

    def call_item_api(self, helper):
        """
            Calls the metadata API to get author details, then parses them.
            Returns True on success, False on any transport/decode failure so the
            caller can keep existing metadata instead of crashing the refresh.
        """
        update_url = helper.build_url()
        try:
            request = str(make_request(update_url))
        except Exception as err:
            log.error("Author update request failed: %s", err)
            return False
        response = json_decode(request)
        # request == 'None' means make_request already exhausted its retries
        # (transport failure) — don't fire a second ladder; only a decodable-
        # but-garbage cached body (request has content) is worth the uncached heal.
        if response is None and request != 'None':
            response = retry_uncached(update_url)
        if response is None:
            # Mirrors the album path: without this line an author whose update
            # silently no-ops leaves nothing to grep.
            log.error(
                'incipit author fetch returned no usable data for %s; '
                'keeping existing metadata', update_url
            )
            return False
        helper.parse_api_response(response)
        return True

    def compile_metadata(self, helper):
        """
            Compiles the metadata for the artist.
        """
        # Description.
        helper.set_metadata_description()
        # Tags.
        helper.set_metadata_tags()
        # Title.
        helper.set_metadata_title()
        # Sort Title.
        helper.set_metadata_sort_title()
        # Thumb.
        # Kept here because of Proxy
        if helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = make_request(helper.thumb)
                if thumb_data is not None:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=0
                    )
            # Offer the alternate (Audible) author image as a secondary option.
            valid_posters = [helper.thumb]
            if (
                helper.thumb_secondary
                and helper.thumb_secondary != helper.thumb
            ):
                if (
                    helper.thumb_secondary not in helper.metadata.posters
                    or helper.force
                ):
                    secondary_data = make_request(helper.thumb_secondary)
                    if secondary_data is not None:
                        helper.metadata.posters[helper.thumb_secondary] = \
                            Proxy.Media(secondary_data, sort_order=1)
                valid_posters.append(helper.thumb_secondary)
            # Re-prioritize so our chosen thumb (the Hardcover portrait, when we
            # have one) becomes the SELECTED poster, keeping the Audible image as
            # the pickable second. sort_order=0 alone does NOT override a poster
            # Plex already selected on a prior scan -- e.g. the Audible book-cover
            # that got picked before the Hardcover author-image fix existed -- so
            # the real photo stayed present-but-not-default (the exact Craig
            # Alanson symptom). validate_keys prunes the set to [thumb, secondary]
            # and pins thumb first, which DOES move the selection. This mirrors the
            # ALBUM path (see AudiobookAlbum below), closing an artist/album
            # mirror-drift: books re-prioritized their square cover, authors never
            # did.
            #
            # TRADE-OFF (documented so we can backtrack): validate_keys re-runs on
            # every refresh, so a HAND-PICKED author poster is overridden on the
            # next scan -- the same behavior the book path already has. It's a
            # no-op for authors with no Hardcover match (thumb is just the Audible
            # image, so pinning it front changes nothing). TO REVERT to
            # "add-but-don't-re-prioritize": delete the single validate_keys line
            # below; the posters are still offered, Plex just keeps its own
            # existing selection.
            helper.metadata.posters.validate_keys(valid_posters)

        helper.log_update_metadata()


class AudiobookAlbum(Agent.Album):
    name = 'Incipit'
    languages = [
        Locale.Language.English,
        'de',
        'es',
        'fr',
        'it',
        'ja',
    ]
    primary_provider = True
    accepts_from = ['com.plexapp.agents.localmedia']

    prev_search_provider = 0

    def search(self, results, media, lang, manual):
        """
            Search for an album.
        """
        # Instantiate search helper
        search_helper = AlbumSearchTool(
            'books', lang, manual, media, Prefs, results)

        pre_check = search_helper.pre_search_logging()
        # Purposefully terminate search if it's bad
        if not pre_check:
            log.debug("Didn't pass pre-check")
            return

        # Check if we can quick match based on asin
        quick_match_asin = search_helper.check_for_asin()
        if quick_match_asin:
            results.Append(
                MetadataSearchResult(
                    id=quick_match_asin,
                    lang=lang,
                    name=quick_match_asin,
                    score=100,
                    year=1969
                )
            )
            log.info(
                'Using quick match based on asin: '
                '%s' % quick_match_asin
            )
            return

        # # Validate author name
        search_helper.validate_author_name()

        # Call search API
        result = self.call_search_api(search_helper)

        # Write search result status to log
        if not result:
            log.warn(
                'No results found for query "%s"',
                search_helper.normalizedName
            )
            return
        log.debug(
            'Found %s result(s) for query "%s"',
            len(result),
            search_helper.normalizedName
        )

        info = self.process_results(search_helper, result)

        # Nested dict for localized separators
        # 'T_A' is the separator between title and author
        # 'A_N' is the separator between author and narrator
        separator_dict = {
            Locale.Language.English: {'T_A': 'by', 'A_N': 'w/'},
            'de': {'T_A': 'von', 'A_N': 'mit'},
            'fr': {'T_A': 'de', 'A_N': 'ac'},
            'it': {'T_A': 'di', 'A_N': 'con'}
        }
        # Fall back to English separators for languages not in the table
        # (es/ja) so an album search never crashes with a KeyError.
        local_separators = separator_dict.get(
            lang, separator_dict[Locale.Language.English])
        log.debug(
            'Using localized separators "%s" and "%s"',
            local_separators['T_A'], local_separators['A_N']
        )

        # Output the final results.
        log.separator(log_level="debug")
        log.debug('Final result:')
        for i, r in enumerate(info):
            # Truncate long titles
            # Displayable chars is ~60 (see issue #32)
            # Inlcude tolerance to only truncate if >4 chars need to be cut
            title_trunc = (r['title'][:30] + '..') if len(
                r['title']) > 36 else r['title']

            # Shorten artist
            artist_initials = search_helper.name_to_initials(r['author'])
            # Shorten narrator
            narrator_initials = search_helper.name_to_initials(r['narrator'])

            description = '\"%s\" %s %s %s %s' % (
                title_trunc,
                local_separators['T_A'],
                artist_initials,
                local_separators['A_N'],
                narrator_initials
            )
            results.Append(
                MetadataSearchResult(
                    id=r['id'],
                    lang=lang,
                    name=description,
                    score=r['score'],
                    year=r['year']
                )
            )

            """
                If there are more than one result,
                and this one has a score that is >= GOOD SCORE,
                then ignore the rest of the results
            """
            if not manual and len(info) > 1 and r['score'] >= GOOD_SCORE:
                log.info(
                    '            *** The score for these results are great, '
                    'so we will use them, and ignore the rest. ***'
                )
                break

    def update(self, metadata, media, lang, force):
        """
            Update an album.
        """
        log.separator(
            msg=(
                "UPDATING: " + media.title + (
                    " ID: " + metadata.id
                )
            ),
            log_level="info"
        )

        # Instantiate update helper
        update_helper = AlbumUpdateTool(
            'books', force, lang, media, metadata, Prefs)

        # A data fetch can legitimately fail (e.g. an Audible preorder ASIN that
        # resolves to an empty product → 404). Don't let that raise and stall the
        # whole library refresh — keep the existing metadata and move on.
        if not self.call_item_api(update_helper):
            return

        self.compile_metadata(update_helper)

    def call_search_api(self, helper):
        """
            Builds URL then calls API, returns the JSON to helper function.
        """
        query = helper.build_search_args()
        search_url = helper.build_url(query)
        try:
            request = str(make_request(search_url, cache_time=search_cache_time()))
        except Exception as err:
            log.error("Book search request failed: %s", err)
            return None
        response = json_decode(request)
        if response is None:
            return None
        results_list = helper.parse_api_response(response)
        return results_list

    def process_results(self, helper, result):
        """
            Process the results from the API call.
        """
        # Walk the found items and gather extended information
        info = []

        log.separator(msg="Search results", log_level="info")
        for index, result_dict in enumerate(result):
            date = self.getDateFromString(result_dict['date'])
            year = ''
            if date is not None:
                year = date.year

                # Make sure this isn't a pre-order listing
                if helper.check_if_preorder(date):
                    continue

            score_helper = ScoreTool(
                helper,
                index,
                info,
                Locale.Language.English,
                Util.LevenshteinDistance,
                result_dict,
                year
            )
            score_helper.run_score_book()

            # Print separators for easy reading
            if index <= len(result):
                log.separator(log_level="info")

        info = sorted(info, key=lambda inf: inf['score'], reverse=True)
        return info

    def call_item_api(self, helper):
        """
            Calls the metadata API to get book details,
            then calls helper to parse those details.
            Returns True on success, False if the fetch failed (so the caller
            can skip the update instead of crashing the refresh).
        """
        update_url = helper.build_url()
        try:
            request = str(make_request(update_url))
        except Exception as e:
            log.error(
                'incipit book fetch failed for %s; keeping existing '
                'metadata: %s', update_url, e
            )
            return False
        response = json_decode(request)
        # 'None' == make_request exhausted its retries (transport failure); skip
        # the second ladder and only heal a garbage-but-present cached body.
        if response is None and request != 'None':
            response = retry_uncached(update_url)
        if response is None:
            log.error(
                'incipit book fetch returned no usable data for %s; '
                'keeping existing metadata', update_url
            )
            return False
        helper.parse_api_response(response)

        # Set date to date object
        helper.date = self.getDateFromString(helper.date)
        return True

    def compile_metadata(self, helper):
        """
            Compiles the metadata for the book.
        """
        # Series: fill a missing series/number from the folder layout before any
        # setter reads self.series / self.title (tags, title, sort title).
        helper.derive_series_from_path()
        # Date.
        helper.set_metadata_date()
        # Tags.
        helper.set_metadata_tags()
        # Title.
        helper.set_metadata_title()
        # Sort Title.
        helper.set_metadata_sort_title()
        # Studio.
        helper.set_metadata_studio()
        # Summary.
        helper.set_metadata_summary()
        # Thumb.
        # Kept here because of Proxy
        if helper.thumb:
            # When preferring local art, add our cover only as a fallback (higher
            # sort_order = lower priority) and don't re-prioritize it to the front,
            # so a local cover.jpg (via Local Media Assets) keeps the default slot.
            # For books with no local cover, ours is still the only option -> used.
            prefer_local = Prefs['prefer_local_cover']
            primary_order = 1 if prefer_local else 0
            # DIAGNOSTIC (1.3.23), no behaviour change. Confirmed in the UI: the
            # local cover IS offered but ours stays selected, so sort_order can't
            # beat our position in the agent chain. Two unknowns decide the real
            # fix, so measure both rather than guess:
            #   1. which posters are already in the container when we run -- is
            #      LMA's contribution visible to us, and under what key? If it is,
            #      we can simply decline to contribute when a local one exists.
            #   2. whether the sandbox lets us READ the cover.jpg next to the audio
            #      file. If it does, we can serve it as OUR OWN poster and the
            #      chain order stops mattering entirely -- the clean fix.
            if prefer_local:
                try:
                    existing_posters = [k for k in helper.metadata.posters]
                    log.warn('incipit poster probe: existing keys = %s', existing_posters)
                except Exception as e:
                    log.error('incipit poster probe (keys) failed: %s', e)
                probe_local_cover(helper)
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = make_request(helper.thumb)
                if thumb_data is not None:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=primary_order
                    )
            # Contribute ONLY our (square) primary as the album poster. We used to
            # also add thumb_secondary (the original portrait cover) as an alternate,
            # but on a re-matched album Plex retained an EARLIER agent poster (a
            # portrait secondary) as the selected default, and sort_order=0 can't
            # evict an existing selection. Pruning our contribution to the single
            # primary forces that stale agent poster out, so the square becomes the
            # default even on re-matched albums.
            #  - Only prune when the square is actually present: a failed fetch must
            #    not leave us with zero posters.
            #  - validate_keys only touches OUR (metadata://) posters; a user's own
            #    manual pick lives in the upload:// namespace and is never evicted.
            #  - Skipped when preferring local art, so the local cover keeps default.
            if (
                not prefer_local
                and helper.thumb in helper.metadata.posters
            ):
                helper.metadata.posters.validate_keys([helper.thumb])
        # Rating.
        helper.set_metadata_rating()

        # Log the resulting metadata
        helper.log_update_metadata()

    def getDateFromString(self, string):
        """
            Converts a string to a date object.
        """
        try:
            return Datetime.ParseDate(string).date()
        except AttributeError:
            return None
        except ValueError:
            return None


# Common helpers

def json_decode(output):
    """
        Decodes JSON output.
    """
    try:
        return json.loads(output, encoding="utf-8")
    except (AttributeError, ValueError):
        # ValueError: malformed/empty/HTML body (e.g. an API 500 page, or the
        # "None" string when make_request returned nothing).
        return None


def is_api_host(url):
    """
        True when url targets the configured incipit-api host (our own local,
        allowlisted service) rather than a third party (Audible/audnexus in
        stock mode, or an Amazon image CDN).
    """
    base = Prefs['api_base_url']
    return bool(base and url.startswith(base.rstrip('/')))


def incipit_headers(url):
    """
        Attaches the user's own Hardcover token, but ONLY on requests to the
        configured incipit-api host — never to Audible or any other host, so the
        token can't leak to a third party.
    """
    token = Prefs['hardcover_token']
    if token and is_api_host(url):
        return {'x-hardcover-token': token}
    return {}


def retry_uncached(update_url):
    """
        One cache-bypassing retry for a decode failure, to heal a poisoned
        cached 200. Call ONLY when the first request actually returned a body
        (its str() was not 'None') — a full transport failure was already
        retried 4x inside make_request and a second ladder just doubles an
        outage's cost. Returns the decoded response or None.
    """
    try:
        return json_decode(str(make_request(update_url, cache_time=0)))
    except Exception as err:
        log.error('uncached retry failed for %s: %s', update_url, err)
        return None


def make_request(url, cache_time=None):
    """
        Makes and returns an HTTP request.
        Retries 4 times, increasing  time between each retry.
        cache_time controls the plugin HTTP cache: SEARCH calls pass
        search_cache_time() (1h, or 0 with the dev toggle) so per-track
        re-searches during a scan are free; ASIN data lookups use the default
        week-long cache, since those records are stable.
    """
    headers = incipit_headers(url)
    # sleep=0 ONLY for our own local, allowlisted API — the framework's per-fetch
    # 1s pause is the largest fixed cost of a cold scan there. Third-party hosts
    # (Audible/audnexus in stock mode, Amazon image CDNs) KEEP the pacing so an
    # unpaced cold scan can't hammer or get throttled by them.
    fetch_sleep = 0 if is_api_host(url) else 1
    sleep_time = 1
    num_retries = 4
    response = None
    for attempt in range(0, num_retries):
        try:
            response = HTTP.Request(
                url, headers=headers, cacheTime=cache_time,
                timeout=90, sleep=fetch_sleep)
            break
        except Exception as err:
            log.error(
                "Failed http request attempt #%d: %s" % (attempt + 1, url))
            log.error(err)
            # No point sleeping after the final attempt.
            if attempt < num_retries - 1:
                sleep(sleep_time)
                sleep_time *= 2
    return response
