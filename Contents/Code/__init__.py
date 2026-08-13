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

# The score Plex itself requires before it will AUTO-APPLY a match. Distinct
# from GOOD_SCORE (which only stops us searching further) and from
# SearchTool.IGNORE_SCORE = 45 (which only decides what we OFFER). Measured on
# the live server: a result below this is listed in Fix Match and applied to
# nothing.
PLEX_AUTO_MATCH_SCORE = 80

# Setup logger
log = Logging()


def artist_recovery_warranted(result, info):
    """
        Whether to attempt the folder-confirmed author recovery.

        This used to be `result is not None and not result` -- a ZERO-result
        search only. Measured live on the .99 rebuild 2026-08-01, that gate is
        exactly why "Stephenson & Galland" survived as a phantom artist:

          * the ARTIST tag is literally 'Stephenson & Galland' (no ALBUMARTIST),
          * author_candidates() splits it correctly to ['Stephenson','Galland'],
          * `/authors?name=Stephenson` RETURNS FOUR AUTHORS with the right one
            (Neal Stephenson) FIRST -- so the result set was non-empty and the
            recovery never ran,
          * but the search scores against the BARE SURNAME, and "Neal
            Stephenson" is 60 against "Stephenson" because the missing first
            name IS the whole edit distance. 60 clears IGNORE_SCORE so it is
            offered, and misses PLEX_AUTO_MATCH_SCORE so nothing is applied.

        A non-empty result set that cannot auto-match is as useless as an empty
        one, and recovery -- which confirms the author against the FILE PATH,
        where the folder is literally "Neal Stephenson" -- is the tool for it.

        `result is None` stays excluded: that is a transport blip, and
        recovering on one would spend a second search on no evidence.
        @param result the raw search result list (None = request failed)
        @param info the SCORED rows for that result
        @returns True when recovery should run
    """
    if result is None:
        return False
    if not result:
        return True
    for row in (info or []):
        try:
            if row.get('score', 0) >= PLEX_AUTO_MATCH_SCORE:
                return False
        except Exception:
            continue
    return True


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


def author_update_cache_time(force=False):
    # An HOUR, not the week-long ASIN-lookup default. The author record is the
    # one the API actively HEALS after first write -- the monthly scheduler
    # sweep and the operator's force=1 both fill a portrait/bio that arrived
    # late (Goodreads answered Roger Zelazny's very first lookup cache-cold and
    # empty; the record healed minutes later). Behind a week-long cache every
    # Refresh Metadata replayed the empty pre-heal body (measured live
    # 2026-07-26: "Fetching '.../authors/B000APXZHK...' from the HTTP cache"),
    # and nothing short of deleting Plex's cache dir could surface the bio. An
    # hour still absorbs a scan's per-album re-requests of the same artist.
    # An operator's explicit Refresh (force) bypasses even the hour -- the
    # same rule the search path follows (a manual Fix Match passes cache 0):
    # a human asking NOW must not be answered with the pre-heal body.
    if force or Prefs['dev_disable_http_cache']:
        return 0
    return CACHE_1HOUR


def book_update_cache_time(force=False):
    # The WEEK-long default stays for a scan: book records really are stable,
    # and a cold scan re-requests the same ASIN once per track, so the cache is
    # carrying real load there.
    #
    # What was missing is the operator bypass. The book item fetch passed no
    # cache_time at all, so a Refresh Metadata replayed whatever body was cached
    # up to a WEEK ago and no operator action could surface a corrected record.
    # Measured live 2026-07-30: after the API was fixed to parse a series for
    # B00HFW9SUE ("Legend of Drizzt #12") and confirmed serving it, Refresh
    # Metadata on every R. A. Salvatore book left the album on its old
    # folder-derived "Forgotten Realms Chronological, Book 12" -- the agent was
    # replaying a pre-fix cached body.
    #
    # Same rule the other two paths already follow: a manual Fix Match passes
    # cache 0, and an author Refresh bypasses its hour. A human asking NOW must
    # not be answered from before the fix they are refreshing to pick up.
    if force or Prefs['dev_disable_http_cache']:
        return 0
    return CACHE_1WEEK


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
    # WARN, not info: the shipped logging_level default is WARN, and info() is
    # suppressed there — so this banner, the ONLY positive proof the bundle
    # loaded, was never written at default prefs. A RestrictedPython violation
    # kills the plugin at compile time with no UI error, so "no banner" has to
    # mean "did not load" rather than "you have the wrong log level set".
    log.separator(
        msg=(
            "Incipit Audiobooks Agent v" + VERSION_NO
        ),
        log_level="warn"
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


# The per-pass memo above answers "did this already run", which is all most
# callers need. It is NOT enough for work whose RETURN VALUE gates a
# destructive action: select_local_cover reports whether the upload really
# holds the selection, and its caller prunes the operator's container copy of
# cover.jpg on the strength of that. Replaying a blanket True on sibling tracks
# inverted a stand-down and pruned the operator's only route back to their own
# art. So the verdict is remembered alongside, and replayed honestly.
verdict_memo = {}


def remember_verdict(tag, guid, token, value):
    """Record what (tag, guid, token) actually DECIDED, for sibling tracks."""
    if len(verdict_memo) > 512:
        verdict_memo.clear()
    verdict_memo[(tag, guid, token)] = (bool(value), time())


def recall_verdict(tag, guid, token, ttl):
    """
        The remembered verdict, or None when we do not have one.

        None is deliberately distinct from False so the caller chooses its own
        safe default rather than inheriting one. For the prune that default is
        False: not pruning leaves a duplicate tile, pruning wrongly destroys the
        operator's route back to their local art, and those costs are not close.
    """
    entry = verdict_memo.get((tag, guid, token))
    if entry and (time() - entry[1]) < ttl:
        return entry[0]
    return None


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


def byte_at(data, index):
    """
        The unsigned byte at `index`, in BOTH Python 2 and Python 3.

        py2 indexing a str yields a 1-char str and needs ord(); py3 indexing
        bytes yields an int and ord() raises. struct reads a one-byte SLICE,
        which is bytes under both, so it is the one form that cannot drift.

        Drift here is INVISIBLE, which is why this exists. Comparing bytes to a
        str literal is silently False, so under PYTHON 3 -- the test harness,
        not production -- image_dimensions returned None for every image and
        local_cover_is_portrait read that as "not portrait". Production is
        py2.7, where the two literal forms are the same type, so the old code
        worked there and the portrait deferral has fired live (measured on
        Extraction and The Ghost, 2026-07-25).

        The cost was therefore not a prod bug but an untestable one: the suite
        stayed green while exercising the mismatched comparison, so nothing in
        this file's image handling could be covered at all. Any NEW comparison
        written the old way re-opens that hole silently.
    """
    return struct.unpack('B', data[index:index + 1])[0]


def webp_dimensions(data):
    """
        (width, height) for the three WebP chunk layouts, or None.

        Three because the format has three: VP8 (lossy), VP8L (lossless) and
        VP8X (extended). Handling one is not enough -- the lossy and extended
        forms are both already SELECTED posters in this library (The Return of
        the King at 760x760, Cujo at 1080x1080).
    """
    chunk = data[12:16]
    if chunk == b'VP8 ' and len(data) >= 30:
        # Frame tag (3 bytes) then the sync code, which is checked rather than
        # skipped: a confidently WRONG size would pick a poster, and None only
        # keeps the current behaviour. Same reasoning as the strict JPEG walk.
        if data[23:26] != b'\x9d\x01\x2a':
            return None
        width, height = struct.unpack('<HH', data[26:30])
        # 14 bits each; the top two bits are the scaling hint, not the size.
        return (int(width) & 0x3FFF, int(height) & 0x3FFF)
    if chunk == b'VP8L' and len(data) >= 25:
        if data[20:21] != b'\x2f':
            return None
        # One signature byte, then width-1 and height-1 as 14 bits each.
        packed = struct.unpack('<I', data[21:25])[0]
        return (int(packed & 0x3FFF) + 1, int((packed >> 14) & 0x3FFF) + 1)
    if chunk == b'VP8X' and len(data) >= 30:
        # Canvas size as two 3-byte little-endian values, each stored minus 1.
        width = struct.unpack('<I', data[24:27] + b'\x00')[0]
        height = struct.unpack('<I', data[27:30] + b'\x00')[0]
        return (int(width) + 1, int(height) + 1)
    return None


def image_dimensions(data):
    """
        (width, height) for JPEG, PNG, BMP or WebP BYTES, or None when
        undeterminable.

        Bytes rather than a URL: the local cover.jpg is already in memory here,
        and measure_image (update_tools) fetches a URL and handles JPEG only.
        Every failure returns None so an unreadable image simply keeps the
        existing behaviour instead of changing which poster is used.

        BMP and WebP are here because "unmeasurable" is not a neutral answer:
        local_cover_is_portrait turns None into "not portrait", so a print
        jacket in an unhandled format is silently defaulted to and the book
        freezes on it. Three selected posters in this library were already
        non-JPEG when that was measured (2026-07-25).
    """
    try:
        if not data:
            return None
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            # IHDR width/height are the two big-endian longs at offset 16.
            width, height = struct.unpack('>II', data[16:24])
            return (int(width), int(height))
        if data[:2] == b'BM' and len(data) >= 26:
            # The DIB header SIZE at offset 14 says which layout follows, and
            # assuming one is the mistake this function exists to refuse. An
            # OS/2 BITMAPCOREHEADER keeps 16-bit dimensions at 18/20, so
            # reading BITMAPINFOHEADER's 32-bit pair at 18/22 turned a 510x680
            # jacket into (44564990, 1572865): not merely wrong but unbeatable,
            # since better_square_portrait ranks on min(width, height).
            header = struct.unpack('<I', data[14:18])[0]
            if header == 12:
                width, height = struct.unpack('<HH', data[18:22])
                return (int(width), int(height))
            if header >= 40:
                # BITMAPINFOHEADER and every later variant. Height is SIGNED:
                # negative means the rows are stored top-down, not that the
                # image has a negative size.
                width, height = struct.unpack('<ii', data[18:26])
                return (abs(int(width)), abs(int(height)))
            # A header size that is neither is a layout we cannot place.
            return None
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return webp_dimensions(data)
        if data[:2] != b'\xff\xd8':
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
            if data[index:index + 1] != b'\xff':
                return None
            marker = byte_at(data, index + 1)
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


def cover_mirror_mode():
    """
        'off' | 'seed' | 'curate' -- the DECLARED direction of truth for
        cover.jpg (the Lambda.bundle pattern: one pref says which side wins,
        and no code path infers it from ambiguous selection state).

        WHY THIS EXISTS (2026-07-26): a rebuild overwrote 92 hand-curated
        cover.jpg files. Both writers below inferred intent -- the mirror
        assumed "whatever is selected is what the operator wants on disk", the
        promote path assumed "the selection matches an offered online cover, so
        a person must have picked it". During a scan both inferences are false:
        the SCAN makes selections, thousands of them, with nobody choosing
        anything. There were no backups of that share.

          off    -- never write the media folder.
          seed   -- (default) write cover.jpg only where NONE exists. Safe
                    during any scan or rebuild; an existing file can never be
                    replaced, whatever gets selected.
          curate -- Plex is truth for this session: the operator is actively
                    picking art and wants picks captured to disk. The mode is
                    turned ON for a curation session and back off after.

        The asymmetry is the design: forgetting to enable curate costs one
        re-refresh after flipping it; the old design's forgetting cost
        unrecoverable curated art. Unknown/legacy values resolve to 'seed' for
        the same reason.

        THE MODE IS THE WHOLE PROTECTION -- do not add a rate/"storm" guard back.
        v1.3.125 shipped one (refuse a replacement once 6+ distinct albums were
        replaced inside 60s, on the theory that only a scan writes that fast).
        Removed in v1.3.127 because the incident data refutes it: the rebuild it
        was written to stop wrote at 1-15 files per MINUTE, median ~4, so it
        would have let more than half the damage through -- while the operator's
        real workflow (apply art to N books, then ONE artist-level refresh, up
        to ~40 covers for a series like Xanth) sits far above any threshold that
        would catch a scan. The two rates overlap, so rate cannot separate them,
        and in practice the guard only ever fired on legitimate curation.
        ZeroQI's Lambda.bundle -- the same export-to-media problem, years of use
        -- carries no rate guard either, for the same reason: the declared
        direction is sufficient and a heuristic is not.

        What DOES make an unexpected write detectable is the log: every write
        announces itself ('poster-backup: saved ->', 'promoted the picked'), and
        the scan logger alerts on those strings. Detection, not prevention, is
        the right job for something that cannot tell the two cases apart.
    """
    try:
        raw = Prefs['cover_mirror_mode']
    except Exception:
        return 'seed'
    if raw is None or raw is True or raw is False:
        return 'seed'
    text = str(raw).strip().lower()
    if text.startswith('off'):
        return 'off'
    if text.startswith('curation'):
        return 'curate'
    return 'seed'


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

        CURATE MODE ONLY (v1.3.125). This function's old safety argument --
        "fires only when the live selection is byte-identical to a cover WE
        offered, so it must be a deliberate pick" -- was FALSE during a scan:
        the scan itself selects offered covers, thousands of times, with nobody
        picking anything. On the 2026-07-26 rebuild that inference made this a
        writer of automatic selections over hand-curated cover.jpg files, with
        no matching log pattern in the damage sweep (its "promoted" line was
        not "poster-backup: saved"). Whether a pick happened is now DECLARED by
        cover_mirror_mode, never inferred from selection state.

        Remaining narrowing, within curate mode:
          - forced refresh + prefer_local_cover only;
          - only when the selection is byte-identical to a cover we offered (a
            custom upload never matches; backup_selected_poster captures it);
          - only when the pick DIFFERS from the current cover.jpg;
          - never the artist photo (the same poison check the backup uses);
    """
    if cover_mirror_mode() != 'curate':
        return
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
    #
    # FAIL CLOSED, through the shared helper -- the same contract both sibling
    # writers honour. This used to do its own HTTP.Request with a bare
    # `except: artist_bytes = None`, and selection_is_artist_art(None, ...)
    # returns False, so ONE 8-second timeout made the guard PASS and the write
    # proceed, logging success. That is exactly how the author photo ends up in
    # cover.jpg -- the shape that destroyed 92 curated covers on 2026-07-26.
    #
    # artist_poster_bytes returns (bytes, known) precisely so a caller can tell
    # "this item has no artist art" from "I could not tell", and its docstring
    # states the rule: callers that WRITE must refuse when known is False. It
    # also memoises, so this no longer re-downloads the artist photo per track.
    if parent_thumb:
        artist_bytes, known = artist_poster_bytes(helper.metadata.guid, tag, parent_thumb)
        if not known:
            log.error('%s: could not read the artist poster for the poison check '
                      '-- skipping this write to be safe', tag)
            return
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

        THE RULE (v1.3.125, after the 2026-07-26 rebuild overwrote 92 curated
        covers): the mirror's DIRECTION is declared by cover_mirror_mode, never
        inferred. In 'seed' (the default) an existing cover.jpg is never
        replaced -- only absent covers are written, so any scan is safe by
        construction. In 'curate' the old rule applies: cover.jpg faithfully
        mirrors the current selection, whoever chose it -- a hand-picked
        upload, the container's Audible art, or a switch between two agent
        covers -- because the operator has DECLARED that a person is choosing
        right now. WHO selected a given poster is still never guessed from
        selection state; that inference is what destroyed the 92.

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
    mode = cover_mirror_mode()
    if mode == 'off':
        return
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
    # SEED MODE: an existing file is never replaced, full stop -- and that is
    # decidable right here, before paying for the selected poster's bytes on
    # every track. Whether the selection matches or differs, the outcome is
    # identical (no write), so the download would be pure cost. mark_done keeps
    # the per-track collapse: this is a definite outcome for this selection.
    if existing and mode != 'curate':
        mark_done('poster-backup', helper.metadata.guid, thumb)
        log.info('incipit poster-backup: cover.jpg exists and mode is seed-only '
                 '-- not replacing it (switch the cover mirror to Curation to '
                 'capture picks over existing files)')
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
    # cover (see RESELECT_PADS): identical pixels, different bytes. Writing it
    # back would silently grow the operator's cover.jpg by the pad -- breaking
    # any byte/sha reconciliation against the curated-cover manifest -- and
    # would make the padded copy the new "plain" base, so every later deselect
    # mints another pad level instead of stopping at the documented boundary.
    #
    # EVERY generation, not just v1. This tested `existing + RESELECT_PAD`
    # alone, so once v1.3.190 made the pad a FAMILY a v2/v3-padded selection
    # failed the test and fell through to the write -- proved in curate mode
    # with a v2 selection, a 52-byte write ending in RESELECT_PADS[1], i.e.
    # exactly the byte growth this guard exists to prevent. Through
    # strip_one_pad, the shipped family reader same_image already uses, so a
    # fourth generation cannot teach one of them and not the other. The
    # length test makes the equal case explicit -- it returned at the
    # unchanged-skip above, and a bare strip comparison would otherwise read
    # an unpadded identical selection as "padded".
    if existing and len(selected) > len(existing) \
            and strip_one_pad(selected) == existing:
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
    # or inherited artist art (refused by the poison guard above).
    #
    # ONE automatic case is now genuinely unguarded, and it is deliberate. Until
    # v1.3.121 a third leg read "or a portrait book's online default, refused by
    # the portrait branch above"; that branch is gone, so a Fix Match that
    # re-seats the online default on a portrait book IS mirrored to disk with no
    # human act. That is the point -- see the docstring: the file it would have
    # protected is one the agent measured as a print jacket and already refused
    # to display. Do not re-derive the old reasoning from this paragraph.
    #
    # Whoever chose the selection, it is what Plex shows, so it is what the
    # sidecar mirrors -- in CURATE mode, where the operator has declared a
    # person is choosing. The byte checks above already mean this only fires on
    # a real change.
    #
    if write_cover_sidecar(cover_path, selected):
        mark_done('poster-backup', helper.metadata.guid, thumb)
        log.warn('incipit poster-backup: saved -> %s (%s bytes)', cover_path, len(selected))


PMS = 'http://127.0.0.1:32400'

# Deterministic suffixes for the padded re-upload trick (see
# upload_and_select_poster). Decoders ignore trailing bytes after a JPEG EOI /
# PNG IEND, so original + suffix renders identically but is NEW content to
# Plex's content-addressed store -- and POSTing NEW content both uploads and
# selects, the agent's only re-select lever (its PUT is downgraded to GET).
# DETERMINISTIC on purpose: sha(padded) is then predictable, so later passes
# recognize the padded upload as ours/selected instead of padding again and
# accumulating a new upload per refresh.
#
# A FAMILY, not one level, since v1.3.190. The 2026-08-08 rebuild wedged 83%
# of 1,607 albums "out of re-select levers": the scan burst left cover.jpg
# AND its v1 pad as de-selected uploads (the select POST raced or failed),
# and with both byte-forms burned the item was unfixable by ANY refresh --
# the sandbox cannot select an existing upload, so the only remedy was an
# external API sweep. Generations turn that dead end into a retry: each
# failed round burns one form, the next round mints the next, and the cap
# bounds store growth at len(RESELECT_PADS) extra copies per image ever.
# Ownership, convergence and poison checks recognize the WHOLE family via
# pad_family_shas/same_image, or a v2-selected upload would read as foreign
# and re-select loops would mint runaway pads.
RESELECT_PADS = [
    b'\nincipit-reselect-v1',
    b'\nincipit-reselect-v2',
    b'\nincipit-reselect-v3',
]
# The v1 name survives: albums touched before v1.3.190 carry v1-padded
# uploads, and the deploy-gate/test anchors reference it.
RESELECT_PAD = RESELECT_PADS[0]


def padded_variants(image_bytes):
    """(sha_original, sha_v1_padded, v1_padded_bytes) for ownership/skip checks.

    The v1-only historical shape; family-aware callers use pad_family_shas.
    DELEGATES to it rather than recomputing: every production call site moved
    to pad_family_shas at v1.3.190, leaving this exercised only by tests -- so
    two independent implementations of the same rule agreed only by accident,
    and the one the tests pin was the one nothing ran.
    """
    fam = pad_family_shas(image_bytes)
    return fam[0][0], fam[1][0], fam[1][1]


def pad_family_shas(image_bytes):
    """[(sha, bytes)] for the plain image and EVERY pad generation, in mint
    order. Index 0 is always the plain form."""
    out = [(hashlib.sha1(image_bytes).hexdigest(), image_bytes)]
    for pad in RESELECT_PADS:
        padded = image_bytes + pad
        out.append((hashlib.sha1(padded).hexdigest(), padded))
    return out


def strip_one_pad(image_bytes):
    """`image_bytes` with ONE trailing family pad removed, if one is present.

    One level only, deliberately: a doubly-padded blob means something
    upstream is re-padding and must NOT silently read as the original
    (the boundary the pre-family same_image already enforced)."""
    for pad in RESELECT_PADS:
        if image_bytes.endswith(pad):
            return image_bytes[:-len(pad)]
    return image_bytes


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


def distinct_rating_keys(text):
    """
        The DISTINCT ratingKeys in a /library/all?guid= response, in order.

        ONE spelling, because two questions are asked of the same response and
        they must not disagree: "did this guid match more than one item" (a
        rebuild's duplicate sections) and "did it match ANY item at all". The
        second used to have no spelling, so a ZERO-result response fell through
        the ambiguity check and was read as a clean resolution -- see
        artist_poster_bytes, where that turned "I could not find the item" into
        the positive fact "this artist has no poster".
    """
    distinct = []
    for k in re.findall(r'ratingKey="([0-9]+)"', text):
        if k not in distinct:
            distinct.append(k)
    return distinct


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
    distinct = distinct_rating_keys(text)
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


def thumb_field_locked(rk, tag):
    """
        (a_human_chose_this_poster, known) for the item with `rk`.

        Plex stamps <Field locked="1" name="thumb"/> on the item whenever a
        poster is selected by hand -- a UI click or the /poster?url= API --
        and leaves the item Field-less for container-scan defaults. Measured
        on this deployment 2026-08-08: an API pick locks the field, a
        never-touched artist carries no Field element at all, and the lock
        survives plain and forced refreshes.

        This is exactly the click-vs-default signal the ownership rules in
        converge_author_art declared Plex never exposes -- the premise under
        which the fit direction was allowed to override a click-pick between
        the agent's own two provider images. It does exist, so that class is
        no longer condemned to ambiguity. Operator directive 2026-08-08: a
        human selection is inviolable, whatever key form it points at.

        TWO values, the shape this docstring already claimed to follow and did
        not: it returned a BARE BOOL that failed closed to True, so no caller
        could tell "a human chose this" from "I could not read it". Both
        answers stand the selection down, but they must not be RECORDED alike
        -- select_local_cover took the unreadable answer down its destructive
        branch (mark_done + remember_verdict(False)), so one timed-out
        localhost GET during a scan burst made every sibling track of the album
        replay False and suppressed the container-twin prune for the whole
        album. The poison guard ten lines below it already states the opposite
        discipline: "a transient failure must retry, not be suppressed."

        FAIL-CLOSED on the VALUE: an unreadable answer still reports True
        ("assume a human chose it"), because every caller is about to CHANGE
        the selection and the poison-guard rule holds -- never overwrite what
        you cannot prove is yours. `known` is False ONLY for a failure, and it
        is what says whether the stand-down may be memoised (the same
        discipline as artist_poster_bytes' `known` flag).
    """
    try:
        url = PMS + '/library/metadata/' + rk
        text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
        return (bool(re.search(r'<Field[^>]*name="thumb"', text)), True)
    except Exception as e:
        log.error('%s: could not read the thumb field lock (%s)', tag, e)
        return (True, False)


def same_image(first, second):
    """
        True when the two blobs are the SAME picture -- byte-identical, or one
        is our own RESELECT_PAD copy of the other. SYMMETRIC: either side may
        be the padded one, because picture identity does not depend on
        argument order and callers legitimately pass the copies either way
        round (the 2026-07-28 review swapped an argument pair and exposed
        one-directional matching that had been latent since the pad existed).

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
    # Family-aware since v1.3.190: strip ONE trailing pad (any generation)
    # from each side and compare the bases. Symmetric by construction, and
    # the one-level strip preserves the double-padding boundary: base of
    # image+PAD+PAD is image+PAD, which never equals image.
    try:
        return strip_one_pad(first) == strip_one_pad(second)
    except Exception:
        return False


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

        DELIBERATELY not routed through the sweep memo below: a selection read
        feeds upload/skip DECISIONS, and our own metadata:// key serves
        MUTABLE bytes -- the same key answers with new content the moment the
        pass rewrites the offer (a replaced cover.jpg). A stale read here can
        skip a required upload, the destructive direction; the sweep's
        comparisons are advisory and fail open, so only they may be memoized.
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


# Container-poster bytes fetched by the duplicate SWEEP, shared by every leg
# that walks the same container: the mirror leg and the online leg each run
# duplicate_shown_detail over the SAME keys, so one track's pass downloaded
# every non-incipit poster twice. Keyed (rk, key); value (payload, stamp)
# where payload is the bytes, or POSTER_FETCH_FAILED when the fetch errored.
# Failures are memoized ON PURPOSE: within one pass the very next leg would
# just repeat the doomed round-trip (8s timeout each), and the TTL clears the
# verdict soon enough that the next pass retries. Staleness is bounded by the
# fail-open rails: the worst a stale sweep byte can do is show one extra tile
# for a pass (prunes additionally require byte identity against CURRENT
# image_bytes, so a stale entry cannot delete anything it shouldn't).
POSTER_BYTES_MEMO = {}
# Same horizon as ALBUM_COVER_TTL: long enough to span one album's sweep
# legs and sibling tracks, short enough that the next refresh re-reads.
POSTER_BYTES_TTL = 60
# ~10 posters x a few hundred KB is the realistic ceiling; the cap only
# guards a runaway container, same clear-when-full idiom as PERCEPTUAL_MEMO.
POSTER_BYTES_MEMO_MAX = 64
POSTER_FETCH_FAILED = object()


def poster_file_bytes(rk, key, tag):
    """
        Bytes of ANY container poster (by its ratingKey form) via the local
        /file endpoint, or None. The generalization of selected_poster_bytes'
        fetch, for callers comparing against posters that are NOT the
        selection (the cross-source duplicate check) -- memoized per
        (rk, key) for the pass, because both sweep legs walk the same keys.
    """
    if not rk or not key:
        return None
    memo_key = (rk, key)
    # .get then use, never `in` then `[]` -- same concurrent-clear race the
    # perceptual memo hit (v1.3.153).
    hit = POSTER_BYTES_MEMO.get(memo_key)
    if hit is not None and (time() - hit[1]) < POSTER_BYTES_TTL:
        payload = hit[0]
        return None if payload is POSTER_FETCH_FAILED else payload
    try:
        url = (PMS + '/library/metadata/' + rk + '/file?url='
               + urllib.quote(key, ''))
        data = HTTP.Request(url, timeout=8, cacheTime=0).content
    except Exception as e:
        log.error('%s: could not read container poster %s (%s)', tag, key, e)
        data = None
    if len(POSTER_BYTES_MEMO) >= POSTER_BYTES_MEMO_MAX:
        POSTER_BYTES_MEMO.clear()
    POSTER_BYTES_MEMO[memo_key] = (
        POSTER_FETCH_FAILED if data is None else data, time())
    return data


def own_container_key(dict_key):
    """
        The container ratingKey form Plex assigns to OUR poster offered under
        `dict_key` -- the agent id plus sha1 of the key STRING (never the image
        bytes; proven live in the v1.3.119 work). This is how a caller asks
        "is the selection the copy I manage?" without fetching anything.
    """
    # NO `bytes` builtin here: the sandbox's whitelist omits that NAME (proven
    # live 2026-07-26 -- an isinstance check against it killed every album
    # update with "global name 'bytes' is not defined" while py_compile and
    # the py3 harness both passed; guarded by TestSandboxBuiltinGuards).
    # encode() covers every real input instead: py2 str (ascii keys
    # round-trip), py2 unicode, py3 str; anything without .encode (the py3
    # harness's byte strings) is already hashable as-is. ValueError, not the
    # Unicode error names: those have no whitelist precedent either, and py2
    # evaluates except tuples LAZILY -- an unwhitelisted name in the tuple is
    # a NameError at the exact moment the fallback should fire. Unicode
    # decode/encode errors both subclass ValueError, which json_decode
    # already proves whitelisted.
    try:
        key_bytes = dict_key.encode('utf-8')
    except (AttributeError, ValueError):
        key_bytes = dict_key
    return ('metadata://posters/com.plexapp.agents.incipit_'
            + hashlib.sha1(key_bytes).hexdigest())


# Cross-source comparisons are bounded: containers are small (2-10 posters),
# but a runaway one must not turn a refresh into a fetch storm.
DUPLICATE_CHECK_MAX_FETCHES = 6


# Perceptual verdicts per unordered byte-pair, for the process lifetime: the
# same two blobs are re-compared on every refresh pass. Only DEFINITIVE
# verdicts are stored (similar / dissimilar / undecodable-None); transient
# network failures are not, so one blip cannot pin a pair to "no verdict"
# until the next Plex restart.
PERCEPTUAL_MEMO = {}
PERCEPTUAL_MEMO_MAX = 512
# Sentinel for the memo lookup: a memoized None (undecodable) is a real
# verdict and must not read as a miss. Module-level so it is one object, and
# deliberately not underscore-prefixed (the sandbox rejects those outright).
PERCEPTUAL_MISSING = object()
# Per-IMAGE signatures the api mints on every full verdict (api c49ed61):
# keyed by the image's byte sha, replayable in place of the base64 body. The
# verdict memo above only helps a REPEATED pair; the dedupe sweep compares
# one image against up to six container posters, so each poster's bytes were
# re-uploaded once per PAIR. With signatures each image uploads once per
# process and rides as a ~600-byte token after that. An older api that
# returns no signatures simply never populates this and every call stays
# bytes -- no version coupling.
PERCEPTUAL_SIG_MEMO = {}
PERCEPTUAL_SIG_MEMO_MAX = 256


def perceptual_consult_request(base, body):
    """One POST /images/similar round-trip, parsed. Raises on any failure."""
    return json.loads(HTTP.Request(
        base.rstrip('/') + '/images/similar',
        data=json.dumps(body),
        headers={'Content-Type': 'application/json'},
        timeout=10, cacheTime=0
    ).content)


def images_similar_via_api(first, second, tag):
    """
        True when the api judges `first` and `second` to be the SAME picture
        in different bytes (a re-encode or resize), False when genuinely
        different, None when no verdict is available (api_base_url unset,
        network failure, undecodable image). Callers must treat None exactly
        like False -- fail-open, because a duplicate tile is cosmetic and a
        hidden poster option is not.

        Exists because byte identity misses re-encodes: census 2026-07-27
        found four artists showing a hand-uploaded author photo next to our
        byte-different copy of the same picture, invisible to same_image, so
        no refresh could ever heal them.
    """
    try:
        base = Prefs['api_base_url']
    except Exception:
        return None
    if not base or not first or not second:
        return None
    # str() both sides BEFORE hashing: Plex hands back `unicode` for anything
    # it read as text (a CDN interstitial served 200 text/html is the live
    # case), and py2 sha1 on non-ASCII unicode raises UnicodeEncodeError --
    # uncaught here, that killed the whole album update mid-write. Same
    # family as the v1.3.139 Unicode fix; the answer is a no-verdict, which
    # every caller already treats as "offer as always".
    # TEXT, not image bytes: Plex decodes anything it read as text/* into
    # `unicode`, and py2 sha1 on non-ASCII unicode raises UnicodeEncodeError
    # -- uncaught, that killed the album update mid-write. (The `bytes`
    # builtin is unavailable in the sandbox; `unicode` is the test this file
    # uses everywhere else, and it is correct under the py3 harness too.)
    if isinstance(first, unicode) or isinstance(second, unicode):
        log.info('%s: perceptual consult skipped (not image bytes)', tag)
        return None
    try:
        key_a = hashlib.sha1(first).hexdigest()
        key_b = hashlib.sha1(second).hexdigest()
    except Exception as e:
        log.info('%s: perceptual consult skipped (unhashable: %s)', tag, e)
        return None
    memo_key = key_a + key_b if key_a < key_b else key_b + key_a
    # .get with a sentinel, never `in` then `[]`: a concurrent agent thread
    # can clear the memo between those two statements (KeyError out of an
    # update). MISSING is a unique object, so a memoized None is still a hit.
    cached = PERCEPTUAL_MEMO.get(memo_key, PERCEPTUAL_MISSING)
    if cached is not PERCEPTUAL_MISSING:
        return cached
    try:
        # Send a cached per-image signature in place of the bytes when we
        # have one; the api compares signatures and bytes interchangeably.
        sig_a = PERCEPTUAL_SIG_MEMO.get(key_a)
        sig_b = PERCEPTUAL_SIG_MEMO.get(key_b)
        body = {}
        if sig_a:
            body['aSig'] = sig_a
        else:
            body['a'] = String.Base64Encode(first)
        if sig_b:
            body['bSig'] = sig_b
        else:
            body['b'] = String.Base64Encode(second)
        answer = perceptual_consult_request(base, body)
        # Inside the try on purpose: a proxy answering valid non-object JSON
        # (null, [], "ok") made `.get` raise AttributeError straight through
        # update(), turning the documented fail-open into a fail-crash.
        if not isinstance(answer, dict):
            raise ValueError('non-object answer %r' % (answer,))
        if answer.get('staleSig') and (sig_a or sig_b):
            # A cached signature no longer decodes (the api retuned its grid
            # geometry across a deploy). Forget both sides and replay ONCE
            # with full bytes -- that response re-mints fresh signatures.
            # Guarded on having SENT a sig, so a server oddly claiming
            # staleSig on a bytes-only call cannot loop the replay.
            PERCEPTUAL_SIG_MEMO.pop(key_a, None)
            PERCEPTUAL_SIG_MEMO.pop(key_b, None)
            log.info('%s: perceptual signatures stale, replaying with bytes',
                     tag)
            answer = perceptual_consult_request(base, {
                'a': String.Base64Encode(first),
                'b': String.Base64Encode(second),
            })
            if not isinstance(answer, dict):
                raise ValueError('non-object answer %r' % (answer,))
        undecodable = answer.get('undecodable')
        similar = answer.get('similar')
        # Bank the minted signatures for both sides. isinstance guards a
        # malformed field; an older api simply has none to bank.
        for side_key, field in ((key_a, 'aSig'), (key_b, 'bSig')):
            sig = answer.get(field)
            if isinstance(sig, (str, unicode)) and sig:
                if len(PERCEPTUAL_SIG_MEMO) >= PERCEPTUAL_SIG_MEMO_MAX:
                    PERCEPTUAL_SIG_MEMO.clear()
                PERCEPTUAL_SIG_MEMO[side_key] = sig
    except Exception as e:
        log.info('%s: perceptual consult unavailable (%s)', tag, e)
        return None
    verdict = None if undecodable else bool(similar)
    if len(PERCEPTUAL_MEMO) >= PERCEPTUAL_MEMO_MAX:
        PERCEPTUAL_MEMO.clear()
    PERCEPTUAL_MEMO[memo_key] = verdict
    return verdict


def perceptual_dedupe_enabled():
    """
        The operator's one switch for perceptual (as opposed to byte-exact)
        duplicate suppression. Gating only ONE of the two legs -- which is
        what v1.3.151 shipped -- let the cross-source leg reach an ungated
        consult, so unchecking the box did not restore variant covers, the
        single thing its label promises. Fails CLOSED (no perceptual
        suppression) so an unreadable pref can never hide art.
    """
    try:
        return bool(Prefs['online_perceptual_dedupe'])
    except Exception:
        return False


def same_picture(withheld, survivor, tag):
    """
        (same, byte_exact) for two image blobs: byte identity first, then the
        pref-gated perceptual consult. ONE primitive so the gate, and any
        future widening, cannot drift between call sites the way the byte
        check already had to be fixed twice.

        ARGUMENT ORDER IS PART OF THE CONTRACT: `withheld` is the tile that
        disappears on a True verdict, `survivor` is the copy left on display.
        The resolution rule below is asymmetric, so the names say which is
        which -- they were `first`/`second` and two of the three call sites
        passed them the other way round, running the keep-the-better-copy
        guard backwards (found by the 2026-07-28 review, five finders).

        `byte_exact` is the caller's licence to PRUNE: identical bytes prove
        the picture is on display elsewhere, so dropping our entry loses
        nothing. A merely SIMILAR verdict does not prove that -- it may be a
        variant -- so it may withhold an offer but must never delete.
    """
    if not withheld or not survivor:
        return False, False
    if same_image(withheld, survivor):
        return True, True
    if not perceptual_dedupe_enabled():
        return False, False
    if not aspect_could_match(withheld, survivor):
        return False, False
    if images_similar_via_api(withheld, survivor, tag) is not True:
        return False, False
    # The same picture -- but the agent can only ever withhold ITS OWN tile,
    # so whichever copy is left standing is the other source's. Keep the
    # BETTER one: Men at Arms held our 739KB poster next to Local Media
    # Assets' 67KB re-encode of the same design (2026-07-28), and withholding
    # unconditionally would have left the operator with only the small one --
    # a loss that widening the perceptual threshold makes MORE likely, not
    # less. A duplicate tile is cosmetic; losing the good copy is not.
    if not other_copy_is_good_enough(withheld, survivor):
        log.info('%s: keeping our higher-resolution copy of this picture', tag)
        return False, False
    return True, False


def other_copy_is_good_enough(ours, theirs):
    """
        True unless THEIR copy is clearly lower resolution than ours.

        Unknown dimensions fail toward withholding, which is the pre-existing
        behaviour -- an unparsed header must not silently disable dedupe. The
        10% margin keeps an ordinary re-encode at the same nominal size from
        reading as a downgrade.
    """
    try:
        our_dims = image_dimensions(ours)
        their_dims = image_dimensions(theirs)
    except Exception:
        return True
    if not our_dims or not their_dims:
        return True
    our_pixels = our_dims[0] * our_dims[1]
    their_pixels = their_dims[0] * their_dims[1]
    if not our_pixels or not their_pixels:
        return True
    return their_pixels >= our_pixels * 0.9


def aspect_could_match(first, second):
    """
        False only when both blobs' shapes are KNOWN and clearly different.

        A re-encode or resize preserves aspect ratio, so a shape mismatch
        rules out "the same picture" without asking anyone -- and
        image_dimensions reads it from the header in microseconds with no
        network, where the consult costs a multi-megabyte round trip. That
        rejects the whole square-cover-vs-portrait-photo class up front.
        Unknown dimensions fail toward ASKING, so an unparsed header can
        never silently disable dedupe.
    """
    try:
        first_dims = image_dimensions(first)
        second_dims = image_dimensions(second)
    except Exception:
        return True
    if not first_dims or not second_dims:
        return True
    if not first_dims[1] or not second_dims[1]:
        return True
    first_ratio = float(first_dims[0]) / first_dims[1]
    second_ratio = float(second_dims[0]) / second_dims[1]
    wider = max(first_ratio, second_ratio)
    narrower = min(first_ratio, second_ratio)
    if narrower <= 0:
        return True
    # 6% covers rounding and an odd pixel of crop; the classes this rejects
    # (square vs 2:3 portrait) differ by 50% or more.
    return (wider / narrower) <= 1.06


def online_prune_allowed(state, thumb_key):
    """
        False when the ONLINE cover is itself the selection -- pruning it is
        the picked-poster-evaporates failure. The author flow has carried
        this rail since v1.3.144 (`sel_key != own_container_key(...)`); the
        album keep-list never did, and the comment claiming safety "by
        construction" reasoned about comparison SOURCES, which says nothing
        about which key is SELECTED. No state means no evidence of a
        selection, which is the pre-rail behaviour.
    """
    if not state:
        return True
    selected_key = state[1]
    if not selected_key:
        return True
    return selected_key != own_container_key(thumb_key)


def local_prune_allowed(state, local_key):
    """
        False when the LOCAL mirror entry is itself the selection. Same rail
        as online_prune_allowed, for the branch that had none.
    """
    return online_prune_allowed(state, local_key)


def mirror_withheld(state, image_bytes, own_dict_key, tag):
    """
        (withheld, byte_exact) for the local mirror -- `duplicate_shown_elsewhere`
        plus the evidence grade behind the verdict, so the caller can withhold
        the offer on similarity but only PRUNE the operator's curated cover.jpg
        on byte identity.
    """
    return duplicate_shown_detail(state, image_bytes, own_dict_key, tag)


def cover_keep_list(thumb_key, local_key, thumb_present, local_present,
                    online_redundant, online_byte_exact, online_prune_ok,
                    mirror_skipped, mirror_byte_exact, local_uploaded=False,
                    alternate_keys=None):
    """
        The membership list for `validate_keys` -- which entries of OUR
        namespace survive this pass.

        A function, not an inline block, because the 2026-07-28 mutation
        sweep proved every guard here was unenforced: the only tests were
        `assertIn('foo(', source)`, which cannot distinguish a live guard
        from a discarded return value. Extracting the decision makes it
        testable behaviourally.

        Two rules, both learned the expensive way:
          * PRUNE only on BYTE identity. Identical bytes prove the picture is
            displayed elsewhere; a perceptual verdict only suggests it, and a
            variant deleted here never returns because the same bytes
            re-derive the same verdict every pass.
          * NEVER prune the selection (`online_prune_ok`), which is the
            picked-poster-evaporates failure.

        `local_uploaded` is the third case, added 2026-07-29: OUR OWN upload now
        holds the selection and carries cover.jpg's exact bytes, so our CONTAINER
        copy of the same file is a twin tile. Measured on a cold library with the
        Incipit agent bound (section 54): 33 of 33 albums came out at 3 tiles / 1
        duplicate, the pair always `(upload) + com.plexapp.agents.incipit`. Both
        rules above still hold -- the prune is on exact byte identity (our own
        bytes) and the selection is the UPLOAD, not this entry. The agent cannot
        delete an upload (PUT/DELETE are downgraded), so this container entry is
        the only removable copy, and the right one.
    """
    keep = [thumb_key] if thumb_present else []
    if online_redundant and online_byte_exact and online_prune_ok:
        keep = []
    if local_present and not (mirror_skipped and mirror_byte_exact) \
            and not local_uploaded:
        keep.append(local_key)
    # ALTERNATE-MARKETPLACE covers (the api's `imageAlternates`) are extra ART
    # ONLY -- a different Audible marketplace's cover for the same recording.
    # They must be listed here or validate_keys withholds them the moment they
    # are offered: added and silently pruned in one pass, which looks exactly
    # like the feature not working.
    #
    # Appended LAST and never displacing anything: the default poster and the
    # local cover keep their positions and their meaning. Their retention is
    # also independent of the online prune above -- the online copy being a
    # byte-identical twin of cover.jpg says nothing about a DIFFERENT
    # marketplace's art.
    for key in (alternate_keys or []):
        if key and key not in keep:
            keep.append(key)
    return keep


def alternate_cover_acceptable(data):
    """
        Whether these bytes earn a bonus poster tile.

        Judged on the PIXELS, not on where the url pointed. Measured live
        2026-08-01 across six alternates the api offered: four were genuine
        squares (two at 2400x2400), one was a 973x1500 PORTRAIT print jacket
        from a Hardcover edition that carries `audio_seconds` while its image is
        the print cover, and one OverDrive url returned HTML rather than an
        image. A runtime proves the EDITION is audio; it says nothing about the
        PICTURE, and a url shape says even less -- Hardcover serves square art
        from /edition/ and portrait from /editions/.

        The bundle already fetches these bytes before offering, so this costs
        nothing and cannot be fooled by a url.

        NOTE the polarity is the OPPOSITE of local_cover_is_portrait, which
        keeps an unmeasurable local file: the operator's own art gets the
        benefit of the doubt, a third party's bonus tile does not. Unmeasurable
        means refused.
        @param data the fetched image bytes
        @returns True when the image is confidently square-ish art
    """
    if not data:
        return False
    dims = image_dimensions(data)
    if not dims:
        return False
    width, height = dims
    if width <= 0 or height <= 0:
        return False
    # Same band the square-cover work uses: real audiobook art is not always
    # exactly 1:1, but a print jacket is nowhere near.
    shorter = width
    longer = height
    if width > height:
        shorter = height
        longer = width
    return (float(shorter) / float(longer)) >= 0.9


# An ACCEPTED alternate needs no memo: it lands in the container, and the
# membership check below skips it on every later track (cover_keep_list carries
# the key through validate_keys, so it survives the prune). A REFUSED one has
# no such trace -- the loop just continues, writing nothing -- so update()'s
# per-track fanout re-fetched and re-decoded the same dead url once per track,
# forever. Refusals are not rare: the live measurement behind
# alternate_cover_acceptable found 2 of 6 offered alternates unusable (a
# portrait print jacket and an OverDrive url serving HTML). On a 27-part book
# that is 26 redundant fetches per pass, each on a THIRD-PARTY host and so
# carrying the framework's full 1s pacing plus a download and a decode.
#
# Keyed on the url alone, not the guid: "the bytes at this url are not square
# art" is a property of the url, and the same dead url is offered across books.
#
# THROUGH verdict_memo, not a bespoke dict. This pair hand-rolled a bounded TTL
# memo -- down to a verbatim copy of the `if len(X) > 512: X.clear()` idiom --
# that the generic remember_verdict/recall_verdict above already are. The
# public helper names stay, so call sites and tests read the same.
#
# verdict_memo and NOT should_run/mark_done: recent_work_memo has no size bound
# at all, and urls are a large keyspace.
ALTERNATE_REFUSAL_TTL = 600
ALTERNATE_REFUSAL_TAG = 'incipit alt-refusal'


def alternate_refused_recently(url):
    """True when this url was fetched and rejected within the TTL."""
    return bool(recall_verdict(ALTERNATE_REFUSAL_TAG, url, 'refused',
                               ALTERNATE_REFUSAL_TTL))


def remember_alternate_refusal(url):
    """Record that this url did not yield a usable cover."""
    remember_verdict(ALTERNATE_REFUSAL_TAG, url, 'refused', True)


def alternate_already_on_display(data, shown, url):
    """
        True when this alternate is the SAME PICTURE as one already offered.

        The feature's own promise is "real choice rather than duplicate tiles"
        -- so an alternate that merely repeats a picture already in the
        container is the one case it must not add. Measured live 2026-08-12 on
        Lamb (prod rk 740608): the container held our 500x500 cover and, from a
        near-tie row scoring 99, the SAME artwork at 1024x1024. The api judged
        them distance 2 (similar); the genuinely different OverDrive jacket
        scored 26. So the verdict this needs already exists -- it simply was
        never consulted on this path.

        NOT same_picture(): that helper is asymmetric on purpose, keeping OUR
        copy when the other tile is a worse re-encode, because there the other
        tile belongs to a different agent and withholding ours can leave the
        operator with only the bad one. Here BOTH tiles are ours and the one
        being kept is the DEFAULT, so nothing disappears from display -- the
        only thing lost is a second copy of a picture already shown. Reusing
        the asymmetry would have kept the Lamb twin precisely because the
        duplicate was the higher-resolution one.

        Fails OPEN, like every other consult here: no verdict means offer it.
        A duplicate tile is cosmetic; a hidden cover option is not.
        @param data the candidate alternate's bytes
        @param shown iterable of byte blobs already on display
        @param url the alternate's url, for logging
        @returns True when the alternate should be skipped
    """
    for other in (shown or []):
        if not other:
            continue
        # Byte-identical is free and needs no api, no pref and no network.
        if same_image(data, other):
            log.info('incipit cover: alternate is the picture already shown '
                     '-- not offering a second copy (%s)', url)
            return True
        if not perceptual_dedupe_enabled():
            continue
        if not aspect_could_match(data, other):
            continue
        if images_similar_via_api(data, other, 'incipit cover-alt') is True:
            log.info('incipit cover: alternate is the same picture at a '
                     'different size -- not offering a twin tile (%s)', url)
            return True
    return False


def offer_alternate_covers(helper, sort_order=3, shown=None):
    """
        Offer the api's `imageAlternates` as extra pickable posters, returning
        the keys actually added.

        A DIFFERENT Audible marketplace's art for the same recording. The api
        gates them on the narrator sets matching (one ASIN can front different
        recordings in different marketplaces) and measured 7 of 15 both-region
        ASINs carrying genuinely different art, so this is real choice rather
        than duplicate tiles.

        EXTRA CHOICE ONLY -- sort_order well below the default and the local
        cover, so nothing here can become the selection. The returned keys must
        be handed to cover_keep_list or validate_keys withholds them the instant
        they are offered: added and pruned in one pass, which is
        indistinguishable from the feature not working.

        Skips a url already in the container (a re-offer costs a fetch and
        changes nothing) and swallows every failure -- spare art is never worth
        failing an update for.
        @param helper the update helper carrying thumb_alternates
        @param sort_order priority for the offered posters
        @returns list of keys added to the container
    """
    added = []
    # NOT getattr(): the sandbox blocks it (the guard suite caught that, as it
    # caught hasattr in the parser). thumb_alternates is initialised on the
    # helper, and a missing one is an AttributeError this try already covers.
    try:
        alternates = helper.thumb_alternates or []
    except Exception:
        return added
    # Anything to judge AGAINST? With nothing on display the twin question
    # cannot be asked, so the old free path stands.
    # What a candidate is judged against: the images already on display, PLUS
    # the alternates accepted earlier in THIS pass.
    #
    # Without the second half, two alternates that are the same picture as each
    # other both pass -- each is compared only to the cover, not to its
    # predecessor. Measured on .99 across 30 albums (v1.3.210): 29 duplicate
    # tiles went, and exactly one pair survived, on The Heroes -- two of its six
    # alternates being the same picture at distance 0, both offered by us.
    judged = [s for s in (shown or []) if s]
    for url in alternates:
        already_offered = url in helper.metadata.posters
        # THE SHORT-CIRCUIT IS NOW CONDITIONAL, and that is the whole fix.
        #
        # It used to be unconditional -- "a re-offer costs a fetch and changes
        # nothing" -- which was true right up until v1.3.208 made re-offering a
        # DECISION. Measured on .99 (v1.3.209 diagnostic build, Gravesong):
        # "3 alternate(s), 2 shown image(s) to judge against" followed by
        # "ALREADY in the container -- kept WITHOUT being judged". So the twin
        # suppression only ever reached alternates being offered for the FIRST
        # time, and every twin already in a container was re-added forever.
        # Lamb only looked fixed because a cross-match had rebuilt its
        # container from scratch.
        #
        # An already-offered alternate is therefore re-fetched and re-judged
        # when there is something to judge it against. That fetch is the cost
        # the short-circuit existed to avoid, so it is bounded three ways: the
        # refusal memo still skips known-bad urls without a fetch, the
        # perceptual verdicts are memoised, and image CDNs now pace at 0.25s
        # rather than 1s (v1.3.207).
        # `judged` GROWS as the loop runs, so this asks the current question,
        # not the one that was true at entry.
        if already_offered and not judged:
            added.append(url)
            continue
        # A refusal leaves no trace in the container, so without this the
        # sibling tracks re-pay the whole fetch+decode to reach the same no.
        if alternate_refused_recently(url):
            continue
        # THE VERDICT, and nothing else, inside this try. Judge the PIXELS: the
        # api can only vouch for the record; a Hardcover edition with a runtime
        # can still ship the print jacket, and a dead CDN url returns HTML. See
        # alternate_cover_acceptable.
        data = None
        acceptable = False
        try:
            data = fetch_url_bytes(url)
            acceptable = bool(data) and alternate_cover_acceptable(data)
        except Exception as e:
            # Belt and braces -- both helpers swallow their own failures and
            # return a falsy answer, so this is not reachable in production.
            # Recording it is still right: failing to reach a VERDICT is a
            # refusal of the url, which is what this memo is keyed on.
            log.warn('incipit cover: alternate cover could not be judged '
                     '(%s: %s)', url, e)
            remember_alternate_refusal(url)
            continue
        if not acceptable:
            if data:
                log.info('incipit cover: alternate refused -- not square art (%s)', url)
            remember_alternate_refusal(url)
            continue
        # Redundant HERE, which is not the same as bad. Deliberately NOT
        # recorded as a refusal: that memo is module-global and keyed on the
        # url alone, so filing a contextual verdict in it would suppress this
        # picture for every OTHER book too -- the same trap the post-verdict
        # comment below already had to be fixed for. The cost of re-deciding
        # is one consult, and the consult is memoised.
        if alternate_already_on_display(data, judged, url):
            # NOT appended, deliberately: dropping out of `added` drops it from
            # the keep list, which is what lets validate_keys prune a twin an
            # OLDER version already put in the container. Returning early
            # instead would have left existing twins on display forever.
            continue
        if already_offered:
            # Judged and still wanted. The container already holds it, so
            # re-offering the bytes would be a no-op -- but it must stay in
            # `added` or the keep list drops a poster we just approved. It
            # also joins `judged`: it is on display, so a later alternate
            # that repeats it is a twin.
            judged.append(data)
            added.append(url)
            continue
        # THE VERDICT IS YES from here, so nothing below may record a refusal.
        # It used to: one blanket `except Exception` wrapped the container write
        # as well, and since fetch_url_bytes and alternate_cover_acceptable both
        # swallow their own exceptions that clause was reachable ONLY from these
        # post-verdict lines -- i.e. only from the case where recording a
        # refusal is WRONG. The memo is keyed on the url alone and is
        # module-global, so one container-write blip suppressed a genuinely good
        # cover for EVERY book in the library for the whole TTL.
        try:
            helper.metadata.posters[url] = Proxy.Media(data, sort_order=sort_order)
        except Exception as e:
            log.warn('incipit cover: alternate cover could not be offered '
                     '(%s: %s) -- NOT recorded as a refusal, the image is good',
                     url, e)
            continue
        added.append(url)
        # On display from here, so it becomes something the NEXT alternate is
        # judged against. Appended only after the container write succeeded --
        # an alternate that failed to land is not on display and must not
        # suppress a later copy of the same picture.
        judged.append(data)
        log.info('incipit cover: offering an alternate-marketplace cover (%s)', url)
    return added


def should_prune_local_twin(upload_holds_selection, local_present):
    """
        Whether to drop OUR container copy of cover.jpg as a duplicate tile.

        Extracted rather than left inline at the call site for the reason
        cover_keep_list was: the 2026-07-28 mutation sweep proved inline guards
        here go unenforced. Making this decision unconditional left the whole
        suite green, so the condition that matters most -- do NOT prune when the
        operator's own pick is showing -- had nothing pinning it.

        BOTH must hold:
          * the upload really holds the selection (select_local_cover said so).
            A stand-down means a user's poster is displayed, and our container
            entry is then their only route back to their local art.
          * we actually have an entry to prune this pass.
    """
    return bool(upload_holds_selection) and bool(local_present)


def online_offer_redundant(thumb_data, cover_bytes, local_set, mirror_skipped,
                           state, thumb_key):
    """
        True when offering the ONLINE cover would list an already-displayed
        picture again: either cover.jpg's picture (byte or perceptual --
        online_copy_is_redundant), or, CROSS-SOURCE, bytes another agent's
        poster already shows. The second leg exists because a rip often
        embeds the very CDN file the record's cover URL serves, so Local
        Media Assets displays bytes IDENTICAL to our online cover while the
        selection is a third picture -- measured on 27 of 246 albums after
        the 2026-07-27 corpse sweep, where the pair regenerated on every
        forced refresh and file surgery could not remove what the agent
        recreates each pass. Same rails as every dedupe: the selection is
        never undercut (duplicate_shown_elsewhere's own-key rail covers the
        online key), and no verdict fails open.

        Returns (redundant, byte_exact). The grade travels with the verdict
        because the caller may only PRUNE on byte identity -- the online
        entry is exactly where a branded or re-encoded VARIANT lives, and
        v1.3.153 gave the local mirror that protection while leaving the
        online copy deletable on a similarity guess (2026-07-28 review).
    """
    if cover_bytes is not None and thumb_data is not None and (
        local_set or mirror_skipped
    ):
        # The ONLINE cover is the tile withheld here, so it goes FIRST --
        # see same_picture's argument contract.
        same, byte_exact = same_picture(thumb_data, cover_bytes,
                                        'incipit cover')
        if same:
            return True, byte_exact
    if thumb_data is None:
        return False, False
    return duplicate_shown_detail(
        state, thumb_data, thumb_key, 'incipit cover-online')


def duplicate_shown_elsewhere(state, image_bytes, own_dict_key, tag):
    """True when another source already displays this picture (see detail)."""
    return duplicate_shown_detail(state, image_bytes, own_dict_key, tag)[0]


def duplicate_shown_detail(state, image_bytes, own_dict_key, tag):
    """
        (shown, byte_exact) when a NON-incipit container poster (an upload,
        Local Media Assets, any other agent) already displays `image_bytes` --
        so offering our copy would just list the same picture twice.

        `byte_exact` grades the evidence: identical bytes PROVE the picture is
        already on display, so the caller may prune our entry; a perceptual
        verdict only suggests it, so the caller may withhold the offer but
        must never delete the operator's own art on that basis.

        Operator rule (2026-07-26): byte-identical means shown ONCE however
        many sources hold it; a UNIQUE alternative is never hidden. This
        predicate implements the first half; the always-offer contract keeps
        the second.

        Rails, in order:
          * state None (fresh scan mid-flight, sealed sandbox) -> False;
          * no selection yet -> False: on a fresh scan the container is the
            ONLY selection mechanism, and our key must exist to be selected;
          * the selection IS our own key for this image -> False: pruning the
            selected key out from under Plex is the picked-poster-evaporates
            failure, and a selected copy is by definition not clutter.
        Every failure fails OPEN (offer as always): a duplicate tile is
        cosmetic, a missing poster option is not.
    """
    if state is None or not image_bytes:
        return False, False
    rk, selected_key, keys = state[0], state[1], state[2]
    if not selected_key:
        return False, False
    if selected_key == own_container_key(own_dict_key):
        return False, False
    fetched = 0
    # The selection first: it is the copy most likely to be the duplicate
    # (a hand upload of the same art), and one match ends the scan.
    ordered = [k for k in keys if k == selected_key]
    ordered += [k for k in keys if k != selected_key]
    for key in ordered:
        # Our own keys are managed where they are OFFERED (the online-vs-local
        # guard); this check looks only ACROSS sources.
        if 'com.plexapp.agents.incipit' in key:
            continue
        if fetched >= DUPLICATE_CHECK_MAX_FETCHES:
            break
        fetched += 1
        data = poster_file_bytes(rk, key, tag)
        if data is None:
            continue
        # same_picture is byte identity, then the pref-gated perceptual
        # consult -- one primitive, so the gate cannot drift between the
        # legs the way it did in v1.3.151.
        shown, byte_exact = same_picture(image_bytes, data, tag)
        if shown:
            log.info(
                '%s: %s already shows this %s -- not listing our copy',
                tag, key, 'image' if byte_exact else 'picture (re-encode)'
            )
            return True, byte_exact
    return False, False


def online_copy_is_redundant(thumb_data, cover_bytes, local_set, mirror_skipped,
                             tag='incipit cover'):
    """
        True when offering (or keeping) the ONLINE cover would just list
        cover.jpg's picture a second time. Two ways that picture is already
        on display: our own local mirror took the default (local_set), or the
        mirror offer was withheld because ANOTHER source's poster shows it
        (mirror_skipped). v1.3.133 keyed this on local_set alone, so the very
        pass that suppressed the local copy re-offered the identical online
        one -- the duplicate came straight back through the other key.

        Since v1.3.150 the equality is perceptual, not just byte-strict: the
        online cover is often a re-encode or lightly-branded variant of the
        very art in cover.jpg (The Knight: the Audible-bannered cover next to
        the clean one, dHash distance 2), and byte-only redundancy re-listed
        the same picture every refresh. Gate and rails unchanged.

        This says nothing about which key is SELECTED -- an earlier version of
        this docstring claimed it did, and the 2026-07-28 review confirmed the
        keep-list could prune a selected online cover on the strength of it.
        The selection rail lives in `online_prune_allowed`, at the prune.
        Fails open on missing bytes or no verdict, like every dedupe rail.
    """
    if thumb_data is None or not cover_bytes:
        return False
    if not (local_set or mirror_skipped):
        return False
    # The ONLINE cover is what gets withheld, so it is the FIRST argument.
    return same_picture(thumb_data, cover_bytes, tag)[0]


# The local-cover block's decisions, carried from the first track of a pass to
# its siblings: update() runs once per TRACK, and a dup-skip never adds our key
# to the container, so the membership guard never engaged and every track of a
# curated album re-read cover.jpg, re-read the poster state, and re-downloaded
# up to DUPLICATE_CHECK_MAX_FETCHES container posters to reach the identical
# decision (a 27-part book: ~200 extra requests per pass). The container itself
# survives between tracks, so only the FLAGS need restoring. Same idiom as
# recent_work_memo/artist_art_memo, but it must carry a payload, which
# should_run's token cannot. Values: ((local_set, mirror_skipped,
# deferred_portrait_local, poisoned_local, online_redundant), stamp).
album_cover_memo = {}
# Short on purpose: long enough to span one album's per-track sweep, short
# enough that an operator who replaces cover.jpg and refreshes again soon gets
# a fresh read (force and scan passes are ALSO keyed apart, so a forced
# refresh never inherits a scan pass's decisions).
ALBUM_COVER_TTL = 60


def album_cover_decision(guid, force):
    """The memoized flag tuple for this guid and pass kind, or None."""
    hit = album_cover_memo.get((guid, bool(force)))
    if hit and (time() - hit[1]) < ALBUM_COVER_TTL:
        return hit[0]
    return None


def remember_album_cover_decision(guid, force, flags):
    """
        Store this pass's cover decisions for the album's other tracks.

        `flags` is a NAMED MAPPING, never a tuple: it was a positional
        5-tuple, two flags were added to the block without being added to it,
        and tracks 2..N silently restored the wrong values -- turning both
        2026-07-28 prune rails off for 26 of a 27-part book's tracks. A dict
        cannot drift positionally, and `album_cover_decision`'s readers use
        `.get(key, False)` so an older entry degrades to the SAFE value.
    """
    album_cover_memo[(guid, bool(force))] = (dict(flags), time())


# Per-guid cache for the artist poster, because update() runs once per TRACK
# and helper.force defeats the container re-read guard -- so without this a
# forced refresh of a 27-part book paid a /library/all round-trip PLUS a full
# artist-image download on every track, 54 requests to answer one question 27
# times identically. Values are (bytes_or_None, known, stamp); the TTL bounds
# staleness the same way recent_work_memo does.
artist_art_memo = {}
ARTIST_ART_TTL = 600
# A NOT-KNOWN answer is memoised too, but only briefly. With no entry at all a
# reliably-failing artist URL paid a fresh 8s HTTP.Request on EVERY track:
# update() runs once per track, and promote_picked_cover's fail-closed return
# deliberately skips mark_done, so should_run's per-track gate never closes and
# the next pass repeats it. On a 27-part book that is 27 x 8s, every pass. The
# TTL is the ALBUM_COVER_TTL scale on purpose -- long enough to span one
# album's per-track sweep so the cost is paid ONCE, short enough that the next
# refresh genuinely retries. "A transient failure must retry, not be
# suppressed" holds across passes, which is the level it was written about.
ARTIST_ART_FAIL_TTL = 60


def artist_art_key(guid, parent_thumb):
    """
        The memo key -- and `parent_thumb` is PART OF IT, which is load-bearing.

        Keyed on the guid alone, the DISPLAY caller (compile_metadata, which
        passes no parent_thumb and so does its own /library/all lookup)
        answered for the WRITE callers, which pass one. Those are exactly the
        callers whose poison guard must fail closed, and update() runs once per
        TRACK -- so track N+1's write path read what track N's display path
        stored. Different question, different key.
    """
    return (guid, parent_thumb or None)


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
    key = artist_art_key(guid, parent_thumb)
    hit = artist_art_memo.get(key)
    if hit:
        # A KNOWN answer is good for the long TTL; a not-known one only for the
        # short one, so a failure collapses an album's per-track sweep without
        # suppressing the next pass's retry.
        ttl = ARTIST_ART_TTL if hit[1] else ARTIST_ART_FAIL_TTL
        if (time() - hit[2]) < ttl:
            return (hit[0], hit[1])
    try:
        purl = parent_thumb
        if not purl:
            url = PMS + '/library/all?guid=' + urllib.quote(guid)
            text = str(HTTP.Request(url, timeout=8, cacheTime=0).content)
            if guid_lookup_is_ambiguous(text, tag):
                artist_art_memo[key] = (None, False, time())
                return (None, False)
            if not distinct_rating_keys(text):
                # ZERO matches. This is NOT "resolved cleanly, no artist art":
                # the item was not found AT ALL -- a fresh-scan row with no
                # ratingKey yet, a truncated response, a section mid-rebuild.
                # It used to fall straight through to the `not purl` branch
                # below and be memoised as the positive fact "this artist has
                # no poster, KNOWN" for 600s, which is what the WRITE paths'
                # fail-closed poison guard consults: known=True with
                # artist_bytes=None makes selection_is_artist_art(None, ...)
                # vacuously False, and a curated cover.jpg gets overwritten on
                # the strength of a lookup that found nothing. Unknown.
                log.warn('%s: /library/all matched no item for this guid -- the '
                         'artist poster is UNREADABLE, not absent', tag)
                artist_art_memo[key] = (None, False, time())
                return (None, False)
            # capital-T parentThumb, so a lowercase thumb= match can't collide.
            pm = re.search(r'parentThumb="([^"]*)"', text)
            purl = pm.group(1) if pm else None
        if not purl:
            # The item WAS found and carries no parentThumb; this artist simply
            # has no poster. KNOWN -- and only reachable now that a zero-result
            # lookup is intercepted above.
            artist_art_memo[key] = (None, True, time())
            return (None, True)
        if not purl.startswith('http'):
            purl = PMS + purl
        data = HTTP.Request(purl, timeout=8, cacheTime=0).content
        artist_art_memo[key] = (data, True, time())
        return (data, True)
    except Exception as e:
        log.error('%s: could not read the artist poster (%s)', tag, e)
        artist_art_memo[key] = (None, False, time())
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
                             pref_asserted=False, selected=None):
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
        family = pad_family_shas(image_bytes)
        sha = family[0][0]
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
    if selected_key:
        # ANY family generation holding the selection is converged -- a
        # v2-selected upload must not read as foreign, or this would re-run
        # forever and mint the next generation every pass.
        for fam_sha, _ in family:
            if fam_sha in selected_key:
                log.info('%s: already the selected poster, skip', tag)
                mark_done(tag, guid, memo_token)
                return True
    # Explicit loop, not any(): the sandbox does not provide any()/all()/sum()
    # (proven live -- `NameError: global name 'any' is not defined` aborted the
    # whole artist update). set() IS available; the blocklist is irregular, so
    # find in-repo precedent before using any builtin here.
    burned = {}
    for k in keys:
        for fam_sha, _ in family:
            if fam_sha in k:
                burned[fam_sha] = True
    have_plain = sha in burned
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
    # The pref path's lever: the FIRST family form Plex does not already hold.
    # Before v1.3.190 this was plain-then-v1-then-give-up, and the give-up was
    # PERMANENT -- the 2026-08-08 rebuild left 83% of the library wedged there
    # (a burst-raced select burns a form without landing the selection, and
    # burned forms can never be re-posted: Plex dedupes content and the
    # sandbox cannot select an existing upload). Generations make the wedge a
    # retry; the cap keeps the store bounded and the give-up log stays for the
    # genuinely exhausted case.
    unburned = None
    if pref_asserted:
        for fam_sha, fam_bytes in family:
            if fam_sha not in burned:
                unburned = fam_bytes
                break
        if unburned is None:
            log.warn(
                '%s: every pad generation already exists de-selected on rk %s; '
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
        # `selected` lets a caller that has ALREADY read the selected poster
        # hand those bytes over instead of paying for the same download twice
        # (correct_portrait_selection reads it to prove the jacket is showing).
        if selected is not None:
            current, known = (selected, True)
        else:
            current, known = selected_poster_bytes(rk, selected_key, tag)
        if known and same_image(image_bytes, current):
            log.info('%s: the selected poster already IS this image (rk %s) -- '
                     'not uploading a duplicate', tag, rk)
            mark_done(tag, guid, memo_token)
            return True
    # pref_asserted: post the first unburned family form (plain when nothing
    # is burned, else the next pad generation) -- new content, identical
    # pixels, and POSTing new content is what re-selects.
    reselect = have_plain and pref_asserted
    post_bytes = unburned if pref_asserted else image_bytes
    # Byte literals, not str: the same py2/py3 drift byte_at exists to kill.
    # Under py3 `image_bytes[:4] == '\\x89PNG'` is always False, so the tests
    # that now drive this function could never catch a mislabelled upload --
    # and in py2 a unicode body (which fetch_url_bytes can return, see below)
    # fails the comparison too. WebP and BMP are named because this diff made
    # both measurable, so both can now reach a POST.
    head = image_bytes[:4]
    content_type = 'image/jpeg'
    if head == b'\x89PNG':
        content_type = 'image/png'
    elif head == b'RIFF':
        content_type = 'image/webp'
    elif image_bytes[:2] == b'BM':
        content_type = 'image/bmp'
    # ...and REFUSE the two Plex stores but will not DRAW. Measured live
    # 2026-08-01: Steven Erikson's artist tile was blank because the selected
    # poster was a 24,314-byte WebP -- exactly 20 bytes over a 24,294-byte
    # twin, so we had uploaded a WebP and then PADDED-re-selected it -- while a
    # 1022x1280 JPEG sat unselected in our own container.
    #
    # Refusing is the SAFE direction and standing down is not a loss: whatever
    # JPEG/PNG is already offered keeps the selection. Posting is the
    # unrecoverable direction, because re-selecting a poster Plex already holds
    # as a de-selected upload needs PUT, which this sandbox downgrades to a
    # no-op -- so one bad upload wedges the item until a HUMAN picks in the UI.
    # Confirmed live: the operator refreshed Erikson and nothing moved.
    #
    # Sniffing these two already existed here, purely to label the
    # Content-Type; that is what made "we knowingly post a format that renders
    # blank" true rather than accidental.
    if content_type not in ('image/jpeg', 'image/png'):
        log.warn('%s: refusing to upload %s -- Plex stores it but will not '
                 'render it, and we could not re-select past it (rk %s)',
                 tag, content_type, rk)
        return False
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


# Every way this agent can change which author image is selected. Each one
# invalidates the others' per-pass memo, because a cached "done" describes a
# selection another direction is about to change. Kept as ONE list so adding a
# direction cannot half-wire it (v1.3.132 added 'fit' to the code and not to
# the old two-entry opposite-map, and the gap survived until 2026-07-31).
ART_DIRECTION_TAGS = (
    'incipit author-art-select',
    'incipit author-art-unpin',
    'incipit author-art-fit',
)


def author_art_withheld(offered, valid_posters):
    """
        The author-poster keys the artist-art prune actually REMOVES: keys
        currently offered in the container that the keep-list does not carry.

        A named decision rather than an inline comprehension, because the log
        that reports it has to be GATED on it and an inline gate is untestable.
        The first attempt at making this prune visible fired on every artist
        update -- including the common two-key no-op -- and reported
        len(valid_posters), the KEPT count. Neither "a prune happened" nor
        "3 survived" identifies the tile that vanished, which is the entire
        reason the line exists.
    """
    keep = valid_posters or []
    out = []
    for key in (offered or []):
        if key and key not in keep and key not in out:
            out.append(key)
    return out


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
    # ALL the other directions, not just a pairwise opposite. v1.3.132 added a
    # THIRD direction, `author-art-fit`, and never added it to the old two-entry
    # map -- so fit invalidated nothing and nothing invalidated fit. That is not
    # theoretical: prefer_square_author_art is ON by default and
    # compile_metadata alternates between `fit` and `unpin` on the same artist
    # depending only on whether the images were measurable that pass, so within
    # the 600s TTL an unpin that flipped the selection left the stale fit entry
    # standing and the next fit pass silently skipped -- the square portrait
    # never came back. A LIST, so a fourth direction cannot reintroduce the gap
    # by being added to one side of a pair.
    for other in ART_DIRECTION_TAGS:
        if other != tag:
            # .pop(), not `del d[k]`: RestrictedPython compiles subscript-
            # deletion through a guard that may be absent (the same class of
            # silent whole-plugin death as leading-underscore names); a method
            # call is unambiguously safe.
            recent_work_memo.pop((other, guid), None)
    if not should_run(tag, guid, target_url, 600):
        return
    state = read_poster_state(guid, tag)
    if state is None:
        return
    rk, selected_key, keys, parent_thumb = state
    # A HUMAN pick is inviolable, whatever it points at -- see
    # thumb_field_locked for the signal and its measurement. This runs FIRST,
    # before the ownership shas and before any CDN fetch: it protects the one
    # class the byte-sha rules cannot (a click-pick between the agent's own
    # two provider images, which the fit direction overrode by design until
    # the operator overruled that on 2026-08-08), and it makes every pass
    # over a curated artist cheaper, not costlier. Only consulted when a
    # selection EXISTS: a fresh artist with nothing selected has nothing a
    # human could have chosen.
    if selected_key:
        locked, lock_known = thumb_field_locked(rk, tag)
        if not lock_known:
            # No mark_done: a transient failure must RETRY, not be suppressed.
            # The value still fails closed (we leave the selection alone), but
            # recording that stand-down would replay it on every sibling track
            # of the album and outlive the blip by the whole TTL.
            log.error('%s: could not read the thumb field lock -- leaving the '
                      'selection alone and retrying next pass', tag)
            return
        if locked:
            log.info('%s: the poster was chosen by a human (thumb field locked) '
                     '-- leaving it', tag)
            mark_done(tag, guid, target_url)
            return
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
                    fam = pad_family_shas(image_bytes)
                except Exception as e:
                    log.error('%s: could not hash a candidate image (%s)', tag, e)
                    continue
                for fam_sha, fam_ignored in fam:
                    owned_shas.append(fam_sha)
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
    acted = upload_and_select_poster(guid, target_bytes, tag, token=target_url,
                                     state=state, pref_asserted=True)
    # The instant the select lands, the upload displays these bytes -- and the
    # agent's own CONTAINER copy of the same image is a duplicate the picker
    # will show alongside it until the NEXT pass's offer-time dedupe catches
    # up (measured live on Robert Harris, 2026-07-27: the new picture listed
    # twice for exactly one refresh). Prune it here, where the knowledge is.
    # The OTHER image stays offered -- byte-identical shown once, unique
    # alternatives never hidden -- and the selection is the upload:// key, so
    # this cannot touch it. On the stand-down paths (acted False) the
    # container is left exactly as it was.
    if acted:
        try:
            if target_url in helper.metadata.posters:
                keep = []
                if other_url and other_url in helper.metadata.posters:
                    keep.append(other_url)
                helper.metadata.posters.validate_keys(keep)
                # WARN, not info: the shipped logging_level default is WARN, so
                # info() is suppressed -- and this REMOVES a poster from the
                # picker. Every prune must leave a trace at the default level;
                # the 2026-07-26 loss of 92 covers was hard to reconstruct
                # precisely because the destructive lines were invisible.
                log.warn(
                    '%s: pruned our container copy of the just-selected image',
                    tag
                )
        except Exception as e:
            log.error('%s: container prune failed (%s)', tag, e)


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
    try:
        family = pad_family_shas(cover_bytes)
        sha = family[0][0]
    except Exception as e:
        log.error('%s: could not hash the local cover (%s)', tag, e)
        return
    # The WHOLE family, exactly as select_local_cover and converge_author_art
    # build it. This kept only family[0] and family[1] -- the pre-v1.3.190
    # plain+v1 pair -- so a v2/v3-padded upload THE AGENT ITSELF minted read
    # as a foreign user upload and the portrait correction stood down
    # permanently on precisely the albums the generations exist to rescue.
    owned = []
    for fam_sha, fam_ignored in family:
        owned.append(fam_sha)
    # MEMO FIRST, like select_local_cover and converge_author_art. update() runs
    # once per TRACK, and every read below is a round trip -- read_poster_state
    # is two, and selected_poster_bytes downloads a full poster. Asking the memo
    # after them meant a 27-part book paid 54 localhost GETs and 27 whole-poster
    # downloads per refresh to reach the same answer, because the only mark_done
    # sat inside upload_and_select_poster where tracks 2..27 never arrive.
    # Keyed on the cover's sha, so a REPLACED cover.jpg re-runs at once rather
    # than waiting out the TTL -- the same token select_local_cover uses.
    if not should_run(tag, guid, sha, 90):
        return
    state = read_poster_state(guid, tag)
    if state is None:
        return
    rk, selected_key, keys, parent_thumb = state
    # The jacket's own shas count as ours: select_local_cover may have uploaded
    # it on an earlier pass, and undoing our own act is the whole point.
    if not selection_is_agent_owned(selected_key, owned):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, sha)
        return
    if not selected_key:
        # Nothing selected is a healthy state, not a failed read -- and there is
        # no jacket to move off. Distinguished from the (None, False) a timed-out
        # fetch returns, which must NOT be logged as an error either way.
        mark_done(tag, guid, sha)
        return
    current, known = selected_poster_bytes(rk, selected_key, tag)
    if not known:
        # No mark_done: a blip must retry on the next pass, not be suppressed.
        log.error('%s: could not read the selected poster -- NOT overriding it, '
                  'so a blip cannot take away a poster on rk %s', tag, rk)
        return
    if not same_image(cover_bytes, current):
        log.info('%s: the selection is not the print jacket -- leaving it', tag)
        mark_done(tag, guid, sha)
        return
    # A HUMAN pick is inviolable -- the third override path to learn the thumb
    # field-lock rule its two siblings got in this release, and without it this
    # one could silently revert a deliberate click.
    #
    # Asked ONLY of the ambiguous class, and only HERE. selection_is_agent_owned
    # admits two very different keys and its docstring records the difference:
    # an `upload://` carrying one of THIS image's family shas is a poster the
    # agent provably WROTE, so overriding it merely undoes our own act and needs
    # no permission; a `com.plexapp.agents.incipit` container key is
    # agent-SUPPLIED but click-vs-default INDISTINGUISHABLE, and that is exactly
    # the class the field lock was measured to resolve. Placed after same_image
    # has proven the jacket is really showing, so a converged book pays no round
    # trip for a question whose answer it does not need.
    agent_written = False
    for fam_sha, fam_ignored in family:
        if fam_sha in selected_key:
            agent_written = True
            break
    if not agent_written:
        locked, lock_known = thumb_field_locked(rk, tag)
        if not lock_known:
            # No mark_done: a blip must retry, not be suppressed -- the same
            # rule the unreadable-selection branch above already follows.
            log.error('%s: could not read the thumb field lock -- NOT '
                      'overriding the selection on rk %s', tag, rk)
            return
        if locked:
            log.info('%s: the jacket was chosen by a human (thumb field '
                     'locked) -- leaving it', tag)
            mark_done(tag, guid, sha)
            return
    log.warn('%s: rk %s is showing the PORTRAIT cover.jpg the deferral declined; '
             'force-selecting the square online cover instead', tag, rk)
    # `selected` hands the bytes we just read to the callee's duplicate guard,
    # which would otherwise re-download this exact poster to ask a question this
    # line already answered: the selection is the jacket, and square_bytes is
    # not the jacket or there would be nothing to fix.
    upload_and_select_poster(guid, square_bytes, tag, token=sha, state=state,
                             selected=current)


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
        for an ALREADY-SCANNED artist. Gated on `prefer_square_author_art`.

        The container ordering only decides on a fresh scan, so without this the
        improvement never reaches an existing library -- measured 2026-07-25, 39
        artists were sitting on the worse-fitting image with no way to converge
        short of picking each by hand.

        ON by default since v1.3.132 (operator decision 2026-07-26): the
        ownership gate in converge_author_art already refuses to touch a USER
        UPLOAD, so the only thing this can override is a click-pick between the
        agent's OWN two provider images -- which Plex cannot distinguish from a
        scan default anyway. The operator's curation convention (upload when it
        matters) makes that class empty, and default-on turns the census-and-
        refresh convergence pass into ordinary refresh behaviour.

        Does nothing without evidence: an unmeasurable image, a missing second
        image, or two identically-sized ones all leave the artist alone rather
        than spend an upload/select round trip to change nothing -- and SAYS
        so: returns True only when a verdict was formed. The caller needs the
        difference, because with the pref defaulting on this branch swallows
        every two-image author, and a silent no-op here made the unpin
        fallback (the pre-1.3.132 stuck-pin remedy) unreachable for exactly
        the authors it exists to heal.
    """
    if not helper.thumb or not helper.thumb_secondary:
        return False
    if not thumb_dims or not secondary_dims or thumb_dims == secondary_dims:
        return False
    winner = better_square_portrait(thumb_dims, secondary_dims)
    if winner is thumb_dims:
        target, other = helper.thumb, helper.thumb_secondary
    else:
        target, other = helper.thumb_secondary, helper.thumb
    converge_author_art(helper, target, other, 'incipit author-art-fit')
    return True


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
# Dimensions remembered per image URL. The artist update runs once per ALBUM,
# and only the pass that FETCHES an image can measure it -- so without this,
# every later pass had dims of None, the square-fit reorder condition failed,
# and validate_keys re-ran in DEFAULT order, undoing the first pass's correct
# ordering. The last pass wins, so any author with several books always ended
# on the default (Audible) image. Verified live 2026-07-26 on Bryce O'Connor:
# 820x820 vs 1000x1500 -- the rule picks the square unambiguously, and the tall
# was selected anyway. Keyed by URL, so a provider serving a new image
# self-invalidates; ~2 entries per author, so growth is trivial.
IMAGE_DIMS_MEMO = {}


def remember_dims(url, data):
    """Measure `data`, remember the answer for `url`, return it (None stays
    unremembered, so a later good fetch can still fill it)."""
    measured = image_dimensions(data)
    if url and measured:
        IMAGE_DIMS_MEMO[url] = measured
    return measured


# How close two portraits' SHORT EDGES must be before squareness decides
# instead of resolution. WAS 0.75; widened to 0.5 on live evidence (2026-07-26,
# Callie Hart): her Hardcover square is 400x400 against an Audible 576x768
# portrait -- 400/576 = 0.69, just outside the old band, so resolution won and
# a portrait selfie beat the square professional photo in a SQUARE tile. A
# centre-crop of a 3:4 portrait loses the top of the head; a modest square
# stays a face -- so squareness deserves the wider berth. Glen Cook's guard
# case (117px thumbnail vs 3072x2304, ratio 0.05) still resolves to
# resolution, so the postage-stamp regression stays impossible.
SQUARE_TIE_BAND = 0.5


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


def offer_secondary_author_poster(helper, valid_posters, dup_state=None,
                                  thumb_data=None):
    """
        Add the Audible `imageAlt` to the artist's poster container as a
        selectable option, and return the updated validate_keys list.

        Kept as an OPTION even for pinned authors: not wanting it selected is not
        the same as not wanting it available, and pruning it left those authors
        with a single poster and no way to switch in the UI. What withholds it
        is byte-identity, two flavors: a copy of a picture the container
        already shows under ANOTHER SOURCE's key (v1.3.133, via dup_state),
        and a copy of the PRIMARY image itself (v1.3.143, via `thumb_data` --
        measured live on Aleron Kong, whose API record served the same
        picture as both image and imageAlt under different URLs, so the URL
        inequality check above proved nothing and the tile listed it twice
        forever: the cross-source dedupe deliberately skips the agent's own
        keys). Either way the copy is dropped from the membership list too,
        pruning any stale entry. Same rails as duplicate_shown_elsewhere:
        fresh scans and selected keys are never touched, and no bytes in
        hand means fail open.
    """
    if not helper.thumb_secondary or helper.thumb_secondary == helper.thumb:
        return valid_posters, None
    # Dimensions come back with the list so the caller can decide which image
    # fills a square tile better (see better_square_portrait). Measured here
    # because this is where the bytes already are -- fetching them again to
    # measure would double the author-art traffic.
    secondary_dims = None
    secondary_dup = False
    if (helper.thumb_secondary not in helper.metadata.posters or helper.force):
        # fetch_url_bytes, NOT make_request: the latter returns Plex's lazy
        # HTTPRequest wrapper, and image_dimensions below slices it -- which
        # raised 'HTTPRequest object has no attribute __getitem__' into the
        # outer except and became None on EVERY artist (measured live
        # 2026-07-25). Proxy.Media accepted the wrapper, so the poster still
        # appeared and only the measurement was lost, which is why it went
        # unnoticed from v1.3.118.
        secondary_data = fetch_url_bytes(helper.thumb_secondary)
        if secondary_data is not None:
            # Measured even for a skipped duplicate -- the select machinery
            # compares by URL bytes, not container membership.
            secondary_dims = remember_dims(helper.thumb_secondary, secondary_data)
            sel_key = dup_state[1] if dup_state else None
            if (thumb_data is not None
                    and same_image(thumb_data, secondary_data)
                    and sel_key != own_container_key(helper.thumb_secondary)):
                # The selection rail (measured live on Aleron Kong): when the
                # SELECTED poster is this very container entry, withholding it
                # achieves nothing -- Plex retains a selected entry regardless
                # of what the agent lists -- and the withholdable copy of the
                # pair is the THUMB (see the twin rail in the artist update).
                secondary_dup = True
                log.info(
                    'incipit author-offer: the secondary is the same picture '
                    'as the primary -- not listing it twice'
                )
            else:
                secondary_dup = duplicate_shown_elsewhere(
                    dup_state, secondary_data, helper.thumb_secondary,
                    'incipit author-offer')
        if secondary_data is not None and not secondary_dup:
            helper.metadata.posters[helper.thumb_secondary] = \
                Proxy.Media(secondary_data, sort_order=1)
    if secondary_dims is None:
        # A pass that did not fetch (image already in the container) must still
        # KNOW the dims, or the square-fit ordering decided on pass 1 reverts
        # on pass 2 -- see IMAGE_DIMS_MEMO.
        secondary_dims = IMAGE_DIMS_MEMO.get(helper.thumb_secondary)
    if not secondary_dup:
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


def local_cover_recovery_needed(helper):
    """
        Whether prefer_local_cover should recover WITHOUT a forced refresh.

        The 2026-08-08 rebuild proved the gap: fresh-scan births race Plex's
        combiner (which seats the agent's online art over localmedia's
        cover.jpg upload), and the recovering upload-lever ran only under
        `helper.force` -- so scheduled and plain refreshes could never heal a
        lost birth, and 83% of the library sat on online art indefinitely.

        The trigger is precise and CHEAP -- one localhost state read, no image
        bytes: a selection exists and it is NOT an upload. Every healthy end
        state is an upload:// selection (localmedia's cover, the agent's own
        select, or a human pick), so a container-key selection on a
        prefer_local library is exactly the lost-birth class. Human picks of
        container art are protected downstream by the thumb-lock gate in
        select_local_cover, and the portrait deferral is re-applied by the
        caller before any select.

        Memoised per guid for the pass (the same per-track collapse every
        selection path uses): track 1 answers for the whole album.

        Returns (needed, state) -- the poster state it just fetched rides
        along, because select_local_cover is the only thing a True answer
        leads to and it asks read_poster_state the SAME question about the
        SAME guid with nothing changing in between. Handing it over turns 4
        localhost GETs into 2 on every plain recovery pass, and
        read_poster_state disables the HTTP cache on both of its requests, so
        neither round trip was ever free. `state` is None whenever there is
        nothing to hand on (memo hit, unreadable read), and the callee then
        does its own read exactly as before -- the same optional-prefetch
        shape upload_and_select_poster already takes.
    """
    guid = helper.metadata.guid
    if not should_run('incipit local-recovery', guid, 'check', 300):
        return (False, None)
    state = read_poster_state(guid, 'incipit local-recovery')
    if state is None:
        return (False, None)
    rk, selected_key, keys, parent_thumb = state
    needed = bool(selected_key) and not selected_key.startswith('upload')
    if not needed:
        # Only the NO-work answer is memoised: a needed recovery must stay
        # re-askable, because select_local_cover's own verdict memo is what
        # collapses its per-track cost once it actually runs.
        mark_done('incipit local-recovery', guid, 'check')
        return (False, None)
    return (True, state)


def select_local_cover(helper, cover_bytes=None, state=None):
    """
        Force the book folder's cover.jpg to become the SELECTED Plex poster on
        a Refresh of an ALREADY-scanned book (the container path only wins on a
        fresh scan). Ownership-guarded: an agent-supplied selection (or our own
        earlier upload) is overridden -- that is what prefer_local_cover means
        -- but a USER'S custom upload is left alone, so hand-picks survive and
        backup_selected_poster (which now runs AFTER this) can capture them to
        cover.jpg instead of this path clobbering them.

        DO NOT make this conditional on the container already offering the same
        bytes. v1.3.166 tried exactly that -- skip the upload on a cold scan and
        let the container's sort_order=0 entry become the default -- and it was
        DISPROVED LIVE on a fresh library: no local posters appeared at all.
        THIS UPLOAD is what selects cover.jpg; the container offer only makes it
        available in the picker. The duplicate tile that motivated v1.3.166 is
        real, but it has to be removed from the CONTAINER side, not by removing
        the only mechanism that asserts a selection.
    """
    # The album update has usually just read this exact file to seed the
    # posters container; accept those bytes rather than pulling ~1MB back over
    # SMB a second time in the same pass. None means "nobody read it for me".
    if cover_bytes is None:
        cover_bytes = local_cover_bytes(helper)
    if not cover_bytes:
        return False
    tag = 'incipit local-select'
    guid = helper.metadata.guid
    try:
        family = pad_family_shas(cover_bytes)
        sha, sha_padded = family[0][0], family[1][0]
    except Exception as e:
        log.error('%s: sha1 failed (%s)', tag, e)
        return False
    if not should_run(tag, guid, sha, 90):
        # A sibling track already handled this album THIS pass. Replay what it
        # actually DECIDED -- not a blanket True. The caller prunes our
        # container copy of cover.jpg on the strength of this value, and three
        # of the paths below return False precisely to keep that copy offered
        # (a user upload holding the selection is the operator's only route
        # back to their local art). Reporting True for those inverted the
        # stand-down on every track after the first.
        #
        # Unknown verdict -> False: not pruning leaves a duplicate tile,
        # pruning wrongly destroys curated art.
        replayed = recall_verdict(tag, guid, sha, 90)
        return replayed if replayed is not None else False
    # `state` is an optional pre-fetched read_poster_state result (see
    # local_cover_recovery_needed, which has just read exactly this for exactly
    # this guid). Read here only when nobody handed one over.
    if state is None:
        state = read_poster_state(guid, tag)
    if state is None:
        return False
    rk, selected_key, keys, parent_thumb = state
    owned = []
    for fam_sha, fam_ignored in family:
        owned.append(fam_sha)
    if not selection_is_agent_owned(selected_key, owned):
        log.info('%s: selection is a user upload -- leaving it', tag)
        mark_done(tag, guid, sha)
        remember_verdict(tag, guid, sha, False)
        # FALSE keeps our container copy OFFERED: it is the operator's only
        # route back to their local art while their own pick is showing.
        return False
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
    converged = False
    if selected_key:
        for fam_sha, fam_ignored in family:
            if fam_sha in selected_key:
                converged = True
                break
    if not converged:
        # A HUMAN pick is inviolable, whatever it points at -- the same thumb
        # field-lock rule converge_author_art enforces (thumb_field_locked).
        # Read HERE, on the would-act path only: a converged book must stay
        # zero-cost on refresh (the pinned "does no work" invariant), and a
        # user-upload selection already stood down above without paying for
        # this read either.
        if selected_key:
            locked, lock_known = thumb_field_locked(rk, tag)
            if not lock_known:
                # No mark_done, no remembered verdict -- the same discipline
                # the poison guard below states and implements. This branch
                # used to record False, so ONE timed-out localhost GET during
                # a scan burst made every sibling track replay the stand-down
                # and suppressed the container-twin prune for the whole album.
                # The stand-down itself still happens (fail closed on the
                # poster-preserving direction); it just is not remembered.
                log.error('%s: could not read the thumb field lock -- NOT '
                          'selecting cover.jpg, so a blip cannot take away a '
                          'poster a human may have chosen', tag)
                return False
            if locked:
                log.info('%s: the poster was chosen by a human (thumb field '
                         'locked) -- leaving it', tag)
                mark_done(tag, guid, sha)
                remember_verdict(tag, guid, sha, False)
                return False
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
            # Fails closed for the prune too: nothing was uploaded, so our
            # container copy must stay OFFERED.
            return False
        if selection_is_artist_art(artist_bytes, cover_bytes):
            # Memoised on the cover's own sha, so a repaired cover.jpg re-runs
            # immediately rather than waiting out the TTL.
            mark_done(tag, guid, sha)
            remember_verdict(tag, guid, sha, False)
            log.warn('%s: cover.jpg IS the artist photo (byte-identical) -- refusing '
                     'to select it, so the book is not re-poisoned; the current '
                     'selection stands and will mirror to disk', tag)
            return False
    # pref_asserted, because OWNERSHIP IS ALREADY PROVEN above. The default
    # stand-down encodes a book-level premise -- "our bytes offered but
    # de-selected can only be a person's choice" -- that this caller has
    # disproved by the time it gets here: a user's custom upload returned at
    # selection_is_agent_owned, so what remains is the agent's own selection,
    # and prefer_local_cover says cover.jpg wins between agent images.
    #
    # The premise is false on a COLD SCAN for a second reason: Plex's Local
    # Media Assets files cover.jpg into the item's own Uploads, so "our bytes
    # exist de-selected" arises with no human involvement at all. Measured
    # 2026-07-29 on a fresh 1,509-album library -- the online cover held the
    # selection on 49 of 60 sampled albums and NOTHING recovered it: plain
    # refresh 0/4, forced refresh 0/4, because this call stood down every time.
    # Ownership was checked and the action then silently vetoed: the same
    # "correct and powerless" shape the portrait deferral hit at v1.3.121.
    # RETURNED, not discarded: the caller prunes our twin container entry only
    # when this reports that the upload really does hold the selection.
    outcome = upload_and_select_poster(guid, cover_bytes, tag, token=sha,
                                       state=state, pref_asserted=True)
    remember_verdict(tag, guid, sha, outcome)
    return outcome


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

        # SCORE FIRST. The recovery gate below needs to know whether anything
        # in the result set can actually auto-match, and only scoring answers
        # that -- a non-empty result set that all sits under Plex's bar is as
        # useless as an empty one. process_results builds a fresh `info` list
        # per call, so running it again after a recovery is safe.
        info = self.process_results(search_helper, result) if result else []

        # Fallback: the tagged artist name matched no USABLE author. Most often
        # it is a NARRATOR mis-tagged as the artist (e.g. "Lauren Fortgang"),
        # or a credit string that is not a person at all ("Stephenson &
        # Galland"). Ask the book API what this album is and recover its author
        # -- but only trust an author that is ALSO a folder in the file's path,
        # so a wrong name can never win.
        #
        # See artist_recovery_warranted for why this is no longer gated on a
        # ZERO result: the Stephenson case RETURNED four authors, so the old
        # gate never fired, while the bare surname it searched scored them all
        # under the bar. A transport blip (None) still never recovers.
        if artist_recovery_warranted(result, info):
            recovered_author = self.recover_author_from_book(
                search_helper, candidates
            )
            if recovered_author:
                search_helper.media.artist = String.StripDiacritics(
                    recovered_author
                )
                recovered = self.call_search_api(search_helper)
                # Keep the ORIGINAL rows if recovery found nothing: they may be
                # below the auto-match bar but they are still what the operator
                # sees in Fix Match, and discarding them would turn a weak
                # offer into no offer at all.
                if recovered:
                    result = recovered
                    info = self.process_results(search_helper, recovered)

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

            # Print separators for easy reading. (No guard: `index` enumerates
            # `result`, so it can never exceed len(result) — the condition that
            # used to wrap this was always true.)
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
            # Hour-long cache, not the week default: author records self-heal
            # server-side and a week-long replay hides the healed bio/portrait;
            # an operator's Refresh (force) bypasses even the hour
            # (see author_update_cache_time).
            request = str(make_request(
                update_url, cache_time=author_update_cache_time(helper.force)))
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
        # On a forced refresh the author-art convergence runs BEFORE the offer
        # phase, deliberately. The container cannot un-offer what the same
        # pass already offered: proven live on Ernest Cline (2026-07-27,
        # v1.3.141) -- converge's validate_keys prune logged success, but the
        # framework serialized the just-added dict entry regardless, and the
        # picker showed the freshly-selected image twice for a pass. With the
        # convergence FIRST, the offer phase's poster-state read (below) sees
        # the new upload as the selection, the cross-source dedupe withholds
        # the container copy of the same bytes, and the keep-list prunes the
        # stored stale entry: single-pass clean, entirely through the
        # already-proven offer machinery.
        #
        # The dims are pre-fetched here because the offer phase has not
        # measured anything yet; fetch_url_bytes rides the framework HTTP
        # cache (week default), so the offer phase's own fetch of the same
        # URLs moments later is served from cache, not the network.
        #
        # Also why this is force-gated: on a REFRESH the container cannot move
        # Plex's persisted selection, so the upload/select API owns it -- and
        # the unpin direction must run even when the record has no Hardcover
        # image left, or an uploaded portrait becomes permanently stuck.
        # Bytes retained past the dims measurement: the identical-pair rail
        # below needs to compare them. None on non-force passes -- the rail
        # fails open there and the pair heals on any forced refresh.
        thumb_prefetch = None
        secondary_prefetch = None
        if helper.force:
            if helper.thumb:
                thumb_prefetch = fetch_url_bytes(helper.thumb)
                if thumb_prefetch is not None:
                    thumb_dims = remember_dims(helper.thumb, thumb_prefetch)
            if helper.thumb_secondary:
                secondary_prefetch = fetch_url_bytes(helper.thumb_secondary)
                if secondary_prefetch is not None:
                    secondary_dims = remember_dims(helper.thumb_secondary, secondary_prefetch)
            if prefer_hardcover:
                select_hardcover_author_art(helper)
            elif Prefs['prefer_square_author_art'] and helper.thumb_secondary:
                # Converge an already-scanned artist onto whichever portrait
                # fills the square tile better (default-on since v1.3.132).
                # The container ordering below only decides on a FRESH scan,
                # so without this an existing library never benefits.
                #
                # Default-on also means this branch swallows every two-image
                # author -- so when the fit has NO VERDICT (unmeasurable or
                # identical dims), fall through to the pre-1.3.132 remedy:
                # a stuck agent-upload pin still reverts to the Audible
                # photo. With a verdict, the convergence IS the policy and
                # must not be undone.
                if not select_best_fit_author_art(
                    helper, thumb_dims, secondary_dims
                ):
                    unpin_hardcover_author_art(helper)
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
        # One container-state read for the cross-source dedupe on BOTH provider
        # images (v1.3.133): the pirate aba case, where the agent's portrait is
        # byte-identical to the operator's selected upload and the picker shows
        # the same face twice. None (fresh scan, sealed sandbox) fails open --
        # both images offered exactly as before. Read AFTER the convergence
        # above, so a just-landed upload is visible to the dedupe this pass.
        author_dup_state = None
        if helper.thumb or helper.thumb_secondary:
            author_dup_state = read_poster_state(
                helper.metadata.guid, 'incipit author-offer')
        thumb_dup = False
        # The identical-pair rail, THUMB side (measured live on Aleron Kong,
        # 2026-07-27, after the v1.3.143 secondary-side guard alone failed):
        # when both provider images are the same picture AND the current
        # selection is the SECONDARY's own container entry, the secondary
        # guard's selection rail refuses to withhold it -- correctly: Plex
        # retains a selected entry regardless of what the agent lists, so
        # withholding the selection achieves nothing. The withholdable copy
        # of the pair is the THUMB. Reuses the thumb_dup flow: skipped from
        # the offer and from the membership list.
        if (thumb_prefetch is not None and secondary_prefetch is not None
                and author_dup_state is not None
                and same_image(thumb_prefetch, secondary_prefetch)
                and author_dup_state[1] == own_container_key(helper.thumb_secondary)):
            thumb_dup = True
            log.info(
                'incipit author-offer: the primary is the same picture as '
                'the SELECTED secondary -- not listing it twice'
            )
        # Hoisted: offer_secondary_author_poster compares the secondary's
        # bytes against these (the Kong identical-pair guard); stays None on
        # every path that does not fetch, and the guard fails open on None.
        thumb_data = None
        if helper.thumb:
            if helper.thumb not in helper.metadata.posters or helper.force:
                # Bytes, not the HTTPRequest wrapper -- see the twin call in
                # offer_secondary_author_poster. thumb_dims below is measured
                # from this, and a wrapper silently measured as None.
                thumb_data = fetch_url_bytes(helper.thumb)
                if thumb_data is not None:
                    # Measured while the bytes are in hand; see
                    # better_square_portrait for what it decides. Measured even
                    # for a skipped duplicate: the select machinery below
                    # compares by URL bytes, not container membership.
                    thumb_dims = remember_dims(helper.thumb, thumb_data)
                    # `not thumb_dup`: the identical-pair rail above may have
                    # already withheld the thumb -- its verdict must not be
                    # overwritten by a scan that skips incipit keys and so
                    # cannot see that duplicate at all.
                    if not thumb_dup:
                        thumb_dup = duplicate_shown_elsewhere(
                            author_dup_state, thumb_data, helper.thumb,
                            'incipit author-offer')
                if thumb_data is not None and not thumb_dup:
                    helper.metadata.posters[helper.thumb] = Proxy.Media(
                        thumb_data, sort_order=0
                    )
                    thumb_added = True
            else:
                thumb_added = True
                # No fetch this pass, so recall the measurement -- otherwise
                # the reorder below is dims-blind on every pass after the
                # first and re-asserts DEFAULT order, which is exactly how
                # multi-book authors ended on the Audible image regardless of
                # what pass 1 correctly decided (see IMAGE_DIMS_MEMO).
                thumb_dims = IMAGE_DIMS_MEMO.get(helper.thumb)
        if helper.thumb:
            # A thumb withheld as a cross-source duplicate stays OUT of the
            # membership list too, or its stale entry would linger as the very
            # tile the skip removed.
            valid_posters = [] if thumb_dup else [helper.thumb]
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
                    helper, valid_posters, dup_state=author_dup_state,
                    thumb_data=thumb_data
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
            # The only prune in this file that logged NOTHING at any level.
            # It withholds artist poster keys, so when it drops the wrong one
            # there was no line to grep for afterwards.
            #
            # ...and the first line written to fix that was almost as useless:
            # it fired on EVERY artist update, including the common two-key
            # no-op, and reported len(valid_posters) -- the KEPT count -- so it
            # named neither what was dropped nor what survived. A prune log
            # exists to make a WRONG DROP greppable, which takes the KEY. Say
            # it only when the set actually shrinks, and say which one went.
            #
            # Membership is asked of the container (the same `in` test the
            # sibling prunes use) rather than iterating it: it arrives
            # deserialized and may carry a sibling library's entries, and this
            # plugin has never iterated it in the sandbox.
            offered = []
            for key in (helper.thumb, helper.thumb_secondary):
                if not key or key in offered:
                    continue
                try:
                    present = key in helper.metadata.posters
                except Exception:
                    present = False
                if present:
                    offered.append(key)
            withheld = author_art_withheld(offered, valid_posters)
            if withheld:
                log.warn(
                    'incipit artist-art: pruning %s author poster key(s) from the '
                    'container -- withheld: %s -- keeping: %s',
                    len(withheld), ' | '.join(withheld),
                    ' | '.join(valid_posters) if valid_posters else '(nothing)'
                )
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
            display_name, display_year = search_helper.quick_match_display(
                quick_match_asin)
            results.Append(
                MetadataSearchResult(
                    id=quick_match_asin,
                    lang=lang,
                    name=display_name,
                    score=100,
                    year=display_year
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

        # Self-check BEFORE writing metadata: compare the analyzed audio against
        # the runtime of the record we are about to apply. Costs nothing (both
        # numbers are already in hand) and turns the manual 2026-08-09 sweep --
        # which found 52 wrong-edition albums -- into something the agent does
        # on every refresh, by itself.
        update_helper.report_runtime_mismatch()

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

            # Print separators for easy reading. (No guard: `index` enumerates
            # `result`, so it can never exceed len(result) — the condition that
            # used to wrap this was always true.)
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
            # Week-long cache for a scan, but an operator's Refresh (force)
            # bypasses it -- without that, a corrected API record stayed
            # invisible for up to a week and no operator action could surface it
            # (see book_update_cache_time).
            request = str(make_request(
                update_url, cache_time=book_update_cache_time(helper.force)))
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

        # LOCAL COVER. Proxy.LocalFile is REJECTED by the posters container
        # (proven in 1.3.23-1.3.27) and the default sandbox blocks open()/Core,
        # so the agent could not read the sidecar at all. Info.plist's
        # PlexPluginCodePolicy=Elevated unlocks Core.storage, and Proxy.Media
        # (BYTES) IS accepted here — so cover.jpg is read and served as our own
        # poster at sort_order=0, then pruned to, and the local cover becomes
        # the default even with Incipit ABOVE Local Media Assets (titles stay
        # clean). local_cover_bytes() swallows every failure, so a sealed
        # sandbox simply yields None and we fall through to the online cover.
        #
        # (This was written as an "attempt" that might replace an external
        # select_cover_poster.py script. It worked, shipped, and has been the
        # mechanism ever since — the speculative framing outlived its truth.)
        #
        # Deliberately OUTSIDE any `if helper.thumb:` gate: a record with no
        # online image (Hardcover/OpenLibrary book-level matches have none) used
        # to skip this whole path, leaving a prefer_local book with a perfectly
        # readable cover.jpg and NO poster at all on a normal incremental scan.
        # The local cover does not depend on the online one existing.
        local_set = False
        # Set when the mirror offer was withheld because another source's
        # poster already displays cover.jpg's exact bytes (v1.3.133 dedupe).
        # Consulted by the membership list at the end of the block, so the
        # stale mirror entry is PRUNED rather than lingering as the duplicate
        # it was skipped to avoid. Initialized here because the offer code
        # only runs on some paths and the keep-list runs on all of them.
        mirror_skipped = False
        # Was that skip byte-exact? Only then may the mirror entry be PRUNED
        # (a perceptual verdict withholds the offer but never deletes).
        mirror_byte_exact = False
        # The poster-container state, fetched at most once per pass: the
        # mirror leg reads it when cover.jpg exists, and the online leg's
        # cross-source check (v1.3.152) needs it even when cover.jpg is
        # absent -- the embedded-art-equals-online-cover pair has nothing to
        # do with the local file.
        dup_state = None
        # Its sibling for the ONLINE copy: set when the online cover's bytes
        # are already on display (via the local mirror, or via the source
        # that caused mirror_skipped), so the offer is withheld and the
        # stale entry pruned. Carried in the album memo like the rest -- a
        # sibling track has no bytes to re-judge with, and failing open
        # there would re-offer the very duplicate the first track removed.
        online_redundant = False
        # ...and the evidence behind it. Only BYTE identity licenses a prune,
        # and only a non-selected entry may be pruned at all; both were
        # re-derived (wrongly) on sibling tracks before 2026-07-28.
        online_byte_exact = False
        online_prune_ok = True
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
        # below reads the square online cover's BYTES, but the only assignment
        # sits under `if helper.thumb:`. Defensive rather than a measured fix --
        # that caller is reached only when deferred_portrait_local is set, which
        # already requires helper.thumb -- but the two conditions are 150 lines
        # apart, and an unbound name here is a NameError mid-update, not a
        # missing poster.
        thumb_data = None
        # Hoisted out of the prefer_local branch: the container-membership check
        # further down runs under `if helper.thumb:`, which is NOT nested inside
        # it, so leaving the assignment there was a NameError whenever the pref
        # was off.
        local_key = 'incipit-local-cover'
        # Consulted for BOTH pref states (v1.3.162): with prefer_local OFF
        # the cross-source leg (v1.3.152) still computes the online flags and
        # downloads the online cover per track on force -- the write-site
        # comment's "with it off, none of these flags can be set" stopped
        # being true the day that leg landed. Worse than the wasted fetches:
        # a sibling running with DEFAULT flags after track 1 pruned a
        # redundant online entry would compute keep-list membership with
        # online_redundant=False and resurrect the very tile track 1 removed.
        remembered = album_cover_decision(helper.metadata.guid, helper.force)
        if remembered is not None:
            # A sibling track already did the reads and the offers this
            # pass, and the container survives between tracks -- only the
            # FLAGS need restoring. Without this, a dup-skip (which never
            # adds our key) defeated the membership guard below and every
            # track of a curated album re-paid the whole read/fetch bill.
            local_set = remembered.get('local_set', False)
            mirror_skipped = remembered.get('mirror_skipped', False)
            mirror_byte_exact = remembered.get('mirror_byte_exact', False)
            deferred_portrait_local = remembered.get(
                'deferred_portrait_local', False)
            poisoned_local = remembered.get('poisoned_local', False)
            online_redundant = remembered.get('online_redundant', False)
            online_byte_exact = remembered.get('online_byte_exact', False)
            online_prune_ok = remembered.get('online_prune_ok', False)
        elif prefer_local:
            # NO container-membership fast-path here. The memo above is the
            # only honest "this pass already did the work" signal: the
            # container arrives DESERIALIZED, and with two libraries side by
            # side it arrives pre-populated with a SIBLING library's entries
            # (bundles are shared per guid) -- measured live on the Testing
            # library 2026-07-27, an entire fresh scan performed ZERO
            # cover.jpg reads because every inherited entry satisfied the old
            # membership check, and no book received its local cover. Cost of
            # the honest signal: one cover.jpg read per book per pass past
            # the memo TTL -- which also means a replaced cover.jpg now takes
            # effect on ANY refresh, not only a forced one.
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
            # Cross-source dedupe (v1.3.133): when an upload or Local Media
            # Assets already displays these exact bytes -- and neither the
            # fresh-scan anchor nor the selection needs OUR copy (see the
            # rails in duplicate_shown_elsewhere) -- offering the mirror
            # just lists the same picture twice. mirror_skipped also keeps
            # the stale mirror entry OUT of the membership list below, so
            # the old duplicate tile is pruned rather than lingering.
            if cover_bytes:
                dup_state = read_poster_state(
                    helper.metadata.guid, 'incipit cover-offer')
                mirror_skipped, mirror_byte_exact = mirror_withheld(
                    dup_state, cover_bytes, local_key, 'incipit cover-offer')
            if cover_bytes and not mirror_skipped:
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
                        # This is a PRUNE as well as a default: validate_keys
                        # with one key withholds every other entry in our
                        # namespace. The old line said only "set as the default
                        # poster", so the removal it performs left no trace to
                        # grep when it took the wrong tile with it.
                        log.warn('incipit cover: LOCAL cover set as the default '
                                 'poster -- pruning our other container entries '
                                 'to it (%s)', local_key)
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
        # Bound BEFORE the branch. `alternate_keys` is assigned inside the
        # `if helper.thumb:` arm below, but it is READ further down by the
        # local-cover prune, which runs unconditionally after the whole
        # if/elif chain. With no online thumb -- a book whose art is a local
        # cover.jpg only -- the assignment never ran and that read raised
        # UnboundLocalError, outside the try that follows it, aborting update()
        # for exactly the books the local-cover path exists to serve. The same
        # call defends against a falsy helper.thumb one argument earlier
        # (`thumb_present=bool(helper.thumb)`), which makes this a slip rather
        # than an assumption.
        alternate_keys = []
        if helper.thumb:
            if remembered is not None:
                # A sibling track already fetched, judged, and offered (or
                # withheld) the online cover this pass; online_redundant came
                # back with the other flags. Re-running the offer here with no
                # bytes in hand would fail open and re-offer the very
                # duplicate the first track suppressed.
                pass
            elif helper.thumb not in helper.metadata.posters or helper.force:
                thumb_data = fetch_url_bytes(helper.thumb)
                # Offering an online cover that is byte-identical to the local
                # cover.jpg just lists the SAME picture twice in the picker (Plex
                # keys each source separately, so one image can appear under our
                # online key, our local key, our upload, and Local Media Assets'
                # own entries). Skip the redundant one -- ALSO when the local
                # copy itself was withheld as a cross-source duplicate
                # (mirror_skipped): the bytes are on display either way, and
                # keying this on local_set alone meant the very pass that
                # suppressed the local copy re-offered the identical online one.
                # Re-evaluated on every refresh against the CURRENT file, so
                # replacing cover.jpg with a different image makes the online
                # cover an option again -- the alternative stays available
                # exactly when it is actually an alternative.
                if dup_state is None:
                    dup_state = read_poster_state(
                        helper.metadata.guid, 'incipit cover-online')
                online_redundant, online_byte_exact = online_offer_redundant(
                    thumb_data, cover_bytes, local_set, mirror_skipped,
                    dup_state, helper.thumb
                )
                # Decided HERE, where the container state is in hand: a
                # sibling track has no state, and re-deriving it there made
                # the rail permissive on 26 of a 27-part book's tracks.
                online_prune_ok = online_prune_allowed(dup_state, helper.thumb)
                if online_redundant:
                    log.info(
                        'incipit cover: this picture is already displayed '
                        '-- not offering the online copy'
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
            # Extra marketplace art, offered before the membership lists are
            # computed so both of them can carry the keys. Never the default
            # (sort_order well below), and never the selection.
            # `shown` is what is ALREADY on display: the online cover we just
            # offered and the local cover.jpg. An alternate matching either is
            # a twin tile, not a choice -- see alternate_already_on_display.
            alternate_keys = offer_alternate_covers(
                helper, shown=(thumb_data, cover_bytes))

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
                # One decision, one function -- see cover_keep_list for the
                # two rules (prune only on byte identity, never prune the
                # selection) and why it is not inline any more.
                keep = cover_keep_list(
                    thumb_key=helper.thumb, local_key=local_key,
                    thumb_present=True,
                    local_present=local_key in helper.metadata.posters,
                    online_redundant=online_redundant,
                    online_byte_exact=online_byte_exact,
                    online_prune_ok=online_prune_ok,
                    mirror_skipped=mirror_skipped,
                    mirror_byte_exact=mirror_byte_exact,
                    alternate_keys=alternate_keys)
                if helper.thumb not in keep:
                    log.warn(
                        'incipit cover: pruning our online cover entry '
                        '(byte-identical to a poster already displayed)')
                if local_key in helper.metadata.posters and local_key not in keep:
                    log.warn(
                        'incipit cover: pruning our local-mirror entry '
                        '(byte-identical to a poster already displayed)')
                try:
                    helper.metadata.posters.validate_keys(keep)
                except Exception as e:
                    log.error('incipit cover: membership prune failed (%s)', e)
        elif (
            mirror_skipped and mirror_byte_exact
            and local_key in helper.metadata.posters
            # ...and the mirror is not itself the selection. The old
            # justification -- "mirror_skipped guarantees the selection is a
            # non-incipit key" -- is false: duplicate_shown_detail SKIPS
            # incipit keys while scanning rather than proving none is
            # selected, so a book re-matched to a thumb-less record could
            # evict the very entry the operator picked (2026-07-28 review).
            and local_prune_allowed(dup_state, local_key)
        ):
            # A thumb-less record (Hardcover/OpenLibrary book-level match)
            # never enters the membership pass above -- but its stale mirror
            # entry is exactly the duplicate the skip exists to remove, and
            # it was skipped-but-never-pruned, keeping the duplicate tile
            # forever. mirror_skipped guarantees the selection is a
            # non-incipit key, so pruning our namespace cannot touch it.
            # BYTE identity only: on a mere perceptual verdict this is the
            # operator's curated cover.jpg being deleted from the picker.
            try:
                helper.metadata.posters.validate_keys([])
                # WARN: this empties the whole incipit namespace, which on a
                # thumb-less (Hardcover/OpenLibrary) match can take the
                # operator's curated cover.jpg entry with it.
                log.warn(
                    'incipit cover: pruned the stale local-mirror entry '
                    '(no online cover to anchor the keep-list)'
                )
            except Exception as e:
                log.error('incipit cover: stale-mirror prune failed (%s)', e)

        # Carry this track's decisions to its siblings -- AFTER the online
        # block, so online_redundant is part of the record. Only when this
        # track actually computed them (a memo hit changes nothing). For BOTH
        # pref states: an earlier gate said "with prefer_local off, none of
        # these flags can be set", which the v1.3.152 cross-source leg made
        # false -- the online flags are computed either way, and without the
        # record every sibling re-fetched the online cover and re-swept the
        # container on a forced pass of a multi-file book.
        # Recorded even when cover.jpg was absent: "there is nothing to do"
        # is also a decision the other 26 tracks should not re-derive.
        if remembered is None:
            remember_album_cover_decision(
                helper.metadata.guid, helper.force,
                {'local_set': local_set,
                 'mirror_skipped': mirror_skipped,
                 'mirror_byte_exact': mirror_byte_exact,
                 'deferred_portrait_local': deferred_portrait_local,
                 'poisoned_local': poisoned_local,
                 'online_redundant': online_redundant,
                 'online_byte_exact': online_byte_exact,
                 'online_prune_ok': online_prune_ok})

        # Local cover, force-select via the trusted Plex API so a dropped/replaced
        # cover.jpg takes effect on Refresh Metadata even on an ALREADY-scanned
        # book -- the posters-container path above only wins on a fresh scan.
        # SMB-safe (writes to Plex's metadata store, not the media folder).
        # Skipped when a portrait local cover was deferred: re-reading it here
        # would re-impose the print jacket the block above deliberately declined.
        # Skipped for a poisoned local cover too -- select_local_cover refuses the
        # artist photo on its own, but not asking saves two round trips and keeps
        # the reason in one place.
        # The shared gate stated ONCE. Both branches want the same four
        # conditions and differ only on which way the portrait deferral went;
        # spelling them out twice meant four conditions to keep in sync.
        # helper.force is what makes cover_bytes freshly read, so neither branch
        # has anything to compare against on an incremental pass.
        #
        # The recovery check is asked LAST and only when the cheap prefs
        # already pass, so a library with prefer_local_cover off still pays
        # nothing -- and it hands BACK the poster state it read, which
        # select_local_cover below would otherwise re-read for the same guid
        # with nothing changed in between (4 cache-disabled localhost GETs
        # where 2 will do).
        want_local = Prefs['prefer_local_cover'] and not poisoned_local
        recovery_state = None
        if want_local and not helper.force:
            want_local, recovery_state = local_cover_recovery_needed(helper)
        if want_local:
            if not deferred_portrait_local:
                # PRUNE OUR TWIN. When the upload really does hold the selection,
                # our container copy of the SAME cover.jpg is a second,
                # indistinguishable tile -- measured at 33 of 33 albums on a cold
                # library (section 54, 2026-07-29), always the pair
                # `(upload) + com.plexapp.agents.incipit`. The agent cannot delete
                # an upload, so the container entry is the only removable copy,
                # and it is the right one: the upload is what is selected.
                #
                # Gated on the return value, NOT assumed: a stand-down (the
                # operator's own pick is showing) reports False and the entry
                # stays, because it is then their only route back to local art.
                #
                # Plain-pass recovery (v1.3.190): without force the hoisted
                # read above did not run, so cover_bytes is None and the
                # portrait/poison flags could not have seen the file -- read
                # here and RE-APPLY the portrait deferral before any select,
                # or recovery would re-impose exactly the print jackets the
                # force path declines. (Poison needs no re-check: the guard
                # lives inside select_local_cover and fails closed.)
                recovery_bytes = cover_bytes
                if recovery_bytes is None:
                    recovery_bytes = local_cover_bytes(helper)
                    if recovery_bytes is not None and local_cover_is_portrait(recovery_bytes):
                        recovery_bytes = None
                if recovery_bytes is not None and should_prune_local_twin(
                    select_local_cover(helper, recovery_bytes,
                                       state=recovery_state),
                    local_key in helper.metadata.posters
                ):
                    keep = cover_keep_list(
                        thumb_key=helper.thumb, local_key=local_key,
                        thumb_present=bool(helper.thumb)
                        and helper.thumb in helper.metadata.posters,
                        local_present=True,
                        online_redundant=online_redundant,
                        online_byte_exact=online_byte_exact,
                        online_prune_ok=online_prune_ok,
                        mirror_skipped=mirror_skipped,
                        mirror_byte_exact=mirror_byte_exact,
                        local_uploaded=True,
                        alternate_keys=alternate_keys)
                    try:
                        helper.metadata.posters.validate_keys(keep)
                        log.warn('incipit cover: pruned our local-cover container '
                                 'entry -- the uploaded copy holds the selection')
                    except Exception as e:
                        log.error('incipit cover: twin prune failed (%s)', e)
            else:
                # The MIRROR of that call. When the jacket was deferred, the
                # container said "use the square" and Plex ignored it, because a
                # container cannot move a selection it persisted on an earlier
                # scan. Say it through the upload lever instead, which can --
                # otherwise the deferral is correct and powerless forever, which
                # is exactly the 3 albums measured frozen on a portrait.
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
        # The portrait deferral does not gate the mirror at all any more. It
        # first gated it wholesale, which swallowed every deliberate pick on a
        # portrait book (Joseph Bridgeman: hand-picked poster, Refresh, nothing
        # on disk, no log). It then refused just the deferred-to online default
        # -- but that refusal only ever protected a file the agent had itself
        # measured as a print jacket and refused to display, so v1.3.121 dropped
        # it and the parameter with it.
        #
        # What reaches disk is now governed by cover_mirror_mode (v1.3.125, the
        # gate lives inside the function): seed-only by default, so a scan can
        # SEED absent covers but never replace an existing one; full mirroring
        # only in declared Curation sessions.
        if helper.force:
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
        # No `encoding=` kwarg. Py2's json.loads accepted (and ignored, for str
        # input) one; py3 REMOVED it, so under the py3 test harness every call
        # raised TypeError — which this clause does not catch — making the real
        # decode path permanently untestable while production quietly worked.
        # A str/unicode body decodes identically on both without it.
        return json.loads(output)
    except (AttributeError, ValueError):
        # ValueError: malformed/empty/HTML body (e.g. an API 500 page, or the
        # "None" string when make_request returned nothing).
        return None


# Third-party hosts that serve ONLY images, and so earn a lighter pace than the
# flat 1s make_request gives every other third party.
#
# MEASURED, not guessed: every alternate-cover url the api served across a
# 120-book sample of the live library (2026-08-12) resolved to one of these six
# hosts -- m.media-amazon.com (78), assets.hardcover.app (77), img1/2/3.od-cdn.com
# (49), is1-ssl.mzstatic.com (8) -- and the PRIMARY cover url lands on the first
# two as well. The Amazon `ssl-images` spelling is the one entry here NOT in that
# sample; it is Audible's older image host, included because it serves the same
# art from the same CDN and would otherwise silently keep the 1s pace.
#
# Suffixes, matched on a dot boundary, because these are sharded: od-cdn.com
# fronts img1/img2/img3 and mzstatic fronts is1-ssl/is2-ssl/..., so pinning the
# exact hostnames would pace two thirds of OverDrive at 1s for no reason.
#
# WHY THIS IS SAFE, stated plainly: the 1s pace exists so a cold scan cannot
# hammer or get throttled by a third party. That risk is real for an API that
# meters you (Audible/audnexus/Hardcover) and negligible for an unauthenticated
# image CDN built to serve exactly this. The scan is serialized, so even at a
# zero gap it would issue at most ~4 requests/second -- the download itself
# measured 229ms mean. Nothing here is an API: adding a host that answers
# queries, rather than one that returns bytes for a picture, is the mistake this
# list must not make.
IMAGE_CDN_SUFFIXES = (
    'media-amazon.com',
    'ssl-images-amazon.com',
    'assets.hardcover.app',
    'od-cdn.com',
    'mzstatic.com',
)

# A MINIMUM GAP, not a blind sleep. A flat sleep would also be paid on a plugin
# HTTP-cache hit and on every url that happens to arrive slowly -- and the cover
# path re-asks for the same thumb more than once per album, so that would have
# handed back a chunk of what this change saves. Pacing only when we are
# actually going fast costs nothing when we are not.
IMAGE_CDN_MIN_GAP = 0.25
# A dict rather than a module global rebound inside the function: the sandbox is
# fine with mutating a container, and this needs no `global` statement.
image_cdn_pace_state = {'last': 0.0, 'announced': False}

# Scheme, then everything up to the first /, ? or # -- the AUTHORITY, which is
# not yet the host.
URL_AUTHORITY_RE = re.compile(r'^[a-z][a-z0-9+.-]*://([^/?#]*)', re.I)


def url_host(url):
    """
        The lowercased hostname of `url`, or '' when there is not one.

        Hand-rolled because the obvious import is not portable here: py2 has
        `urlparse.urlparse` and py3 has `urllib.parse.urlparse`, and this module
        is compiled by BOTH (Plex runs py2.7, the test harness is py3). `re` is
        already imported and behaves identically on each.
        @param url the url to read
        @returns the hostname, lowercased, or ''
    """
    try:
        match = URL_AUTHORITY_RE.match(url.strip())
    except Exception:
        return ''
    if not match:
        return ''
    authority = match.group(1)
    # USERINFO FIRST, and on the LAST '@'. "https://m.media-amazon.com@evil.example/x.jpg"
    # has a host of evil.example, so a left-to-right read hands back the decoy --
    # and these urls come straight out of a JSON response body (imageAlternates),
    # which is exactly the reachability test_api_host.py documents for the same
    # class of check.
    if '@' in authority:
        authority = authority.rsplit('@', 1)[1]
    # An IPv6 literal is bracketed and full of colons; only a colon OUTSIDE the
    # brackets is a port.
    if authority.startswith('['):
        end = authority.find(']')
        if end != -1:
            authority = authority[:end + 1]
    elif ':' in authority:
        authority = authority.split(':', 1)[0]
    # A trailing dot is a legal absolute FQDN ("m.media-amazon.com.") and would
    # otherwise miss every suffix below.
    return authority.lower().rstrip('.')


def is_image_cdn_host(url):
    """
        True when url targets a known image-only CDN, which is paced lighter.

        The DOT BOUNDARY is the whole guard, exactly as in is_api_host: a bare
        `endswith('od-cdn.com')` also accepts "evil-od-cdn.com", and a bare
        substring test accepts "od-cdn.com.attacker.example". Getting this wrong
        does not leak a secret -- there is no token on this path any more -- it
        silently drops an attacker-shaped host to a lighter pace, which is a
        smaller harm than is_api_host's but not one to hand over for free.
        @param url the url to classify
        @returns True when the host is a known image CDN
    """
    host = url_host(url)
    if not host:
        return False
    for suffix in IMAGE_CDN_SUFFIXES:
        if host == suffix or host.endswith('.' + suffix):
            return True
    return False


def pace_image_cdn():
    """
        Hold IMAGE_CDN_MIN_GAP between image-CDN fetches, and no longer.

        Deliberately NOT the framework's `sleep=` argument. That one takes the
        whole pause as a number we hand it, and whether it accepts a FRACTION is
        a framework detail this bundle cannot see or test -- if it floors to an
        int, 0.25 silently becomes no pacing at all. Doing it here keeps the
        argument an integer the framework has always been given, and makes the
        gap something the test suite can assert.
    """
    # ONCE per plugin life, not per fetch: this fires thousands of times in a
    # scan. It exists because the effect of this path cannot be observed from
    # the outside -- a scan that is merely faster looks the same as one where
    # the branch never ran -- and because a bundle change needs one log string
    # only the new version can emit, for load proof under log rotation.
    if not image_cdn_pace_state['announced']:
        image_cdn_pace_state['announced'] = True
        log.info('incipit pace: image-CDN gap %ss engaged (third parties keep 1s)',
                 IMAGE_CDN_MIN_GAP)
    now = time()
    waited = now - image_cdn_pace_state['last']
    if waited < IMAGE_CDN_MIN_GAP:
        sleep(IMAGE_CDN_MIN_GAP - waited)
        # Re-read: the sleep may overshoot, and stamping the pre-sleep time
        # would let the NEXT call fire early by however much it overshot.
        now = time()
    image_cdn_pace_state['last'] = now


def is_api_host(url):
    """
        True when url targets the configured incipit-api host (our own local,
        allowlisted service) rather than a third party (Audible/audnexus in
        stock mode, or an Amazon image CDN).

        THE SEPARATOR IS THE WHOLE GUARD. A bare startswith on the base has no
        boundary after it, so a base of "http://incipit-api" -- a plausible
        container/service-name setting, and the pref is free text -- also
        accepts "http://incipit-api.attacker.example/x.jpg". make_request acts
        on the answer in ways that must not reach a third party: it drops the
        framework's 1s pacing, and it aborts the retry ladder on a 4xx that only
        our own API is trusted to mean. (It used to guard a second caller too --
        incipit_headers, which attached the operator's Hardcover token. That is
        GONE as of 1.3.206: every deployment self-hosts the API with its own
        HARDCOVER_TOKEN, so forwarding a personal key from Plex's plaintext
        prefs on every request bought nothing.) And the URLs reaching here are
        not all ours --
        the alternate-cover path fetches urls straight out of a JSON body.
        Same shape with a port: base "http://host:3737" would match
        "http://host:37370/...".

        Requiring "/" costs nothing: every api url in this plugin is built as
        `base.rstrip('/') + '/' + ...` (region_tools.get_api_search_url and the
        callers that follow it), so a legitimate url always continues with the
        separator. A bare `base` with nothing after it is accepted too.
    """
    base = Prefs['api_base_url']
    if not base or not url:
        return False
    base = base.rstrip('/')
    return url == base or url.startswith(base + '/')


# Longest we will honour from a Retry-After. The limiter's window is a minute,
# so a full wait is the useful case; anything beyond that is a server asking for
# a backoff longer than a scan can absorb, and the ladder's own timing is a
# better answer than stalling Plex's update window indefinitely.
MAX_RETRY_AFTER = 60


def retry_after_seconds(err):
    """
        Seconds a 429 asked us to wait, or None when it did not say.

        Only the delta-seconds form is read. @fastify/rate-limit (what our own
        API runs) sends an integer, and an HTTP-date would need timezone-aware
        parsing that buys nothing here -- returning None just falls back to the
        ladder's own backoff, which is the safe direction.

        getattr is BLOCKED by the RestrictedPython sandbox, so every access is a
        plain attribute read inside try/except.
    """
    try:
        raw = err.headers.get('Retry-After')
    except Exception:
        return None
    if raw is None:
        return None
    try:
        secs = int(str(raw).strip())
    except Exception:
        return None
    if secs < 0:
        return None
    return secs


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
    # sleep=0 ONLY for our own local, allowlisted API — the framework's per-fetch
    # 1s pause is the largest fixed cost of a cold scan there. Third-party hosts
    # (Audible/audnexus in stock mode) KEEP the pacing so an unpaced cold scan
    # can't hammer or get throttled by them.
    #
    # IMAGE CDNs sit between the two. Measured 2026-08-12 against the live
    # library: 83% of books carry alternate covers averaging 1.88 urls each, and
    # with the primary cover on the same hosts the flat 1s pace added ~50-63
    # minutes to a cold scan of 1650 albums -- 81% of that cost being the sleep
    # itself, not the 229ms download. They are unauthenticated CDNs built to
    # serve pictures, so they earn a lighter pace than an API that meters us; a
    # serialized scan cannot burst past ~4 requests/second regardless.
    fetch_sleep = 1
    if is_api_host(url):
        fetch_sleep = 0
    elif is_image_cdn_host(url):
        # The framework argument stays an INTEGER it has always been given; the
        # sub-second part is ours, so it is testable and cannot be floored away.
        fetch_sleep = 0
        pace_image_cdn()
    sleep_time = 1
    num_retries = 4
    response = None
    for attempt in range(0, num_retries):
        try:
            response = HTTP.Request(
                url, cacheTime=cache_time,
                timeout=90, sleep=fetch_sleep)
            # FORCE THE FETCH INSIDE THE LADDER.
            #
            # HTTP.Request returns Plex's LAZY wrapper -- the network call does
            # not happen until .content/str(). This documents it twice already
            # (fetch_url_bytes' docstring, and the 2026-07-25 measurement where
            # the un-fetched wrapper reached image_dimensions and raised
            # 'HTTPRequest object has no attribute __getitem__'). So an HTTP
            # status error was raised at the CALLER's str(), outside this try,
            # and every retry and every 4xx/5xx decision below was dead code for
            # exactly the transients they were written for.
            #
            # Proven from production, not inferred: across four agent log files
            # 'Failed http request attempt' appears ZERO times while 55 HTTP
            # errors surfaced at call sites -- 'incipit book fetch failed for
            # <url> ... HTTP Error 404'. The ladder had never once fired.
            #
            # Touching .content here costs nothing: all eight call sites already
            # consume it (six via str(), two via .content), and the wrapper
            # memoises, so the caller's later read is free.
            if response is not None:
                response.content
            break
        except Exception as err:
            # DISCARD the wrapper the failed attempt left behind. Under the lazy
            # model HTTP.Request SUCCEEDS and only .content raises, so `response`
            # is already bound to a poisoned wrapper -- returning it after the
            # ladder gives up hands the caller an object that throws on read
            # instead of the None every call site checks for. (The eager model
            # never had this: the constructor raised, so response stayed None.)
            response = None
            log.error(
                "Failed http request attempt #%d: %s" % (attempt + 1, url))
            log.error(err)
            # An ANSWERED 4xx FROM OUR OWN API is a permanent no: the server
            # parsed the request and rejected it, so retrying with backoff
            # burns ~7s per call teaching nothing (measured live on
            # /authors?name=4, answered 400). Everything else keeps the
            # ladder: transport failures carry no code, 5xx/429 are the
            # transients it exists for, 408/425 are 4xx by number but
            # transient by meaning, and THIRD-PARTY 4xx (Audible's edge
            # serves one-off bot-check 403s, image CDNs blip) are exactly
            # what the 2s retry has always absorbed -- aborting on those
            # turns a blip into an unmatched book for the whole pass.
            # err.code read without getattr (blocked in the sandbox).
            try:
                err_code = err.code
            except Exception:
                err_code = None
            # PERMANENT codes abort for ANY host; the rest of the 4xx range
            # aborts only for our own API.
            #
            # 404/410 mean the resource is gone and no amount of retrying
            # changes that, yet the abort used to require is_api_host(url) -- so
            # a rotted third-party image URL (the most common failure here: an
            # Amazon author photo that moved) ran the whole ladder. Four
            # attempts at timeout=90, plus 1+2+4s of backoff, plus the
            # framework's per-call pacing. compile_metadata calls
            # fetch_url_bytes up to four times per artist update on a force, so
            # one dead image cost tens of seconds PER ALBUM inside Plex's
            # bounded update window.
            #
            # 403 deliberately stays on the ladder for third parties: Audible's
            # edge serves one-off bot-check 403s, which is the case the ladder
            # was written for.
            permanent = err_code in (404, 410)
            answered_4xx = (
                err_code is not None and 400 <= err_code < 500
                and err_code not in (408, 425, 429)
            )
            if permanent or (answered_4xx and is_api_host(url)):
                break
            # A 429 TELLS US how long to wait; the ladder's 1/2/4s cannot
            # outlast a per-minute window, so obeying it is the difference
            # between succeeding and silently losing the record.
            #
            # Measured 2026-08-05 on a from-scratch rebuild of 1,591 albums:
            # Plex re-matches an album once per track, the scan sustained ~90
            # albums/min against a 100/min bucket, and this ladder spent all
            # four attempts inside ~7s and gave up. update() then kept whatever
            # the file tags said, so 34 albums ended up titled
            # '"The Way of Kings" by B.Sanderson w/ K.Reading' and three more
            # went unmatched -- damage that no later pass repairs on its own,
            # because Plex does not re-run update() for an album it considers
            # done.
            #
            # Waiting the stated time self-throttles the scan to the rate the
            # server will actually serve. Slower, and correct. The API-side
            # exemption for direct container-to-host calls should mean this
            # never fires against our own API again; it stays because a 429
            # from anywhere deserves the same treatment.
            wait = sleep_time
            if err_code == 429:
                stated = retry_after_seconds(err)
                if stated is not None:
                    wait = min(stated, MAX_RETRY_AFTER)
            # No point sleeping after the final attempt.
            if attempt < num_retries - 1:
                sleep(wait)
                # NOT `sleep_time *= 2`. RestrictedPython rejects that operator
                # -- "Operator '*=' is not supported" -- and this line sits
                # INSIDE the except handler, so the error it raises is not
                # caught by that same handler. It propagated straight out of
                # make_request, turning every retryable failure into an instant
                # hard one and reaching the caller as
                # "book fetch failed ... keeping existing metadata: Operator
                # '*=' is not supported", which names the wrong culprit.
                #
                # It was unreachable until v1.3.186. Before that HTTP.Request's
                # laziness meant errors surfaced at the CALLER's str(), so the
                # ladder never ran at all ("Failed http request attempt"
                # appeared ZERO times across four log files). Forcing .content
                # inside the try made the ladder live and woke this.
                # Measured on the 2026-08-05 rebuild: the ladder fired 56 times
                # and 13 of them -- every invocation that reached this line --
                # died right here.
                #
                # `+=` is fine (25 uses across search_tools/__init__, all in hot
                # paths); it is specifically the other operators the sandbox
                # refuses. tests/test_sandbox_operators.py pins this.
                sleep_time = sleep_time * 2
    return response
