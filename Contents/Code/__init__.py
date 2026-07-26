# Incipit (fork of Audnexus Agent)
# coding: utf-8
import hashlib
import json
import re
import struct
import urllib
# Import internal tools
from _version import version
from logging import Logging
from search_tools import AlbumSearchTool, ArtistSearchTool, ScoreTool, name_key
from time import sleep, time
from update_tools import AlbumUpdateTool, ArtistUpdateTool

VERSION_NO = version

# Score required to short-circuit matching and stop searching.
GOOD_SCORE = 98

# Setup logger
log = Logging()


def author_pref_key(value):
    """
        Normalize an author name for `authors_prefer_hardcover` matching.

        Delegates to search_tools.name_key, which is the single implementation
        for the whole bundle. This used to be a private copy of the same recipe;
        the copy drifted (a third spelling in search_tools still deleted accents
        instead of folding them) and carried a whitespace hole that turned any
        MULTI-WORD non-Latin name into an empty key. One implementation, one
        place to fix.
    """
    return name_key(value)


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


def search_cache_time(manual=False):
    # Search responses cache for an hour, unlike ASIN data lookups (a week).
    # Plex fires the SAME album search once per track during a scan — a
    # multi-part book means dozens of identical searches, and with no caching
    # each one is a full network round-trip (~1s), which is what made large
    # initial scans crawl. An hour makes every repeat free within a scan while
    # still surfacing API-side matching improvements the same day. The dev
    # toggle keeps forcing fully fresh searches.
    #
    # A MANUAL search never caches. Fix Match is a human saying "the automatic
    # answer is wrong", so the one thing it must not do is replay the automatic
    # answer -- yet the dialog's auto-fired list sends the SAME URL the scan
    # sent, so it was a guaranteed cache hit and showed the ranking from up to
    # an hour ago. Measured live on Defiance of the Fall Book 10: the dialog
    # listed a stale pre-fix body (Apple's series-less record at 76, the correct
    # ASIN-pinned Audible edition absent) while the very same URL answered 1.0 /
    # score 100 when actually fetched. Picking from that list pins the wrong
    # record, and an Apple record carries no seriesPrimary -- which is how the
    # album ended up with no sort title at all. The cache exists for a SCAN's
    # per-track fan-out; a manual search is one user-initiated request, so
    # skipping it costs a single round-trip and buys a current answer.
    if manual or Prefs['dev_disable_http_cache']:
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


# How much TALLER than wide a local cover must be before it reads as a PRINT
# scan rather than audiobook art. Plex music art is square, and every real
# audiobook cover is square-ish, so a clearly portrait cover.jpg in a book folder
# is almost always an upstream pipeline mistake (a print edition's jacket written
# alongside a wrong-edition sidecar) rather than a deliberate pick. Deliberately
# generous -- 1.15 lets a slightly-off square (a 1000x1100 rip) still count as
# intended art, and only flags an unmistakable portrait (a 1600x2400 jacket).
PORTRAIT_RATIO = 1.15


def image_dimensions(data):
    """
        (width, height) for JPEG or PNG BYTES, or None when undeterminable.

        Bytes rather than a URL: the local cover.jpg is already in memory here,
        and measure_image (update_tools) fetches a URL and handles JPEG only.
        Every failure returns None so an unreadable image simply keeps the
        existing behaviour instead of changing which poster is used.
    """
    try:
        if data[:8] == '\x89PNG\r\n\x1a\n':
            # IHDR width/height are the two big-endian longs at offset 16.
            width, height = struct.unpack('>II', data[16:24])
            return (int(width), int(height))
        if data[:2] != '\xff\xd8':
            return None
        # Walk the JPEG segments to the SOFn frame header, as measure_image does.
        #
        # STRICT walk: every step must land on a real marker. An earlier version
        # resynced with `index += 1` when it found a non-0xff byte, which scans
        # byte-by-byte into ENTROPY-CODED payload and happily accepts the first
        # 0xff 0xc0..0xcf pair it finds there as a frame header -- returning some
        # other number entirely (measured: 100x100 for a 1000x1600 image). Since
        # the ONLY consumer decides whether to override the operator's cover, a
        # confidently-wrong answer is far worse than none: give up instead, which
        # returns None -> not portrait -> the local cover keeps its precedence.
        index = 2
        total = len(data)
        while index < total - 9:
            if data[index] != '\xff':
                return None
            marker = ord(data[index + 1])
            # Padding fill bytes: a run of 0xff before the real marker byte.
            if marker == 0xff:
                index += 1
                continue
            # SOFn frames carry the dimensions; skip the non-frame markers.
            if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                height, width = struct.unpack('>HH', data[index + 5:index + 9])
                return (int(width), int(height))
            # Standalone markers carry no length payload to skip over.
            if marker in (0xd8, 0xd9) or 0xd0 <= marker <= 0xd7:
                index += 2
                continue
            # SOS (0xda) is followed by entropy-coded data, not another parseable
            # segment -- the dimensions always precede it, so reaching it means we
            # will not find a real SOFn.
            if marker == 0xda:
                return None
            seg = struct.unpack('>H', data[index + 2:index + 4])[0]
            # A length below 2 cannot be a real segment and would stall the walk.
            if seg < 2:
                return None
            index += 2 + seg
    except Exception as e:
        log.warn('incipit cover: could not measure local cover (%s)', e)
    return None


def local_cover_is_portrait(data):
    """
        True when the local cover is clearly TALLER than wide (a print jacket).

        Only a confident yes: an unmeasurable image returns False, so the local
        cover keeps its normal precedence unless we positively know it is
        portrait.
    """
    dims = image_dimensions(data)
    if not dims:
        return False
    width, height = dims
    if width <= 0 or height <= 0:
        return False
    return (float(height) / float(width)) >= PORTRAIT_RATIO


# Destination temp name for the sidecar write. Deliberately NOT "._"-prefixed:
# that namespace is exactly what vfs_fruit vetoes (see write_cover_sidecar).
COVER_TMP_SUFFIX = '.incipit-tmp'
COVER_STAGE_PREFIX = 'incipit-cover-'
# Per-INVOCATION counter for the staging name (see write_cover_sidecar). A
# one-element list rather than a bare int so the increment mutates in place and
# needs no `global` statement -- the same reason recent_work_memo is a dict.
COVER_STAGE_SEQ = [0]


def write_cover_sidecar(cover_path, image_bytes):
    """
        Write image_bytes to cover_path without ever creating a "._" file on
        the destination.

        Core.storage.save() is the sandbox's only writer (open() is blocked
        even under Elevated), and it is atomic-by-temp: it writes a sibling
        named "._<name>" then moves it into place. On an SMB share with
        vfs_fruit loaded, fruit:veto_appledouble reserves that namespace for
        AppleDouble resource forks and refuses the create, so the whole save
        died with ENOENT -- which is why hand-picked posters could not persist
        on this library, where Plex reaches the media over SMB.

        So: stage the bytes into the plugin's own DataItems folder (local,
        always writable, "._" harmless there), copy them onto the share under a
        name fruit does not claim (shutil.copy opens the destination directly
        and writes the literal filename -- no temp), then rename into place.
        The temp and the destination share a directory, so shutil.move degrades
        to os.rename: atomic, so a reader never sees a half-written cover, and
        a failure leaves an inert .incipit-tmp rather than a broken cover.jpg.

        Verified against this Framework build before shipping: copy=shutil.copy,
        rename=shutil.move, remove=os.remove, and data_item_path/save_data_item
        both exist. No os import is needed -- paths are plain string work.

        Returns True when the cover is in place.
    """
    # Unique per INVOCATION, not per destination. A per-destination name gave
    # every writer of the SAME cover.jpg one shared stage file AND one shared
    # dest_tmp, so two update() passes for one album -- two tracks, or one
    # arriving just past the 60s memo TTL while the first is still copying ~1MB
    # over SMB -- could interleave: B's save_data_item rewrites the stage under
    # A's copy, or A's finally-remove unlinks it under B. The rename is atomic,
    # so what got PUBLISHED as cover.jpg was a truncated JPEG, silently, with
    # no failure logged. Distinct names make concurrent writers independent;
    # the atomic rename still picks a single winner.
    # Plain assignment, NOT `+= 1`: RestrictedPython rejects augmented
    # assignment to a subscript at COMPILE time ("Augmented assignment of
    # object items and slices is not allowed"), which kills the whole plugin
    # silently -- Fix Match spins forever with no UI error. Same guard family
    # as del-subscript. Plain d[k] = v is fine, as recent_work_memo proves.
    COVER_STAGE_SEQ[0] = COVER_STAGE_SEQ[0] + 1
    unique = '%d-%d' % (int(time() * 1000), COVER_STAGE_SEQ[0])
    # isinstance guard: .encode on a BYTE str implicitly decodes as ascii first
    # and dies on any non-ASCII path -- the same Py2 trap as quote_param.
    key = cover_path if isinstance(cover_path, str) else cover_path.encode('utf8')
    stage_name = (COVER_STAGE_PREFIX + hashlib.sha1(key).hexdigest()[:12]
                  + '-' + unique + '.jpg')
    dest_tmp = cover_path + '.' + unique + COVER_TMP_SUFFIX
    staged = None
    try:
        Core.storage.save_data_item(stage_name, image_bytes)
        staged = Core.storage.data_item_path(stage_name)
        Core.storage.copy(staged, dest_tmp)
        Core.storage.rename(dest_tmp, cover_path)
        return True
    except Exception as e:
        log.error('incipit cover-write: FAILED %s (%s)', cover_path, e)
        # Leave nothing half-done on the share.
        try:
            Core.storage.remove(dest_tmp)
        except Exception:
            pass
        return False
    finally:
        if staged:
            try:
                Core.storage.remove(staged)
            except Exception:
                pass


def promote_picked_cover(helper):
    """
        PART 2: a deliberate pick of an OFFERED online cover becomes the local
        cover. With prefer_local_cover on, cover.jpg is the DEFAULT but the
        operator can still PICK the online square/portrait we offer -- and a
        forced Refresh should make that pick stick, by copying it INTO cover.jpg
        so the local cover IS the pick and the rest of the pipeline asserts it
        rather than fighting it.

        Narrow by construction, so it can never clobber curated work:
          - forced refresh + prefer_local_cover only;
          - fires only when the LIVE selection is byte-identical to a cover WE
            offered (helper.thumb / thumb_secondary). A custom upload dragged in
            never matches -> untouched (backup_selected_poster preserves it); a
            cover.jpg replaced on disk is not our offered cover -> untouched, so
            the disk still wins;
          - only when the pick DIFFERS from the current cover.jpg (steady state
            does nothing);
          - never the artist photo (the same poison check the backup uses).
    """
    if not (Prefs['prefer_local_cover'] and helper.force):
        return
    offered = []
    for candidate_url in (helper.thumb, helper.thumb_secondary):
        if candidate_url:
            offered.append(candidate_url)
    if not offered:
        return
    tag = 'incipit promote-pick'
    # cover.jpg path (same resolution as the backup below).
    try:
        raw = helper.album_file_path()
        if not raw:
            return
        path = urllib.unquote(raw).decode('utf8') if '%' in raw else raw
        if '/' not in path:
            return
        cover_path = path.rsplit('/', 1)[0] + '/cover.jpg'
    except Exception as e:
        log.error('%s: path resolve failed (%s)', tag, e)
        return
    # Live selection + the artist poster (for the poison check), one round-trip.
    try:
        url = PMS + '/library/all?guid=' + urllib.quote(helper.metadata.guid)
        text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
        if guid_lookup_is_ambiguous(text, tag):
            return
        m = re.search(r'thumb="([^"]*)"', text)
        if not m:
            return
        thumb = m.group(1)
        pm = re.search(r'parentThumb="([^"]*)"', text)
        parent_thumb = pm.group(1) if pm else None
    except Exception as e:
        log.error('%s: could not resolve the selection (%s)', tag, e)
        return
    # Per-track collapse, keyed on the selection (see recent_work_memo).
    if not should_run(tag, helper.metadata.guid, thumb, 600):
        return
    # The selected poster's bytes.
    try:
        turl = thumb if thumb.startswith('http') else PMS + thumb
        selected = HTTP.Request(turl, timeout=8, cacheTime=0).content
    except Exception as e:
        log.error('%s: could not download the selection (%s)', tag, e)
        return
    if not selected:
        return
    # Current cover.jpg. A RAISED read fails closed (never overwrite a cover we
    # could not see); a genuinely-absent file reads falsy and is fine to create.
    try:
        existing = Core.storage.load(cover_path)
    except Exception as e:
        log.error('%s: cover.jpg unreadable at %s (%s) -- skipping', tag, cover_path, e)
        return
    if existing and len(existing) == len(selected) and existing == selected:
        mark_done(tag, helper.metadata.guid, thumb)
        return
    # Is the selection one of OUR offered covers? Only then is it a promotable
    # pick; a custom upload or a replaced cover.jpg matches nothing here.
    is_offered = False
    for candidate_url in offered:
        try:
            resp = make_request(candidate_url)
            offered_bytes = resp.content if resp else None
        except Exception:
            offered_bytes = None
        if offered_bytes and len(offered_bytes) == len(selected) and offered_bytes == selected:
            is_offered = True
            break
    if not is_offered:
        return
    # Poison guard: never make the artist photo the local cover, even on a pick.
    if parent_thumb:
        try:
            aurl = parent_thumb if parent_thumb.startswith('http') else PMS + parent_thumb
            artist_bytes = HTTP.Request(aurl, timeout=8, cacheTime=0).content
        except Exception:
            artist_bytes = None
        if selection_is_artist_art(artist_bytes, selected):
            log.warn('%s: picked cover is the artist photo -- refusing to write it', tag)
            mark_done(tag, helper.metadata.guid, thumb)
            return
    if write_cover_sidecar(cover_path, selected):
        mark_done(tag, helper.metadata.guid, thumb)
        log.warn('%s: promoted the picked online cover to %s (%s bytes) -- now local',
                 tag, cover_path, len(selected))


def backup_selected_poster(helper):
    """
        Mirror the poster Plex is CURRENTLY showing to cover.jpg next to the
        book, so every item ends up with one and it survives a library rebuild
        (the fresh scan re-serves it via prefer_local_cover).

        THE PORTRAIT EXCEPTION IS GONE (v1.3.121). This used to take a
        `portrait_deferred` flag and refuse exactly one selection: the online
        cover the deferral itself chose, on the reasoning that mirroring an
        automatic choice would overwrite the operator's file. The flaw was in
        what that protected. The only file it could ever protect is one the
        agent had just POSITIVELY MEASURED as a print jacket -- that is what the
        deferral means -- and had already refused to display. So it preserved a
        file nobody wanted, made cover.jpg an unfaithful mirror in precisely the
        case where the agent had judged the file wrong, and left the book
        depending on the deferral firing on every future scan rather than being
        settled on disk.

        Measured live on Douglas Preston / "Extraction" (2026-07-25): the
        portrait-fix correctly force-selected the square 2400x2400 and this
        refusal then left cover.jpg as the 31,820-byte jacket.

        Dropping it is self-limiting -- the only write it newly permits replaces
        a portrait file with the square the agent preferred. A book whose
        cover.jpg is square never reaches that state (verified against Brandon
        Sanderson / "The Sunlit Man", whose hand-uploaded 1500x1500 square is
        already the file on disk). It also removes a per-refresh CDN fetch that
        existed only to answer the question this no longer asks.

        Residual, worth knowing: a genuinely portrait audiobook cover just over
        PORTRAIT_RATIO would now be replaced on disk as well as in Plex. The
        agent already refuses to DISPLAY such a file, so the operator sees it
        and can pick their own art -- and a deliberate pick still mirrors.

        THE RULE (operator's model): cover.jpg is a faithful mirror of the
        current selection, whoever chose it -- a hand-picked upload, the
        container's Audible art, or a switch from the Audible cover to the
        Hardcover one. Any change is captured on the next refresh. WHO selected
        it is deliberately not consulted: the earlier ownership test meant a
        book whose poster came from the agent never got a cover.jpg at all, and
        swapping between two agent-supplied covers was invisible to disk.

        Writes only on an actual change: identical bytes are skipped, as is our
        own padded re-select of the same image (see RESELECT_PAD), so a
        converged library does no work on refresh.

        This composes with prefer_local_cover rather than fighting it. That
        pref decides what Plex DISPLAYS -- select_local_cover runs first and
        pushes cover.jpg into Plex -- and this then mirrors the result, which
        by then is the same bytes, so nothing is written. With the pref off,
        Plex's selection is authoritative by definition and mirroring it is
        exactly right.

        Mechanism (the Lambda.bundle pattern, every step verified live under the
        Elevated code policy): resolve this item through Plex's own HTTP API
        (reachable, and the plugin's request is trusted -- no token needed), read
        its selected `thumb`, download those bytes, and Core.storage.save to
        cover.jpg. Byte-compare is a safe change-detector: /thumb serves the
        ORIGINAL bytes (verified identical to cover.jpg on an unchanged book).
    """
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
    # Resolve Plex's CURRENT selection first: one localhost round-trip, and the
    # only value that can key the per-track memo honestly.
    try:
        url = PMS + '/library/all?guid=' + urllib.quote(helper.metadata.guid)
        text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
        if guid_lookup_is_ambiguous(text, 'incipit poster-backup'):
            return
        m = re.search(r'thumb="([^"]*)"', text)
        if not m:
            log.warn('incipit poster-backup: no thumb in API response (first 200: %s)', text[:200]); return
        thumb = m.group(1)
        # The ARTIST's poster URL, from the same response. Used by the poison
        # guard at the first-write path below to recognise inherited art.
        # `parentThumb` is capital-T, so the lowercase `thumb=` match above never
        # picked it up by mistake.
        pm = re.search(r'parentThumb="([^"]*)"', text)
        parent_thumb = pm.group(1) if pm else None
    except Exception as e:
        log.error('incipit poster-backup: could not resolve the selected poster (%s)', e)
        return
    # Per-track collapse (see recent_work_memo), keyed on the SELECTION.
    #
    # This used to pass a FIXED token with a 60s TTL, which made it a rate
    # limiter rather than a change detector: change a poster in Plex, hit
    # Refresh Metadata, and the backup was silently skipped -- no log line and
    # nothing written -- until 60 seconds had elapsed. Observed live on
    # "A Warrior's Knowledge": refreshes at 00:59:33 and 00:59:41 did nothing
    # at all and only the one at 01:00:46 saved. That is the entire feature
    # failing precisely when someone is watching for it, which is worse than
    # failing loudly.
    #
    # Plex's thumb URL carries a version stamp that moves whenever the
    # selection changes (verified live: one item held stamps ...763 and ...782
    # nineteen seconds apart across a poster change), so keying on it still
    # collapses the repeats WITHIN a pass while letting a genuine change
    # through on the very next refresh. Correctness no longer rests on the TTL,
    # so it can be generous.
    if not should_run('poster-backup', helper.metadata.guid, thumb, 600):
        return
    # Read the on-disk cover first, for the unchanged-skip and the padded
    # re-select check below.
    #
    # A RAISED read is NOT the same as "there is no cover.jpg here".
    # Core.storage.load returns falsy for a file that simply is not there --
    # verified live across 5478 successful reads and 1487 first-time writes
    # with zero read-failure lines -- so an exception means the file may well
    # EXIST and we merely could not see it: an SMB blip, a stale handle,
    # EACCES. Both change guards below are `if existing`, so carrying None
    # forward from a failed read skips BOTH and turns this mirror into an
    # unconditional overwrite of a hand-curated cover we never managed to
    # read. Worse, it is the one path that can write `existing + RESELECT_PAD`
    # into the operator's file, the exact byte growth the comment below
    # forbids. Fail closed and let the next refresh retry.
    existing = None
    try:
        existing = Core.storage.load(cover_path)
    except Exception as e:
        log.error('incipit poster-backup: cover.jpg unreadable at %s (%s) -- '
                  'skipping, so an existing file is never blindly overwritten',
                  cover_path, e)
        return
    # Now the expensive half: the selected poster's actual bytes.
    try:
        turl = thumb if thumb.startswith('http') else PMS + thumb
        selected = HTTP.Request(turl, timeout=8, cacheTime=0).content
    except Exception as e:
        log.error('incipit poster-backup: could not download selected poster (%s)', e)
        return
    if not selected:
        return
    # Change detection: skip when the on-disk cover.jpg already matches.
    #
    # mark_done fires only on the paths below, where we reached a DEFINITE
    # outcome for this selection: mirrored, or provably not needing it. Every
    # failure path above returns without marking, so a blip retries on the next
    # track instead of being suppressed for the whole TTL.
    if existing and len(existing) == len(selected) and existing == selected:
        mark_done('poster-backup', helper.metadata.guid, thumb)
        log.info('incipit poster-backup: unchanged, skip'); return
    # ...including when the selection is OUR OWN padded re-select of that same
    # cover (see RESELECT_PAD): identical pixels, different bytes. Writing it
    # back would silently grow the operator's cover.jpg by the pad -- breaking
    # any byte/sha reconciliation against the curated-cover manifest -- and
    # would make the padded copy the new "plain" base, so every later deselect
    # mints another pad level instead of stopping at the documented boundary.
    if existing and selected == existing + RESELECT_PAD:
        mark_done('poster-backup', helper.metadata.guid, thumb)
        log.info('incipit poster-backup: selection is our padded re-select of '
                 'this cover, skip'); return
    # (The portrait-deferral refusal that used to sit here is gone in v1.3.121 --
    # see the docstring. It protected a file the agent had itself measured as a
    # print jacket and refused to display.)
    # POISON GUARD. A fresh book with no poster of its own shows its ARTIST's
    # art, and the first metadata pass fires before a real cover is selected --
    # so the "current selection" is the inherited author photo, and mirroring
    # THAT seeds cover.jpg with it (10 books hit this on the last rebuild).
    #
    # This used to be gated on `not existing`, i.e. the first write only, on the
    # theory that the birth moment was the only one worth guarding. It is not.
    # The selection can BECOME the artist photo at any time -- most easily via
    # select_local_cover pushing an already-poisoned cover.jpg back into Plex --
    # and mirroring that over a good cover.jpg destroys curated art, which is the
    # one outcome this bundle must never produce. Byte-identity with the artist's
    # own poster is ground truth, not a version heuristic, so there is no
    # false-skip risk in applying it to every write: the ONLY thing refused is a
    # cover that IS the author photo, which is never a legitimate book cover.
    #
    # Fails closed when the artist poster cannot be read -- a skipped mirror
    # retries on the next refresh (no mark_done below), whereas a wrong write is
    # permanent.
    # Through the shared helper, which memoises per guid: without it a forced
    # refresh of a multi-part book downloaded the same artist photo once per
    # track, and twice per track whenever the display path checked it too.
    # parent_thumb is already in hand, so this costs no extra lookup.
    if parent_thumb:
        artist_bytes, known = artist_poster_bytes(
            helper.metadata.guid, 'incipit poster-backup', parent_thumb
        )
        if not known:
            log.error('incipit poster-backup: could not read artist poster for the '
                      'poison check -- skipping this write to be safe')
            return
        if selection_is_artist_art(artist_bytes, selected):
            log.warn('incipit poster-backup: the selection is the inherited ARTIST '
                     'poster (byte-identical) -- refusing to mirror it to %s, so the '
                     'book is not poisoned; a real cover will mirror once selected',
                     'a new cover.jpg' if not existing else 'the existing cover.jpg')
            return
    # NO de-selected-upload guard here, and that is a decision with a history:
    # v1.3.114 added one (refuse to mirror an agent METADATA selection while
    # the disk file's own upload sat de-selected, on the theory that a Fix
    # Match could re-seat an agent poster with no human involved). Its first
    # live firing -- Joseph Bridgeman, 2026-07-25 16:26 -- was a person's
    # deliberate pick of an agent-offered poster, the single most common way a
    # poster is chosen, and the guard blocked the mirror that pick needed. The
    # guard could not tell those cases apart, and the automatic ones it feared
    # are covered without it: a re-match's default selection is either the
    # local cover (byte-identical to disk, caught by the unchanged-skip above)
    # or a portrait book's online default (refused by the portrait branch
    # above), and inherited artist art is refused by the poison guard below.
    #
    # Whoever chose the selection, it is what Plex shows, so it is what the
    # sidecar mirrors. The byte checks above already mean this only fires on a
    # real change.
    if write_cover_sidecar(cover_path, selected):
        mark_done('poster-backup', helper.metadata.guid, thumb)
        log.warn('incipit poster-backup: saved -> %s (%s bytes)', cover_path, len(selected))


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
RESELECT_PAD = b'\nincipit-reselect-v1'


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


def guid_lookup_is_ambiguous(text, tag):
    """
        True when /library/all?guid= matched MORE THAN ONE item.

        Both callers scrape the FIRST regex hit out of a SERVER-WIDE response,
        so when one agent guid exists in two library sections -- exactly the
        state during a rebuild, where an old and a new section coexist over the
        same media -- they silently act on whichever item happens to sort first:
        reading one item's selected poster and mirroring it into the other's
        folder, or POSTing an upload to the copy nobody is looking at. Every
        later refresh repeats it, because the state read never matches what was
        changed, so it never converges and never errors.

        Refusing is the only honest answer -- this response carries nothing that
        says which item belongs to the library being updated. One warn line
        naming the duplicates beats silently mirroring the wrong one.
    """
    distinct = []
    for k in re.findall(r'ratingKey="([0-9]+)"', text):
        if k not in distinct:
            distinct.append(k)
    if len(distinct) > 1:
        log.warn('%s: guid resolves to %s items (%s) -- refusing to guess which '
                 'copy is this library\'s; remove the duplicate section',
                 tag, len(distinct), ', '.join(distinct[:4]))
        return True
    return False


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
        if guid_lookup_is_ambiguous(text, tag):
            return None
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
        # parentThumb rides along in the SAME response, so hand it back rather
        # than making a caller re-fetch this document to scrape one attribute.
        pm = re.search(r'parentThumb="([^"]*)"', text)
        return (rk, selected_key, keys, (pm.group(1) if pm else None))
    except Exception as e:
        log.error('%s: poster state read failed (%s)', tag, e)
        return None


def same_image(first, second):
    """
        True when `second` is the SAME picture as `first` -- byte-identical, or
        our own RESELECT_PAD copy of it.

        The pad matters, and still does even though upload_and_select_poster no
        longer MINTS one: it used to re-POST image+RESELECT_PAD to force a
        re-selection when the plain bytes already existed de-selected, so albums
        touched before that changed are still carrying padded posters. An exact
        byte comparison does not recognise them.

        Shared by the two questions that both reduce to picture identity -- "is
        the selection the artist photo?" (poison) and "is the selection already
        the image I am about to upload?" (duplicate tiles). They were one rule
        copied twice in review; keeping a single implementation is what stops
        one of them learning about a new pad form and the other not.
    """
    if not first or not second:
        return False
    if len(first) == len(second) and first == second:
        return True
    try:
        _, _, padded = padded_variants(first)
    except Exception:
        return False
    return len(padded) == len(second) and padded == second


def selection_is_artist_art(artist_bytes, selected):
    """
        True when `selected` IS the artist photo -- plain, or our own padded
        re-select of it.

        Measured on Kyle Mills / "Fade", whose selected poster was byte-for-byte
        the author photo plus the 20-byte pad: an exact comparison missed it and
        the guard waved the poison through.
    """
    return same_image(artist_bytes, selected)


def selected_poster_bytes(rk, selected_key, tag):
    """
        (bytes_of_the_currently_selected_poster, known), via the local API.

        TWO values, for the same reason artist_poster_bytes returns two: the
        caller must be able to tell "the selection is a DIFFERENT image" from "I
        could not read it", because only the first may skip an upload. Collapsing
        them would turn one timed-out localhost call into a refusal to fix a
        wrong poster.

        The key Plex hands back is the `ratingKey` form (metadata://posters/...
        or upload://posters/...); the fetchable URL is /file?url=<it>, fully
        quoted -- the slashes and the colon must be escaped or Plex reads a
        truncated key and 404s.
    """
    if not rk or not selected_key:
        return (None, False)
    try:
        url = (PMS + '/library/metadata/' + rk + '/file?url='
               + urllib.quote(selected_key, ''))
        return (HTTP.Request(url, timeout=8, cacheTime=0).content, True)
    except Exception as e:
        log.error('%s: could not read the selected poster (%s)', tag, e)
        return (None, False)


# Per-guid cache for the artist poster, because update() runs once per TRACK
# and helper.force defeats the container re-read guard -- so without this a
# forced refresh of a 27-part book paid a /library/all round-trip PLUS a full
# artist-image download on every track, 54 requests to answer one question 27
# times identically. Values are (bytes_or_None, known, stamp); the TTL bounds
# staleness the same way recent_work_memo does.
artist_art_memo = {}
ARTIST_ART_TTL = 600


def artist_poster_bytes(guid, tag, parent_thumb=None):
    """
        (artist_poster_bytes_or_None, known) for the item with `guid`.

        TWO pieces of information, because collapsing them is a security hole in
        the poison guards: `None` alone cannot distinguish "this item genuinely
        has no artist art" from "I could not tell", and a caller that treats the
        second as the first proceeds to overwrite a poster on the strength of a
        timed-out request. `known` is False ONLY for a failure; callers that
        WRITE must refuse when it is False, callers that merely display may
        proceed.

        `parent_thumb` lets a caller that already holds the value skip the
        lookup entirely -- read_poster_state and backup_selected_poster both
        parse it out of a response they already fetched.
    """
    hit = artist_art_memo.get(guid)
    if hit and (time() - hit[2]) < ARTIST_ART_TTL:
        return (hit[0], hit[1])
    try:
        purl = parent_thumb
        if not purl:
            url = PMS + '/library/all?guid=' + urllib.quote(guid)
            text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
            if guid_lookup_is_ambiguous(text, tag):
                return (None, False)
            # capital-T parentThumb, so a lowercase thumb= match can't collide.
            pm = re.search(r'parentThumb="([^"]*)"', text)
            purl = pm.group(1) if pm else None
        if not purl:
            # Resolved cleanly; this artist simply has no poster. KNOWN.
            artist_art_memo[guid] = (None, True, time())
            return (None, True)
        if not purl.startswith('http'):
            purl = PMS + purl
        data = HTTP.Request(purl, timeout=8, cacheTime=0).content
        artist_art_memo[guid] = (data, True, time())
        return (data, True)
    except Exception as e:
        log.error('%s: could not read the artist poster (%s)', tag, e)
        return (None, False)


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


def upload_and_select_poster(guid, image_bytes, tag, token=None, state=None,
                             pref_asserted=False):
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
        downgraded to GET.

        By default this only ADDS a poster Plex does not already hold. When our
        bytes exist as a DE-selected upload, the v1.3.112 premise applies: for a
        BOOK, only a person choosing another poster produces that state, so we
        stand down and backup_selected_poster mirrors their choice to disk.

        `pref_asserted=True` is the one exception, for AUTHOR art. There the
        premise is false: the agent's own unpin (authors_prefer_hardcover
        removed) is what de-selects the pinned upload, so on re-pin the
        de-selected copy is the agent's doing, not a person's -- and the
        operator's wish is expressed BY the pref. That caller keeps the
        RESELECT_PAD lever: re-POST image+PAD, new content with identical
        pixels, which re-selects. One level only; when both variants already
        exist de-selected the budget is spent and we stand down loudly.
        Without this, one pin->unpin round trip wedged the pref permanently
        (v1.3.113 regression, caught in review).

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
    rk, selected_key, keys, parent_thumb = state
    if selected_key and (sha in selected_key or sha_padded in selected_key):
        log.info('%s: already the selected poster, skip', tag)
        mark_done(tag, guid, memo_token)
        return True
    # Explicit loop, not any(): the sandbox does not provide any()/all()/sum()
    # (proven live -- `NameError: global name 'any' is not defined` aborted the
    # whole artist update). set() IS available; the blocklist is irregular, so
    # find in-repo precedent before using any builtin here.
    have_plain = False
    have_padded = False
    for k in keys:
        if sha in k:
            have_plain = True
        if sha_padded in k:
            have_padded = True
    if have_plain and not pref_asserted:
        # Plex ALREADY offers this image and is not selecting it. For a BOOK,
        # nothing but a deliberate de-selection produces that state: on a new
        # album our bytes are not in the list at all, which is the upload below.
        #
        # This used to answer by re-POSTing a byte-PADDED copy -- new content to
        # the store, identical pixels -- because that re-selects. Measured live
        # on Will Wight (Soulsmith, Blackflame, Skysworn, 2026-07-25): three
        # hand-picked posters were replaced by the file on disk, each logged at
        # exactly 20 bytes over it, and backup_selected_poster then skipped
        # because the selection was "our own padded re-select". A second attempt
        # appeared to work only because the one-level pad budget was already
        # spent -- so the same action gave opposite results and the fix looked
        # random. The docstring called the pad the one lever unverified by live
        # test; this was its first real occurrence, and it took the operator's
        # choice away.
        #
        # Standing down is what lets backup_selected_poster mirror that choice
        # into cover.jpg, which converges in ONE refresh instead of two.
        log.info(
            '%s: this image is offered on rk %s but de-selected -- treating '
            'that as a deliberate choice and leaving the selection alone',
            tag, rk
        )
        mark_done(tag, guid, memo_token)
        return False
    if have_plain and pref_asserted and have_padded:
        # The pref wants this image back but both variants already sit
        # de-selected: the one-level pad budget is spent, and minting deeper
        # pads would grow the store without bound. Loud, because the pref is
        # now unenforceable without a manual UI pick.
        log.warn(
            '%s: image and its padded variant both exist de-selected on rk %s; '
            'out of re-select levers -- pick it in the UI', tag, rk
        )
        mark_done(tag, guid, memo_token)
        return False
    # LAST cheap-evidence gap: the keys say nothing about whether the selection
    # is already our picture. The sha tests above only recognise an upload://
    # key, because Plex names uploads by CONTENT sha; a CONTAINER key is sha1 of
    # the KEY STRING we filed the poster under -- sha1('incipit-local-cover') --
    # never the image's bytes, the same fact selection_is_agent_owned records.
    # So when Plex is already DISPLAYING our picture as a container poster,
    # nothing above can tell, and the POST below adds a byte-identical duplicate
    # that stays in the picker forever as a second, indistinguishable tile.
    #
    # Measured live 2026-07-25: 147 of 150 sampled albums (98%) and 75 of 169
    # artists carried exactly that duplicate, and 45% of every poster tile in
    # the library was a copy of another tile.
    #
    # Gated on `not have_plain`, and LAST, because it is the only step here that
    # costs a round trip. When our bytes ARE among the keys the selection is by
    # definition some other image, so the read would buy nothing -- and every
    # stand-down above must stay free.
    #
    # FAILS OPEN, deliberately asymmetric: `known` False means the read failed,
    # and uploading anyway is merely the status quo -- one wasted POST. Reading
    # "could not tell" as "already correct" would leave a WRONG poster standing,
    # the destructive direction the poison guards already fail closed against.
    # Byte equality is also the only thing that may skip: a CHANGED cover.jpg
    # must still reach Plex (v1.3.45), and it differs.
    if not have_plain and selected_key:
        current, known = selected_poster_bytes(rk, selected_key, tag)
        if known and same_image(image_bytes, current):
            log.info('%s: the selected poster already IS this image (rk %s) -- '
                     'not uploading a duplicate', tag, rk)
            mark_done(tag, guid, memo_token)
            return True
    # pref_asserted with the plain copy de-selected: the agent's own unpin put
    # it there, so re-select via the pad -- new content, identical pixels.
    reselect = have_plain and pref_asserted
    post_bytes = padded_bytes if reselect else image_bytes
    content_type = 'image/png' if image_bytes[:4] == '\x89PNG' else 'image/jpeg'
    try:
        up = PMS + '/library/metadata/' + rk + '/posters'
        HTTP.Request(up, data=post_bytes,
                     headers={'Content-Type': content_type}, timeout=8)
        log.warn('%s: uploaded + selected (rk %s, %s bytes, %s%s)',
                 tag, rk, len(post_bytes), content_type,
                 ', PADDED pref re-select' if reselect else '')
        mark_done(tag, guid, memo_token)
        return True
    except Exception as e:
        log.error('%s: upload failed (%s)', tag, e)
        return False


def converge_author_art(helper, target_url, other_url, tag, own_uploads_only=False):
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
    rk, selected_key, keys, parent_thumb = state
    # Strict mode (the unpin direction): act only on a selection this agent
    # demonstrably UPLOADED. A metadata:// container key is ambiguous by
    # construction -- the container may have defaulted to it, or the user may
    # have clicked it, and Plex exposes nothing that tells the two apart. The
    # loose test counted every incipit metadata:// key as ours, so unpin ran
    # for authors that were never pinned at all: 69 uploads in one day against
    # 233 no-ops, each one converting a container selection into a permanent
    # upload:// poster, and each one capable of overwriting a poster the user
    # had chosen by hand. Undo only what we can prove we did.
    #
    # The case this gives up -- reverting a pin that only ever existed as a
    # fresh-scan container prune -- largely self-corrects: with the author off
    # the pref, the next scan offers both images again and validate_keys
    # selects the secondary, which is what unpin wanted anyway.
    if own_uploads_only and not (selected_key or '').startswith('upload'):
        log.info('%s: selection is not one of our uploads -- leaving it', tag)
        mark_done(tag, guid, target_url)
        return
    owned_shas = []
    target_bytes = None
    if selected_key and selected_key.startswith('upload'):
        # Only an upload:// selection needs byte shas to judge ownership.
        target_bytes = fetch_url_bytes(target_url)
        for image_bytes in (target_bytes, fetch_url_bytes(other_url)):
            if image_bytes:
                # The one padded_variants call site that used to run bare --
                # its two siblings are both guarded. HTTPRequest.content
                # decodes text-ish content types to unicode, so a CDN
                # interstitial or throttle page served as 200 text/html
                # yields a unicode body and hashlib.sha1 raises
                # UnicodeEncodeError. Nothing between here and
                # Agent.Artist.update() catches it, so the artist update died
                # half-written with only a generic traceback. A body we cannot
                # hash is by definition not one of our images, so skipping it
                # is also the correct answer.
                try:
                    s, sp, _ = padded_variants(image_bytes)
                except Exception as e:
                    log.error('%s: could not hash a candidate image (%s)', tag, e)
                    continue
                owned_shas.extend([s, sp])
    if not selection_is_agent_owned(selected_key, owned_shas):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, target_url)
        return
    if target_bytes is None:
        target_bytes = fetch_url_bytes(target_url)
    if not target_bytes:
        return
    # pref_asserted: for author art the de-selected copy is the agent's OWN
    # doing (the opposite-direction pin/unpin uploaded something else), so the
    # v1.3.112 "a de-selection is a person's choice" stand-down misreads it and
    # wedges the pref after one round trip. The ownership gate above has already
    # established no USER upload is being overridden.
    upload_and_select_poster(guid, target_bytes, tag, token=target_url,
                             state=state, pref_asserted=True)


def correct_portrait_selection(helper, cover_bytes, square_bytes):
    """
        Move a book OFF a print-jacket cover.jpg that won a fresh scan.

        The portrait deferral (local_cover_is_portrait) decides cover.jpg is a
        print jacket and declines to make it the default -- but it expresses that
        through the posters CONTAINER, which only wins on a FRESH scan. When the
        square online cover was not yet in hand at scan time (an unresolved match,
        a CDN blip) the jacket gets selected and NO later refresh can move it: the
        deferral fires correctly on every subsequent pass and is powerless.

        Measured live 2026-07-25: 3 of 1403 albums sat frozen on a portrait while
        their own container already held a square 2400x2400 -- Enemy of the State,
        The Ghost, Extraction. The Ghost's own UPLOAD was the portrait, which is
        the proof the deferral had not fired on the pass that selected it.

        The upload lever CAN move a persisted selection, so use it -- but only on
        proof. FAILS CLOSED, the deliberate opposite of the duplicate guard in
        upload_and_select_poster: that one merely declines to write, this one
        OVERWRITES a selection, so "could not read it" must never license the
        write. Two facts must hold first:

          - the selection is OURS (a container poster, or an upload carrying the
            jacket's own sha -- the agent's earlier select_local_cover). A
            hand-uploaded poster is never touched.
          - the selected bytes ARE the measured print jacket. Anything else means
            somebody already moved this book, and it is left alone.

        Does nothing without a square to offer: for a book whose every cover is a
        jacket (measured on four Adrian McKinty titles, whose only square is the
        embedded art Local Media Assets contributes and which this agent cannot
        select) the jacket is the best art we hold.
    """
    if not cover_bytes or not square_bytes:
        return
    tag = 'incipit portrait-fix'
    guid = helper.metadata.guid
    state = read_poster_state(guid, tag)
    if state is None:
        return
    rk, selected_key, keys, parent_thumb = state
    try:
        sha, sha_padded, _ = padded_variants(cover_bytes)
    except Exception as e:
        log.error('%s: could not hash the local cover (%s)', tag, e)
        return
    # The jacket's own shas count as ours: select_local_cover may have uploaded
    # it on an earlier pass, and undoing our own act is the whole point.
    if not selection_is_agent_owned(selected_key, [sha, sha_padded]):
        log.info('%s: selection is a user upload -- leaving it', tag)
        return
    current, known = selected_poster_bytes(rk, selected_key, tag)
    if not known:
        log.error('%s: could not read the selected poster -- NOT overriding it, '
                  'so a blip cannot take away a poster on rk %s', tag, rk)
        return
    if not same_image(cover_bytes, current):
        log.info('%s: the selection is not the print jacket -- leaving it', tag)
        return
    log.warn('%s: rk %s is showing the PORTRAIT cover.jpg the deferral declined; '
             'force-selecting the square online cover instead', tag, rk)
    upload_and_select_poster(guid, square_bytes, tag, token=sha, state=state)


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


def select_best_fit_author_art(helper, thumb_dims, secondary_dims):
    """
        Force-select whichever author portrait fills the square tile better,
        for an ALREADY-SCANNED artist. Opt-in via `prefer_square_author_art`.

        The container ordering only decides on a fresh scan, so without this the
        improvement never reaches an existing library -- measured 2026-07-25, 39
        artists were sitting on the worse-fitting image with no way to converge
        short of picking each by hand.

        Off by default because this re-selects images the operator may have
        chosen deliberately, and Plex exposes no way to tell a hand-picked
        container key from one the agent set (the same limitation that makes
        unpin_hardcover_author_art refuse to touch container keys). Turning it
        on is the operator asking for the tile to be filled.

        Does nothing without evidence: an unmeasurable image, a missing second
        image, or two identically-sized ones all leave the artist alone rather
        than spend an upload/select round trip to change nothing.
    """
    if not helper.thumb or not helper.thumb_secondary:
        return
    if not thumb_dims or not secondary_dims or thumb_dims == secondary_dims:
        return
    winner = better_square_portrait(thumb_dims, secondary_dims)
    if winner is thumb_dims:
        target, other = helper.thumb, helper.thumb_secondary
    else:
        target, other = helper.thumb_secondary, helper.thumb
    converge_author_art(helper, target, other, 'incipit author-art-fit')


def select_sole_author_art(helper):
    """
        Force-select the ONE author image we have, for an already-scanned artist.

        The container can only set a selection on a FRESH scan, so on a Refresh an
        artist keeps whatever Plex persisted. That is fine when there are two
        images -- the default (the Audible photo) is the better pick for most
        authors, which is why anything else is opt-in via authors_prefer_hardcover.

        WHY THE AUDIBLE PHOTO IS THE DEFAULT WHEN THE API RANKS IT SECOND: the
        API deliberately puts the more TRUSTWORTHY portrait in `image`
        (Hardcover, because Audible's is often a book cover rather than a face --
        Craig Alanson's is his ebook cover, measured 734x1080), and the leftover
        in `imageAlt`. This container inverts that for DISPLAY: it offers both
        and a two-key validate_keys selects the SECOND, because Audible's photos
        are square and Plex's artist tiles are square, while Hardcover's are
        often tall. Rank by trust there, choose by fit here. Neither side is a
        bug; reading one without the other has cost real debugging time.

        Consequence worth knowing: an artist scanned while only ONE image
        existed keeps that stale selection forever, because the unpin direction
        below refuses to touch a container key -- it cannot tell a stale
        automatic pick from a deliberate one in the UI. Fix those by picking in
        the UI; it sticks, since nothing here overrides a non-agent-upload.

        But when the API returns a portrait and NO alternative (`imageAlt` empty),
        there is no taste question left: the choice is that portrait or the blank
        placeholder. Measured on JD Franx and Graham McNeill, whose portraits come
        from Goodreads/Hardcover and who have no Audible photo at all -- a Refresh
        ran the UNPIN direction and left them with no poster at all.

        Same tag as the pin direction so the two keep sharing their memo.
    """
    converge_author_art(
        helper, helper.thumb, helper.thumb_secondary,
        'incipit author-art-select'
    )


# How close two portraits' SHORT EDGES must be before squareness decides
# instead of resolution. 0.75 = within 25%. Measured against the live library:
# it takes Bryce O'Connor's 820x820 over his 1000x1500 (820/1000 = 0.82) while
# leaving Glen Cook on his 3072x2304 rather than a 117x150 thumbnail.
SQUARE_TIE_BAND = 0.75


def better_square_portrait(first, second):
    """
        Which of two (width, height) portraits fills a SQUARE tile better.

        Plex renders artist art in a square tile, so the usable resolution is
        the SHORT EDGE -- what survives the crop. That single number is why a
        3072x2304 landscape (2304px usable) beats a 117x150 thumbnail however
        much squarer the thumbnail is, and it is the whole reason this is not a
        "prefer square" rule: measured on Glen Cook, preferring squareness
        outright swapped a sharp photo for a postage stamp.

        Squareness only decides when the short edges are COMPARABLE
        (SQUARE_TIE_BAND): there a native square wins, because the pixels given
        up are few and cropping a tall portrait cuts the top or bottom off the
        subject. Orientation-blind on purpose -- a wide photo loses its sides,
        a tall one its ends, and both keep min(w, h).

        None means "could not measure" (image_dimensions failed) and must never
        win by accident, so it always loses to a measurable image; two Nones
        return None so the caller keeps whatever order it already had.
    """
    if not first:
        return second
    if not second:
        return first
    first_short = min(first[0], first[1])
    second_short = min(second[0], second[1])
    if first_short <= 0 or second_short <= 0:
        return first if first_short >= second_short else second
    smaller = min(first_short, second_short)
    larger = max(first_short, second_short)
    if (float(smaller) / float(larger)) >= SQUARE_TIE_BAND:
        # Comparable resolution: let shape decide.
        first_off = abs(float(first[0]) / float(first[1]) - 1.0)
        second_off = abs(float(second[0]) / float(second[1]) - 1.0)
        if abs(first_off - second_off) > 0.01:
            return first if first_off < second_off else second
    return first if first_short >= second_short else second


def offer_secondary_author_poster(helper, valid_posters):
    """
        Add the Audible `imageAlt` to the artist's poster container as a
        selectable option, and return the updated validate_keys list.

        Kept as an OPTION even for pinned authors: not wanting it selected is not
        the same as not wanting it available, and pruning it left those authors
        with a single poster and no way to switch in the UI.
    """
    if not helper.thumb_secondary or helper.thumb_secondary == helper.thumb:
        return valid_posters, None
    # Dimensions come back with the list so the caller can decide which image
    # fills a square tile better (see better_square_portrait). Measured here
    # because this is where the bytes already are -- fetching them again to
    # measure would double the author-art traffic.
    secondary_dims = None
    if (helper.thumb_secondary not in helper.metadata.posters or helper.force):
        secondary_data = make_request(helper.thumb_secondary)
        if secondary_data is not None:
            helper.metadata.posters[helper.thumb_secondary] = \
                Proxy.Media(secondary_data, sort_order=1)
            secondary_dims = image_dimensions(secondary_data)
    valid_posters.append(helper.thumb_secondary)
    return valid_posters, secondary_dims


def unpin_hardcover_author_art(helper):
    """
        Unpin direction: the author is NOT on the pref (any more), so make the
        Audible photo (`thumb_secondary`) the selection again -- but ONLY when
        the current selection is one this agent uploaded.

        There is no "was this author ever pinned" state anywhere, so this runs
        for every author absent from the pref, which by default is every author
        in the library. Under the old key-based ownership test that meant any
        incipit metadata:// selection counted as ours, so unpin did real work
        for authors that were never pinned -- 69 uploads in a single day
        against 233 no-ops -- converting container selections into permanent
        upload:// posters and, worse, silently replacing a poster the user had
        picked in the Plex UI. A container key cannot be told apart from a
        deliberate click; Plex exposes no signal for it.

        So this direction now undoes only what it can prove it did. The pin
        path's own uploads are still revertable, which is the case that
        matters. A pin that existed only as a fresh-scan container prune is
        not reverted here, but that self-corrects: with the author off the
        pref, the next scan offers both images and validate_keys selects the
        secondary -- the outcome unpin was reaching for.

        Needs only the TARGET image: an author whose record lost its Hardcover
        image can still be reverted to the Audible one.
    """
    if not helper.thumb_secondary or helper.thumb_secondary == helper.thumb:
        return
    converge_author_art(
        helper, helper.thumb_secondary, helper.thumb,
        'incipit author-art-unpin', own_uploads_only=True
    )


def select_local_cover(helper, cover_bytes=None):
    """
        Force the book folder's cover.jpg to become the SELECTED Plex poster on
        a Refresh of an ALREADY-scanned book (the container path only wins on a
        fresh scan). Ownership-guarded: an agent-supplied selection (or our own
        earlier upload) is overridden -- that is what prefer_local_cover means
        -- but a USER'S custom upload is left alone, so hand-picks survive and
        backup_selected_poster (which now runs AFTER this) can capture them to
        cover.jpg instead of this path clobbering them.
    """
    # The album update has usually just read this exact file to seed the
    # posters container; accept those bytes rather than pulling ~1MB back over
    # SMB a second time in the same pass. None means "nobody read it for me".
    if cover_bytes is None:
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
    rk, selected_key, keys, parent_thumb = state
    if not selection_is_agent_owned(selected_key, [sha, sha_padded]):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, sha)
        return
    # POISON GUARD (select side). cover.jpg on disk can itself BE the artist
    # photo -- that is what "poisoned" means -- and this function's whole job is
    # to force cover.jpg to become the selection, padding the bytes to defeat
    # Plex's de-duplication when it has to. So on a poisoned book it does not
    # merely fail to help: it actively re-poisons the album, overwriting a good
    # poster the operator had just picked by hand.
    #
    # Measured live on Brian Jacques / "Mattimeo": the operator selected a real
    # cover, hit Refresh Metadata, and this path re-uploaded the 12,575-byte
    # author photo as a 12,595-byte padded re-select and re-selected it. A
    # second refresh 79 seconds later "worked" only because should_run's 90s
    # window happened to suppress this call -- so the same action gave opposite
    # results and the fix looked random.
    #
    # The existing guard in backup_selected_poster only stops poison being
    # WRITTEN to disk for the first time; nothing stopped poison already on disk
    # being pushed back into Plex. Refuse, and let backup_selected_poster mirror
    # whatever is legitimately selected over the bad cover.jpg -- which is
    # exactly what the accidental second refresh did.
    #
    # Skipped on a book that has already converged (cover.jpg IS the selection),
    # which is the overwhelmingly common case: there is nothing to push, so the
    # read would be pure cost and "a converged library does no work on refresh"
    # would stop being true.
    converged = bool(selected_key) and (sha in selected_key or sha_padded in selected_key)
    if not converged:
        artist_bytes, known = artist_poster_bytes(guid, tag, parent_thumb)
        # FAIL CLOSED. This path force-selects cover.jpg over whatever the
        # operator picked, so "could not tell" must not read as "not poison":
        # artist_poster_bytes returns None on a timed-out localhost call, an
        # ambiguous guid mid-rebuild or a 404 parentThumb, and treating that as
        # a clean bill of health re-uploaded the padded author photo and
        # re-selected it -- the Mattimeo failure, reachable from a single blip.
        # backup_selected_poster's twin guard has always failed closed for the
        # same reason ("a wrong write is permanent"); this one is the MORE
        # destructive of the two, because it overwrites a selection in Plex.
        # No mark_done: a transient failure must retry, not be suppressed.
        if not known:
            log.error('%s: could not read the artist poster -- NOT selecting '
                      'cover.jpg, so a blip cannot re-poison the album', tag)
            return
        if selection_is_artist_art(artist_bytes, cover_bytes):
            # Memoised on the cover's own sha, so a repaired cover.jpg re-runs
            # immediately rather than waiting out the TTL.
            mark_done(tag, guid, sha)
            log.warn('%s: cover.jpg IS the artist photo (byte-identical) -- refusing '
                     'to select it, so the book is not re-poisoned; the current '
                     'selection stands and will mirror to disk', tag)
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
        # match that already works, so it runs unconditionally.
        # Genuine zero-result ONLY ([], not None): a None is a transport blip
        # from the loop above, and firing a second (recovery) search on a blip
        # is wasted work -- and contradicts this block's "genuine zero-result"
        # contract. `result is not None` excludes the blip; `not result` keeps [].
        if result is not None and not result:
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
            request = str(make_request(
                book_url, cache_time=search_cache_time(helper.manual)
            ))
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
            request = str(make_request(
                search_url, cache_time=search_cache_time(helper.manual)
            ))
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
        # Read the DISPLAYED title before anything can rewrite it. The
        # authors_prefer_hardcover match below deliberately compares against
        # the title Plex shows -- the operator types what they SEE -- but
        # set_metadata_title() overwrites metadata.title with the API's `name`
        # whenever force is set, which is exactly the path the pin/unpin API
        # runs on. Reading it after that point collapsed the two keys into one
        # and silently unpinned any author pinned by their displayed name
        # (e.g. a phantom "Author (Series)" artist) on every Refresh Metadata.
        displayed_title = helper.metadata.title
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
        # BOTH initialised before any branch: the pinned-author path below
        # skips offer_secondary_author_poster entirely, and an artist with no
        # thumb skips the whole block -- either way the force-refresh section
        # still reads these, and an unbound name is a NameError that would
        # abort the whole artist update.
        thumb_dims = None
        secondary_dims = None
        if helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = make_request(helper.thumb)
                if thumb_data is not None:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=0
                    )
                    thumb_added = True
                    # Measured while the bytes are in hand; see
                    # better_square_portrait for what it decides.
                    thumb_dims = image_dimensions(thumb_data)
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
            for value in (helper.name, displayed_title)
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
                valid_posters, secondary_dims = offer_secondary_author_poster(
                    helper, valid_posters
                )
                # validate_keys selects the LAST key, so put the image that
                # fills a square tile better at the end. Only when BOTH were
                # measurable and they actually differ -- an unmeasured image
                # must not change which poster an artist has had for months.
                if (thumb_dims and secondary_dims
                        and thumb_dims != secondary_dims
                        and len(valid_posters) == 2):
                    if better_square_portrait(thumb_dims, secondary_dims) is thumb_dims:
                        valid_posters = [helper.thumb_secondary, helper.thumb]
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
            elif Prefs['prefer_square_author_art'] and helper.thumb_secondary:
                # Opt-in: converge an already-scanned artist onto whichever
                # portrait fills the square tile better. The container ordering
                # above only decides on a FRESH scan, so without this an
                # existing library never benefits.
                select_best_fit_author_art(helper, thumb_dims, secondary_dims)
            elif helper.thumb and not helper.thumb_secondary:
                # Exactly one image exists, so there is nothing to defer TO: the
                # unpin below would leave the artist with no poster at all.
                select_sole_author_art(helper)
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
            request = str(make_request(
                search_url, cache_time=search_cache_time(helper.manual)
            ))
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

        # PART 2: a deliberate pick of an offered online cover becomes local.
        # Runs BEFORE the local-cover block so, when the operator has picked the
        # square/portrait we offer, it is copied into cover.jpg first and the
        # block below then reads and asserts it. No-op unless prefer_local + force
        # + the live selection byte-matches a cover WE offered (never a custom
        # upload, never a hand-placed cover.jpg).
        promote_picked_cover(helper)

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
        # Set when a PORTRAIT local cover.jpg was skipped in favour of the square
        # online cover, so the force-select below does not simply re-read the file
        # and re-impose it, and the online cover keeps the default slot.
        deferred_portrait_local = False
        # Local cover.jpg IS the artist photo. Like a deferred portrait it must
        # not take the default slot -- but UNLIKE one it is not worth preserving,
        # so the poster mirror still runs and can overwrite it. Keeping the two
        # apart is the whole point: sharing a flag is what left two books stuck
        # with the author photo on disk through repeated forced refreshes.
        poisoned_local = False
        # Hoisted so the bytes read below can be handed to select_local_cover
        # instead of it re-reading the same file in the same pass. Stays None
        # on every path that does not read, so the callee still falls back.
        cover_bytes = None
        # Hoisted for the same reason as cover_bytes: correct_portrait_selection
        # below needs the square online cover's BYTES, but the only assignment
        # sits under `if helper.thumb:` -- so a book with no online image (the
        # very case that comment set out to survive) left it unbound and raised
        # NameError instead of simply having nothing to offer.
        thumb_data = None
        # Hoisted out of the prefer_local branch: the container-membership check
        # further down runs under `if helper.thumb:`, which is NOT nested inside
        # it, so leaving the assignment there was a NameError whenever the pref
        # was off.
        local_key = 'incipit-local-cover'
        if prefer_local:
            # Per-track guard: Plex calls update() once PER TRACK, so a
            # multi-part book would re-read the (up to ~1MB) cover.jpg on every
            # track. Skip the re-read once our poster is already in this pass's
            # container -- UNLESS force, so a real "Refresh Metadata" (force=1)
            # always re-reads and picks up a NEWLY dropped/replaced cover.jpg.
            if local_key in helper.metadata.posters and not helper.force:
                local_set = True
            else:
                cover_bytes = local_cover_bytes(helper)
                # POISON FIRST, independent of shape and of whether the record
                # has an online cover.
                #
                # This check used to sit INSIDE the portrait branch on the
                # premise that "an author photo is portrait by definition". It
                # is not: Audible author art is frequently square, and a
                # Hardcover/OpenLibrary book-level match has no online cover at
                # all (helper.thumb falsy) -- the case the block below is
                # deliberately written to survive. In either, poisoned_local
                # stayed False and the author photo was handed sort_order=0 plus
                # validate_keys, i.e. Plex was TOLD to make it the default
                # poster. Gating a byte-identity fact behind an aspect-ratio
                # heuristic made the repair work for tall author photos and not
                # square ones, which reads as random.
                if cover_bytes:
                    artist_bytes, known = artist_poster_bytes(
                        helper.metadata.guid, 'incipit cover'
                    )
                    if selection_is_artist_art(artist_bytes, cover_bytes):
                        # ...unless the artist's art IS this book's own cover.
                        # For a one-book author Audible/Hardcover routinely serve
                        # the book cover AS the author image, so byte-identity
                        # says nothing about poison -- and acting on it would let
                        # the mirror below replace a hand-curated cover.jpg with
                        # the online copy. Comparing against the online cover
                        # settles it with data already in hand.
                        online = fetch_url_bytes(helper.thumb) if helper.thumb else None
                        if online and selection_is_artist_art(artist_bytes, online):
                            log.info(
                                'incipit cover: cover.jpg matches the artist art, but '
                                'so does the ONLINE cover -- this is the book\'s own '
                                'art, not poison; leaving it alone'
                            )
                        else:
                            poisoned_local = True
                            log.warn(
                                'incipit cover: local cover.jpg is the ARTIST photo -- '
                                'not offering it as the default, and allowing the '
                                'selected poster to mirror back over it'
                            )
                    elif not known:
                        # Could not tell. Display-only decision, so proceed as
                        # before rather than demoting a possibly-fine cover --
                        # but say so, because the WRITE paths fail closed on the
                        # same signal and the asymmetry should be visible.
                        log.error(
                            'incipit cover: could not read the artist poster; '
                            'cannot check cover.jpg for poison this pass'
                        )
                # A clearly PORTRAIT cover.jpg is a print jacket, not audiobook
                # art -- the signature of an upstream pipeline that matched a
                # print edition (the same mismatch that writes a wrong-edition
                # ASIN into the sidecar). Plex music art is square, and the API
                # offers a real square cover, so defer to it rather than making
                # the portrait scan the default. Square/near-square local covers
                # (including every hand-curated one) are untouched. Never for a
                # file already judged poison: that one must not be preserved.
                if (
                    cover_bytes and helper.thumb and not poisoned_local
                    and local_cover_is_portrait(cover_bytes)
                ):
                    log.warn(
                        'incipit cover: local cover.jpg is PORTRAIT (print jacket?) '
                        '-- deferring to the square online cover as the default'
                    )
                    deferred_portrait_local = True
                if cover_bytes:
                    try:
                        # A DEFERRED portrait cover is still OFFERED, just not the
                        # default: dropping it entirely left the operator unable to
                        # pick their own art back, which is the very bug the
                        # always-offer comment below records as already fixed.
                        helper.metadata.posters[local_key] = Proxy.Media(
                            cover_bytes,
                            sort_order=1 if (deferred_portrait_local or poisoned_local) else 0
                        )
                        if not (deferred_portrait_local or poisoned_local):
                            helper.metadata.posters.validate_keys([local_key])
                            log.warn('incipit cover: LOCAL cover set as the default poster')
                            local_set = True
                    except Exception as e:
                        log.error('incipit cover: Proxy.Media(local) failed (%s)', e)

        # The online cover (native square Apple, else the provider portrait) is
        # ALWAYS offered as a pickable option -- even when a local cover.jpg is
        # the default (prefer_local). prefer_local was only ever meant to make the
        # local cover the DEFAULT, not the ONLY option: the whole block used to be
        # gated on `not local_set`, so a book whose migrated cover.jpg is a
        # portrait print cover never even LISTED its square audiobook cover (The
        # Skin Map, The Spirit Well). Now the local cover keeps the selection
        # (validate_keys above) while the online cover rides at a lower priority
        # (primary_order = 1 when preferring local), so the operator can switch to
        # it and a later Refresh mirrors that pick back to cover.jpg.
        if helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = fetch_url_bytes(helper.thumb)
                # Offering an online cover that is byte-identical to the local
                # cover.jpg just lists the SAME picture twice in the picker (Plex
                # keys each source separately, so one image can appear under our
                # online key, our local key, our upload, and Local Media Assets'
                # own entries). Skip the redundant one. Re-evaluated on every
                # refresh against the CURRENT file, so replacing cover.jpg with a
                # different image makes the online cover an option again -- the
                # alternative stays available exactly when it is actually an
                # alternative.
                if (
                    thumb_data is not None
                    and local_set
                    and cover_bytes
                    and thumb_data == cover_bytes
                ):
                    log.info(
                        'incipit cover: online cover is byte-identical to '
                        'cover.jpg -- not offering a duplicate'
                    )
                elif thumb_data is not None:
                    # When the local cover was deferred (portrait print jacket)
                    # or refused (it is the artist photo), this square cover IS
                    # the default -- it must not keep the demoted slot the
                    # prefer_local setting assigned before we measured the file.
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data,
                        sort_order=0 if (deferred_portrait_local or poisoned_local)
                        else primary_order
                    )
            # SELECT the online cover only when there is no local cover to be the
            # default. With a local cover set it is merely OFFERED, not selected;
            # validate_keys only touches our metadata:// posters, so a user's
            # upload:// pick is never evicted.
            if (
                not local_set
                and helper.thumb in helper.metadata.posters
            ):
                # Keep the DEMOTED local cover in the container. A single-key
                # validate_keys is a PRUNE (see the pin/unpin paths above), so
                # passing only the online cover evicted the local entry added
                # 60 lines earlier -- the one whose comment promises "a DEFERRED
                # portrait cover is still OFFERED, just not the default:
                # dropping it entirely left the operator unable to pick their
                # own art back". It was being dropped entirely, which also made
                # its sort_order=1 demotion dead code. The online cover still
                # becomes the default by sort_order=0; validate_keys is what
                # decides membership, not priority.
                keep = [helper.thumb]
                if local_key in helper.metadata.posters:
                    keep.append(local_key)
                helper.metadata.posters.validate_keys(keep)

        # Local cover, force-select via the trusted Plex API so a dropped/replaced
        # cover.jpg takes effect on Refresh Metadata even on an ALREADY-scanned
        # book -- the posters-container path above only wins on a fresh scan.
        # SMB-safe (writes to Plex's metadata store, not the media folder).
        # Skipped when a portrait local cover was deferred: re-reading it here
        # would re-impose the print jacket the block above deliberately declined.
        # Skipped for a poisoned local cover too -- select_local_cover refuses the
        # artist photo on its own, but not asking saves two round trips and keeps
        # the reason in one place.
        if (
            Prefs['prefer_local_cover'] and helper.force
            and not deferred_portrait_local and not poisoned_local
        ):
            select_local_cover(helper, cover_bytes)
        # The MIRROR of that call. When the jacket was deferred, the container
        # said "use the square" and Plex ignored it, because a container cannot
        # move a selection it persisted on an earlier scan. Say it through the
        # upload lever instead, which can -- otherwise the deferral is correct
        # and powerless forever, which is exactly the 3 albums measured frozen
        # on a portrait. Same force gate as its sibling: cover_bytes is only
        # re-read on a real Refresh Metadata, so there is nothing to compare
        # against on an incremental pass.
        elif (
            Prefs['prefer_local_cover'] and helper.force
            and deferred_portrait_local and not poisoned_local
        ):
            correct_portrait_selection(helper, cover_bytes, thumb_data)
        # Back up the currently-selected poster to cover.jpg (opt-in). Runs
        # AFTER the select: select_local_cover is ownership-guarded (a user's
        # custom upload survives it), so what is selected HERE is the state
        # worth persisting -- a new cover.jpg just got selected (backup sees
        # identical bytes and skips), or a user hand-pick survived (backup
        # captures it to cover.jpg, so it survives a library rebuild). The old
        # backup-first order clobbered a freshly-dropped cover.jpg with the
        # previous selection before prefer_local could ever serve it.
        #
        # The portrait deferral no longer gates the mirror off wholesale -- that
        # swallowed every deliberate pick on a portrait book (measured live on
        # Joseph Bridgeman: hand-picked poster, Refresh, nothing on disk, no log).
        # The flag is passed down instead, and backup_selected_poster refuses
        # exactly ONE selection: the online default the deferral itself made,
        # which is the only selection that can occur without a human act.
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
        search_cache_time(manual) (1h for a scan, 0 for a manual Fix Match or
        with the dev toggle) so per-track re-searches during a scan are free
        while a human-driven search always gets a current answer; ASIN data
        lookups use the default week-long cache, since those records are stable.
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
