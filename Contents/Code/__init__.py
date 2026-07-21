# Incipit (fork of Audnexus Agent)
# coding: utf-8
import hashlib
import json
import re
import urllib
# Import internal tools
from _version import version
from logging import Logging
from search_tools import AlbumSearchTool, ArtistSearchTool, ScoreTool
from time import sleep, time
from update_tools import AlbumUpdateTool, ArtistUpdateTool

VERSION_NO = version

# Score required to short-circuit matching and stop searching.
GOOD_SCORE = 98

# Setup logger
log = Logging()


def author_pref_key(value):
    """
        Normalize an author name for `authors_prefer_hardcover` matching:
        case-, whitespace-, punctuation- AND diacritic-insensitive, so
        "J. R. R. Tolkien"/"j r r tolkien" and "José Saramago"/"Jose Saramago"
        each resolve to one key. The old [^a-z0-9] strip DELETED accented
        letters ('José' -> 'jos' vs 'Jose' -> 'jose' -- keys never matched, the
        pin silently never fired for any non-ASCII author) and collapsed fully
        non-Latin names to ''. StripDiacritics folds instead of deleting, and
        the \\W strip keeps unicode word characters (CJK/Cyrillic names work).
    """
    if not value:
        return ''
    try:
        folded = String.StripDiacritics(value)
        if folded:
            value = folded
    except Exception:
        pass
    return re.sub(r'[\W_]+', '', value.lower(), flags=re.UNICODE)


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


# Plex calls update() once PER TRACK, so on a force refresh of a multi-part
# book the API-backed poster work (backup, local-select, author-art) would
# repeat its full HTTP round-trips for every track -- a 27-part book means 27x.
# The container path has its own in-container guard; these calls need one too.
# Keyed by (tag, guid) with the work's INPUT as the token (e.g. the cover's
# sha1), so CHANGED input always re-runs and only true repeats are skipped. The
# TTL bounds staleness: entries expire in seconds-to-minutes, so a deliberate
# later re-Refresh does the work again while one pass's track-fanout collapses.
recent_work_memo = {}


def should_run(tag, guid, token, ttl):
    """True unless the same (tag, guid, token) completed within ttl seconds."""
    entry = recent_work_memo.get((tag, guid))
    if entry and entry[0] == token and (time() - entry[1]) < ttl:
        return False
    return True


def mark_done(tag, guid, token):
    recent_work_memo[(tag, guid)] = (token, time())


def local_cover_bytes(helper):
    """
        Raw bytes of the book folder's cover.jpg, or None.

        BYTES specifically, because Proxy.Media(bytes) IS accepted by the posters
        container while Proxy.LocalFile is rejected (proven 1.3.26/1.3.27).
        Core.storage.load is the reader that works under PlexPluginCodePolicy=
        Elevated (open() stays blocked even then, so we don't attempt it). Every
        failure is caught, so a missing cover.jpg or a sealed sandbox simply
        returns None and the caller falls back to the online cover.
    """
    try:
        raw = helper.album_file_path()
        if not raw:
            return None
        path = urllib.unquote(raw).decode('utf8') if '%' in raw else raw
        if '/' not in path:
            return None
        candidate = path.rsplit('/', 1)[0] + '/cover.jpg'
    except Exception as e:
        log.error('incipit cover: path resolve failed (%s)', e)
        return None
    try:
        data = Core.storage.load(candidate)
        if data:
            log.info('incipit cover: read %s (%s bytes)', candidate, len(data))
            return data
    except Exception as e:
        log.warn('incipit cover: Core.storage read failed (%s)', e)
    return None


def backup_selected_poster(helper):
    """
        Back up the currently-selected Plex poster to cover.jpg next to the book,
        writing only when it differs -- so a cover you set manually in Plex
        persists to disk and survives a library rebuild (the fresh scan then
        re-serves it via prefer_local_cover).

        Mechanism (the Lambda.bundle pattern, every step verified live under the
        Elevated code policy): resolve this item through Plex's own HTTP API
        (reachable, and the plugin's request is trusted -- no token needed), read
        its selected `thumb`, download those bytes, and Core.storage.save to
        cover.jpg. Byte-compare is a safe change-detector: /thumb serves the
        ORIGINAL bytes (verified identical to cover.jpg on an unchanged book).
    """
    PMS = 'http://127.0.0.1:32400'
    # Per-track collapse (see recent_work_memo): the input here is Plex's current
    # selection, unknowable without the HTTP round-trips this guard exists to
    # avoid -- so the token is fixed and the short TTL alone bounds the work to
    # once per pass. Marked optimistically: a failed backup simply retries on
    # the next refresh rather than on the next track.
    if not should_run('poster-backup', helper.metadata.guid, '', 60):
        return
    mark_done('poster-backup', helper.metadata.guid, '')
    # Where cover.jpg lives for this book.
    try:
        raw = helper.album_file_path()
        if not raw:
            return
        path = urllib.unquote(raw).decode('utf8') if '%' in raw else raw
        if '/' not in path:
            return
        cover_path = path.rsplit('/', 1)[0] + '/cover.jpg'
    except Exception as e:
        log.error('incipit poster-backup: path resolve failed (%s)', e)
        return
    # Read the on-disk cover first for the unchanged-skip below. Overwriting an
    # existing cover.jpg is safe again: this runs AFTER select_local_cover in
    # compile_metadata, so by now either the dropped cover IS the selection
    # (bytes identical -> skip) or a user's custom pick survived the
    # ownership-guarded select and deserves capturing. The 1.3.57 never-replace
    # rule fixed the clobber but silently stopped persisting hand-picks.
    try:
        existing = Core.storage.load(cover_path)
    except Exception:
        existing = None
    # The currently-selected poster, via the Plex API (guid -> thumb -> bytes).
    try:
        url = PMS + '/library/all?guid=' + urllib.quote(helper.metadata.guid)
        text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
        m = re.search(r'thumb="([^"]*)"', text)
        if not m:
            log.warn('incipit poster-backup: no thumb in API response (first 200: %s)', text[:200]); return
        thumb = m.group(1)
        turl = thumb if thumb.startswith('http') else PMS + thumb
        selected = HTTP.Request(turl, timeout=8, cacheTime=0).content
    except Exception as e:
        log.error('incipit poster-backup: could not read selected poster (%s)', e)
        return
    if not selected:
        return
    # Change detection: skip when the on-disk cover.jpg already matches.
    if existing and len(existing) == len(selected) and existing == selected:
        log.info('incipit poster-backup: unchanged, skip'); return
    # Write via the framework's Core.storage.save (open() is blocked in this
    # sandbox even under Elevated -- verified). CAVEAT: Core.storage.save writes a
    # "._<name>" atomic temp, which vfs_fruit on an SMB share intercepts as an
    # AppleDouble resource fork -> ENOENT. So this works on a LOCAL library but
    # FAILS on a fruit-enabled SMB media share (both verified live). For that
    # split topology the write must be done server-side by a companion script on
    # the box that holds the media.
    try:
        Core.storage.save(cover_path, selected)
        log.warn('incipit poster-backup: saved -> %s (%s bytes)', cover_path, len(selected))
    except Exception as e:
        log.error('incipit poster-backup: save FAILED %s (%s)', cover_path, e)


PMS = 'http://127.0.0.1:32400'

# Deterministic suffix for the padded re-upload trick (see
# upload_and_select_poster). Decoders ignore trailing bytes after a JPEG EOI /
# PNG IEND, so original + suffix renders identically but is NEW content to
# Plex's content-addressed store -- and POSTing NEW content both uploads and
# selects, the agent's only re-select lever (its PUT is downgraded to GET).
# DETERMINISTIC on purpose: sha(padded) is then predictable, so later passes
# recognize the padded upload as ours/selected instead of padding again and
# accumulating a new upload per refresh. One pad level = exactly one extra
# re-select per image; a further flip hits the old boundary and logs.
RESELECT_PAD = '\nincipit-reselect-v1'


def padded_variants(image_bytes):
    """(sha_original, sha_padded, padded_bytes) for ownership/skip checks."""
    sha = hashlib.sha1(image_bytes).hexdigest()
    padded = image_bytes + RESELECT_PAD
    return sha, hashlib.sha1(padded).hexdigest(), padded


def fetch_url_bytes(url):
    """Bytes of `url` via make_request (lazy HTTPRequest -> .content), or None."""
    if not url:
        return None
    try:
        response = make_request(url)
        return response.content if response else None
    except Exception as e:
        log.error('incipit fetch_url_bytes failed for %s (%s)', url, e)
        return None


def read_poster_state(guid, tag):
    """
        (ratingKey, selected_key, all_poster_keys) for the item with `guid`, via
        the trusted local Plex API -- the CHEAP pre-flight every selection path
        runs BEFORE any image download, so foreign selections cost two localhost
        round-trips instead of CDN fetches. None on any failure (fresh-scan item
        with no ratingKey yet, sealed sandbox), which callers treat as transient.
    """
    try:
        url = PMS + '/library/all?guid=' + urllib.quote(guid)
        text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
        m = re.search(r'ratingKey="([0-9]+)"', text)
        if not m:
            log.info('%s: no ratingKey for this item yet (fresh scan?)', tag)
            return None
        rk = m.group(1)
        purl = PMS + '/library/metadata/' + rk + '/posters'
        data = json.loads(HTTP.Request(
            purl, headers={'Accept': 'application/json'}, timeout=8, cacheTime=0
        ).content)
        selected_key = None
        keys = []
        for p in (data.get('MediaContainer', {}).get('Metadata', []) or []):
            pk = p.get('ratingKey', '') or ''
            keys.append(pk)
            if p.get('selected'):
                selected_key = pk
        return (rk, selected_key, keys)
    except Exception as e:
        log.error('%s: poster state read failed (%s)', tag, e)
        return None


def selection_is_agent_owned(selected_key, owned_shas):
    """
        Whether the CURRENT selection is the agent's to change.

        THE ownership rule (replaces the old byte-sha-only guard, which was
        blind to container selections -- proven live: a fresh-scan pin's
        metadata:// key hash is NOT the image's byte sha, so unpin never
        recognized it):
        - no selection yet                          -> ours (nothing to preserve)
        - metadata://...com.plexapp.agents.incipit -> ours (agent-supplied
          poster, whether the container defaulted to it or a user clicked it --
          the two are indistinguishable, so the pref owns the choice BETWEEN
          agent images)
        - upload:// containing one of owned_shas    -> ours (we uploaded it)
        - anything else (a user's custom upload, another agent's poster)
                                                    -> theirs, never touched
    """
    if not selected_key:
        return True
    if 'com.plexapp.agents.incipit' in selected_key:
        return True
    for sha in (owned_shas or []):
        if sha and sha in selected_key:
            return True
    return False


def upload_and_select_poster(guid, image_bytes, tag, token=None, state=None):
    """
        Make `image_bytes` the SELECTED Plex poster for the item with `guid`,
        via the trusted local Plex API (Elevated policy -> no token needed).

        WHY this exists: the posters CONTAINER (Proxy.Media + sort_order=0 +
        validate_keys) only wins on a FRESH scan -- it cannot move Plex's
        PERSISTED selection. An upload/select through the API DOES override the
        persisted pick, and it writes into Plex's OWN metadata store (NOT the
        media folder), so the SMB vfs_fruit veto does not apply.

        Live findings that shape the logic: POST /posters selects only NEW
        content; re-POSTing an existing upload is a no-op; the agent's PUT is
        downgraded to GET. When our bytes already exist as a DE-selected
        upload, re-POST with the deterministic RESELECT_PAD suffix -- new
        content to the store, identical pixels -- which re-selects. (The pad
        trick is the one unverified-by-live-test lever here; its WARN line
        makes the first real occurrence auditable.)

        `token` keys the per-track memo (callers pass a cheap identity like the
        image URL or the cover sha); `state` is an optional precomputed
        read_poster_state result so pre-flighted callers don't re-fetch it.
        Returns True when the image ends up selected. OWNERSHIP is the
        caller's job (selection_is_agent_owned) -- this function converges.
    """
    if not image_bytes:
        return False
    try:
        sha, sha_padded, padded_bytes = padded_variants(image_bytes)
    except Exception as e:
        log.error('%s: sha1 failed (%s)', tag, e)
        return False
    memo_token = token or sha
    # Per-track collapse (see recent_work_memo): this exact convergence already
    # completed seconds ago -- we are on track N of the same refresh pass.
    if not should_run(tag, guid, memo_token, 90):
        return True
    if state is None:
        state = read_poster_state(guid, tag)
    if state is None:
        return False
    rk, selected_key, keys = state
    if selected_key and (sha in selected_key or sha_padded in selected_key):
        log.info('%s: already the selected poster, skip', tag)
        mark_done(tag, guid, memo_token)
        return True
    have_plain = any(sha in k for k in keys)
    have_padded = any(sha_padded in k for k in keys)
    if have_plain and have_padded:
        # Both variants exist and neither is selected: the one-extra-level pad
        # budget is spent. Stable state -- mark it so the pass collapses.
        log.warn(
            '%s: image and its padded variant both exist de-selected on rk %s; '
            'out of in-agent re-select levers -- pick it in the UI, or use '
            'select_cover_poster.py', tag, rk
        )
        mark_done(tag, guid, memo_token)
        return False
    post_bytes = padded_bytes if have_plain else image_bytes
    content_type = 'image/png' if image_bytes[:4] == '\x89PNG' else 'image/jpeg'
    try:
        up = PMS + '/library/metadata/' + rk + '/posters'
        HTTP.Request(up, data=post_bytes,
                     headers={'Content-Type': content_type}, timeout=8)
        log.warn('%s: uploaded + selected (rk %s, %s bytes, %s%s)',
                 tag, rk, len(post_bytes), content_type,
                 ', PADDED re-select' if have_plain else '')
        mark_done(tag, guid, memo_token)
        return True
    except Exception as e:
        log.error('%s: upload failed (%s)', tag, e)
        return False


def converge_author_art(helper, target_url, other_url, tag):
    """
        Make the image at `target_url` the selected poster for this author,
        respecting ownership -- the shared engine behind pin (target=Hardcover)
        and unpin (target=Audible), so the two directions cannot drift.

        Order matters for cost: the cheap localhost poster-state read runs
        FIRST, and a foreign (user-upload) selection bails before ANY image
        download -- the old unpin fetched two CDN images per author per track
        just to conclude "not ours". Image bytes are fetched only when the
        selection is agent-owned; `other_url` is fetched only when judging an
        upload:// selection, where its sha is needed to recognize our own
        earlier upload of the other direction.
    """
    if not target_url:
        return
    guid = helper.metadata.guid
    # Running in ONE direction invalidates the OPPOSITE direction's memo entry:
    # its cached "done" describes a selection this direction is about to (or
    # just did) change. Observed live within the TTL: pin at 16:47 marked the
    # select memo; unpin at 16:50 flipped the poster to Audible; re-pin at
    # 16:51 hit the four-minute-old select entry and silently skipped.
    opposite = {
        'incipit author-art-select': 'incipit author-art-unpin',
        'incipit author-art-unpin': 'incipit author-art-select',
    }.get(tag)
    # .pop(), not `del d[k]`: RestrictedPython compiles subscript-deletion
    # through a guard that may be absent (the same class of silent whole-plugin
    # death as leading-underscore names); a method call is unambiguously safe.
    if opposite:
        recent_work_memo.pop((opposite, guid), None)
    if not should_run(tag, guid, target_url, 600):
        return
    state = read_poster_state(guid, tag)
    if state is None:
        return
    rk, selected_key, keys = state
    owned_shas = []
    target_bytes = None
    if selected_key and selected_key.startswith('upload'):
        # Only an upload:// selection needs byte shas to judge ownership.
        target_bytes = fetch_url_bytes(target_url)
        for image_bytes in (target_bytes, fetch_url_bytes(other_url)):
            if image_bytes:
                s, sp, _ = padded_variants(image_bytes)
                owned_shas.extend([s, sp])
    if not selection_is_agent_owned(selected_key, owned_shas):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, target_url)
        return
    if target_bytes is None:
        target_bytes = fetch_url_bytes(target_url)
    if not target_bytes:
        return
    upload_and_select_poster(guid, target_bytes, tag, token=target_url,
                             state=state)


def select_hardcover_author_art(helper):
    """
        Pin direction: make the Hardcover portrait (`thumb`) the selection for
        an author on the `authors_prefer_hardcover` pref. The container only
        wins on a FRESH scan, so without this the pref did nothing for an
        already-scanned author on Refresh.
    """
    converge_author_art(
        helper, helper.thumb, helper.thumb_secondary,
        'incipit author-art-select'
    )


def offer_secondary_author_poster(helper, valid_posters):
    """
        Add the Audible `imageAlt` to the artist's poster container as a
        selectable option, and return the updated validate_keys list.

        Kept as an OPTION even for pinned authors: not wanting it selected is not
        the same as not wanting it available, and pruning it left those authors
        with a single poster and no way to switch in the UI.
    """
    if not helper.thumb_secondary or helper.thumb_secondary == helper.thumb:
        return valid_posters
    if (helper.thumb_secondary not in helper.metadata.posters or helper.force):
        secondary_data = make_request(helper.thumb_secondary)
        if secondary_data is not None:
            helper.metadata.posters[helper.thumb_secondary] = \
                Proxy.Media(secondary_data, sort_order=1)
    valid_posters.append(helper.thumb_secondary)
    return valid_posters


def unpin_hardcover_author_art(helper):
    """
        Unpin direction: the author is NOT on the pref (any more), so make the
        Audible photo (`thumb_secondary`) the selection again -- but only when
        the current selection is agent-owned, so a user's custom upload
        survives every refresh. Ownership is key-based (agent metadata:// keys
        count), which makes FRESH-SCAN pins revertable too -- the old
        byte-sha-only guard was blind to them, proven live.

        Needs only the TARGET image: an author whose record lost its Hardcover
        image can still be reverted to the Audible one.
    """
    if not helper.thumb_secondary or helper.thumb_secondary == helper.thumb:
        return
    converge_author_art(
        helper, helper.thumb_secondary, helper.thumb,
        'incipit author-art-unpin'
    )


def select_local_cover(helper):
    """
        Force the book folder's cover.jpg to become the SELECTED Plex poster on
        a Refresh of an ALREADY-scanned book (the container path only wins on a
        fresh scan). Ownership-guarded: an agent-supplied selection (or our own
        earlier upload) is overridden -- that is what prefer_local_cover means
        -- but a USER'S custom upload is left alone, so hand-picks survive and
        backup_selected_poster (which now runs AFTER this) can capture them to
        cover.jpg instead of this path clobbering them.
    """
    cover_bytes = local_cover_bytes(helper)
    if not cover_bytes:
        return
    tag = 'incipit local-select'
    guid = helper.metadata.guid
    try:
        sha, sha_padded, _ = padded_variants(cover_bytes)
    except Exception as e:
        log.error('%s: sha1 failed (%s)', tag, e)
        return
    if not should_run(tag, guid, sha, 90):
        return
    state = read_poster_state(guid, tag)
    if state is None:
        return
    rk, selected_key, keys = state
    if not selection_is_agent_owned(selected_key, [sha, sha_padded]):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, sha)
        return
    upload_and_select_poster(guid, cover_bytes, tag, token=sha, state=state)


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
        # first_offer BEFORE any add: True only on the genuine FIRST match of
        # this author (the metadata posters container persists across scans, so
        # later incremental passes see the thumb already present). The pinned
        # prune below is restricted to this case -- gating it on `not force`
        # alone made every incremental scan re-prune the Audible option that
        # the previous forced refresh had restored (options oscillated).
        first_offer = bool(
            helper.thumb and helper.thumb not in helper.metadata.posters
        )
        thumb_added = False
        if helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = make_request(helper.thumb)
                if thumb_data is not None:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=0
                    )
                    thumb_added = True
            else:
                thumb_added = True
        # Author-image selection. Two authors want the Hardcover portrait, MOST
        # want the Audible photo, and there is no reliable signal to tell them
        # apart automatically (both providers return real photos; which looks
        # better is a judgement call). So:
        #
        #  - DEFAULT: offer BOTH images -- the API's `image` (Hardcover portrait,
        #    = helper.thumb) AND the Audible `imageAlt` (= helper.thumb_secondary)
        #    -- and validate_keys([thumb, secondary]), which in practice SELECTS
        #    the secondary (Audible). This is the better photo for most authors
        #    (Brian Jacques, Octavia Butler, Margaret Atwood, Leigh Bardugo,
        #    Piers Anthony ...). DO NOT drop the secondary to force Hardcover
        #    (tried in 1.3.49): it removed those better images entirely.
        #
        #  - OVERRIDE: authors named in the `authors_prefer_hardcover` pref get
        #    ONLY the Hardcover portrait at FIRST match (validate_keys([thumb]) --
        #    a single-key prune reliably SELECTS on a fresh scan); afterwards the
        #    upload/select API owns the selection and both images stay offered.
        #
        # Match the pref against BOTH the API's author name AND the artist
        # title Plex displays: the user types what they SEE (the title), and
        # that is not always byte-identical to the API's `name`. Keys are
        # punctuation/space/case-insensitive (see author_pref_key).
        pref_raw = Prefs['authors_prefer_hardcover'] or ''
        hardcover_keys = set(
            author_pref_key(part) for part in pref_raw.split(',')
        )
        hardcover_keys.discard('')
        author_keys = set(
            author_pref_key(value)
            for value in (helper.name, helper.metadata.title)
        )
        author_keys.discard('')
        prefer_hardcover = bool(author_keys & hardcover_keys)
        # Logged so a non-firing override can be diagnosed from the plugin
        # log instead of guessed at (set logging_level=DEBUG/INFO to see it).
        log.info(
            'author-art: name=%s title=%s pref=%s -> %s',
            helper.name, helper.metadata.title, pref_raw,
            'HARDCOVER' if prefer_hardcover else 'audible-default'
        )
        if helper.thumb:
            valid_posters = [helper.thumb]
            if prefer_hardcover and first_offer and thumb_added:
                # FIRST match of a pinned author: the container is the ONLY
                # thing that can set the selection (the upload/select API has no
                # ratingKey to act on yet), and a two-key validate_keys selects
                # the SECONDARY -- so prune to the Hardcover portrait alone,
                # ONLY when the portrait actually made it into the container (a
                # failed CDN fetch must fall through to offering the Audible
                # photo, not prune to an empty set and leave NO poster at all).
                # The Audible option returns on the next pass.
                pass
            else:
                valid_posters = offer_secondary_author_poster(
                    helper, valid_posters
                )
            helper.metadata.posters.validate_keys(valid_posters)
        # On a REFRESH the container can't move Plex's persisted selection, so
        # the upload/select API owns it -- which is also why the Audible photo
        # can stay on offer above without stealing the pick. OUTSIDE the
        # `if helper.thumb:` gate: the unpin direction targets the AUDIBLE
        # image and must still run when the record has no Hardcover image left,
        # or an uploaded portrait became permanently stuck.
        if helper.force:
            if prefer_hardcover:
                select_hardcover_author_art(helper)
            else:
                # Not pinned. If it WAS pinned before, the portrait we
                # uploaded (or the container selected on a fresh scan) is
                # still selected -- undo it. No-ops unless the selection is
                # agent-owned, so a user's custom upload survives.
                unpin_hardcover_author_art(helper)

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
        # When preferring local art, add our cover only as a fallback (higher
        # sort_order = lower priority) and don't re-prioritize it to the front,
        # so a local cover.jpg (via Local Media Assets) keeps the default slot.
        # For books with no local cover, ours is still the only option -> used.
        prefer_local = Prefs['prefer_local_cover']
        primary_order = 1 if prefer_local else 0

        # LOCAL COVER (Elevated-policy attempt). Prior builds (1.3.23-1.3.27)
        # proved Proxy.LocalFile is REJECTED by the posters container and the
        # default sandbox blocks open()/Core -- so the agent couldn't read the
        # sidecar. NEW lever (Info.plist PlexPluginCodePolicy=Elevated): it may
        # unlock open()/Core.storage. And crucially Proxy.Media(BYTES) IS
        # accepted here. So if we can READ cover.jpg we serve it as our own
        # poster at sort_order=0 and prune to it -> the local cover becomes the
        # sole default even with Incipit ABOVE Local Media Assets (titles stay
        # clean). local_cover_bytes() swallows every failure, so a still-sealed
        # sandbox just yields None and we fall through to the online cover
        # exactly as before. If this works it replaces select_cover_poster.py
        # for freshly-scanned items; if it doesn't, that script stays the fix.
        #
        # Deliberately OUTSIDE any `if helper.thumb:` gate: a record with no
        # online image (Hardcover/OpenLibrary book-level matches have none) used
        # to skip this whole path, leaving a prefer_local book with a perfectly
        # readable cover.jpg and NO poster at all on a normal incremental scan.
        # The local cover does not depend on the online one existing.
        local_set = False
        if prefer_local:
            local_key = 'incipit-local-cover'
            # Per-track guard: Plex calls update() once PER TRACK, so a
            # multi-part book would re-read the (up to ~1MB) cover.jpg on every
            # track. Skip the re-read once our poster is already in this pass's
            # container -- UNLESS force, so a real "Refresh Metadata" (force=1)
            # always re-reads and picks up a NEWLY dropped/replaced cover.jpg.
            if local_key in helper.metadata.posters and not helper.force:
                local_set = True
            else:
                cover_bytes = local_cover_bytes(helper)
                if cover_bytes:
                    try:
                        helper.metadata.posters[local_key] = Proxy.Media(
                            cover_bytes, sort_order=0
                        )
                        helper.metadata.posters.validate_keys([local_key])
                        log.warn('incipit cover: LOCAL cover set as the default poster')
                        local_set = True
                    except Exception as e:
                        log.error('incipit cover: Proxy.Media(local) failed (%s)', e)

        if not local_set and helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = make_request(helper.thumb)
                if thumb_data is not None:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=primary_order
                    )
            # Prune to our single primary so a stale earlier poster can't stay
            # the default -- but NOT when preferring local, so a not-yet-readable
            # local cover keeps its pickable slot. (validate_keys only touches
            # our metadata:// posters; a user's upload:// pick is never evicted.)
            if (
                not prefer_local
                and helper.thumb in helper.metadata.posters
            ):
                helper.metadata.posters.validate_keys([helper.thumb])

        # Local cover, force-select via the trusted Plex API so a dropped/replaced
        # cover.jpg takes effect on Refresh Metadata even on an ALREADY-scanned
        # book -- the posters-container path above only wins on a fresh scan.
        # SMB-safe (writes to Plex's metadata store, not the media folder).
        if Prefs['prefer_local_cover'] and helper.force:
            select_local_cover(helper)
        # Back up the currently-selected poster to cover.jpg (opt-in). Runs
        # AFTER the select: select_local_cover is ownership-guarded (a user's
        # custom upload survives it), so what is selected HERE is the state
        # worth persisting -- a new cover.jpg just got selected (backup sees
        # identical bytes and skips), or a user hand-pick survived (backup
        # captures it to cover.jpg, so it survives a library rebuild). The old
        # backup-first order clobbered a freshly-dropped cover.jpg with the
        # previous selection before prefer_local could ever serve it.
        if Prefs['backup_poster_to_cover'] and helper.force:
            backup_selected_poster(helper)
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
