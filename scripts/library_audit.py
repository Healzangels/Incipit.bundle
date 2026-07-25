#!/usr/bin/env python3
"""
Report library mess: duplicate books, mixed audio formats, junk series folders.

WHY THIS EXISTS
    Three problems that all look the same from the shelf and all come from the
    same place -- a library assembled from several sources over time:

      1. the SAME book present twice (sometimes under two different authors,
         which is why an artist-scoped check misses it: Mitch Rapp #14 sits
         under both "Vince Flynn" and "Kyle Mills")
      2. the same book as .mp3 AND .m4b, so one copy is dead weight
      3. series FOLDERS that are not series names -- a foreign-language title
         ("Der große Bruderkrieg" for The Horus Heresy), or a marketing blurb
         ("Thriller von Bestseller-Autor David Baldacci"). Those leak into the
         sort title whenever the provider has no series of its own.

READ-ONLY BY DESIGN
    Reports only, never writes or deletes. Which copy of a duplicate to keep is
    a judgement call -- bitrate, narrator, completeness -- so this prints the
    evidence (runtime, size, format, path) and stops. There is deliberately no
    --fix flag. Deleting the wrong copy is unrecoverable; a stale report is not.

USAGE (on the Plex box)
    python3 library_audit.py
    The token is read from Plex's own Preferences.xml; --token/PLEX_TOKEN
    override it. Everything comes from ONE Plex API call, so it is fast.
"""

import argparse
import collections
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TIMEOUT = 180

# Runtime agreement below which two files are almost certainly the same
# recording. Two DIFFERENT books sharing a title (Defiance of the Fall book 1
# and book 10 are both titled "Defiance of the Fall") differ by far more.
SAME_RUNTIME_TOLERANCE = 0.02

# Foreign function words, whole-token. Tokens that are also ordinary English
# words are excluded below, or every English title would match.
FOREIGN_WORDS = (
    'der die das des dem den ein eine und von vom im am grosse große krieg welt '
    'zeit buch roman geschichte bruder nacht blut '
    'les une dans avec pour sans guerre monde histoire livre nuit sang trone '
    'los las unos unas del guerra mundo historia libro noche sangre torre '
    'gli della delle degli mondo storia notte sangue '
    'het een oorlog wereld boek '
    'annales compagnie noire trilogia bruderkrieg'
).split()
AMBIGUOUS = set('a i die den is are no in on so to at be we me he it as or of and '
                'die dos as os las la le el il un une des van der den col gli'.split())

# Wording that marks a "series" folder as marketing copy rather than a series.
BLURB_RE = re.compile(
    r'\b(bestsell\w*|best[- ]selling|award[- ]winning|acclaimed|author|novels?\s+by|'
    r'series\s+by|thriller|from\s+the\s+(author|creator)|new\s+york\s+times|#\s*1)\b',
    re.I)

AUDIO_EXT_RE = re.compile(r'\.([A-Za-z0-9]{2,4})$')


def token_from_preferences(path):
    """The server's own Plex token, or '' (see poison_sweep.py for the rationale)."""
    try:
        with open(path) as fh:
            m = re.search(r'PlexOnlineToken="([^"]+)"', fh.read())
        return m.group(1) if m else ''
    except Exception:
        return ''


def fetch_albums(base, token):
    """
    ratingKey -> (guid, titleSort) for every album.

    The guid is what actually settles a duplicate: two rows with the SAME guid
    are the same edition, full stop. Runtime agreement is only a proxy, and it
    produces false positives -- Craig Alanson's "Ascendant" and Michael R.
    Miller's "Ascendant" are different books whose runtimes agree to 0.2%. One
    extra request buys certainty.
    """
    url = '%s/library/sections/%s/all?%s' % (
        base, ARGS.section, urllib.parse.urlencode(
            {'type': '9', 'X-Plex-Token': token}))
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        root = ET.fromstring(r.read())
    return {d.get('ratingKey'): ((d.get('guid') or '').replace(
                'com.plexapp.agents.incipit://', ''), d.get('titleSort') or '')
            for d in root}


def fetch_tracks(base, token):
    """
    Every TRACK in the section, with its file, size and runtime.

    One request for the whole library: asking for type=10 returns the Media and
    Part children inline, so the per-album fan-out that made the poison sweep
    slow is not needed here.
    """
    url = '%s/library/sections/%s/all?%s' % (
        base, ARGS.section, urllib.parse.urlencode(
            {'type': '10', 'X-Plex-Token': token}))
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return ET.fromstring(r.read())


def norm_title(s):
    """Comparison key for a book title: case, punctuation and edition noise folded."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r'\b(un)?abridged\b', '', s)
    return re.sub(r'[^a-z0-9]+', '', s)


def ext_of(path):
    m = AUDIO_EXT_RE.search(path or '')
    return m.group(1).lower() if m else '?'


def hours(ms):
    return (ms or 0) / 3600000.0


def foreign_reasons(name):
    """Why `name` doesn't look like an English series name (possibly empty)."""
    out = []
    odd = sorted({c for c in name if ord(c) > 127 and unicodedata.category(c).startswith('L')})
    if odd:
        out.append('non-ASCII %s' % ''.join(odd))
    toks = {t for t in re.split(r"[^0-9A-Za-zÀ-ɏ']+", name.lower()) if t}
    hit = sorted((toks & set(FOREIGN_WORDS)) - AMBIGUOUS)
    if hit:
        out.append('foreign words: %s' % ','.join(hit))
    return out


def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--url', default='http://127.0.0.1:32400', help='Plex base URL')
    ap.add_argument('--token', default=os.environ.get('PLEX_TOKEN', ''))
    ap.add_argument('--section', default='33', help='library section key')
    ap.add_argument('--prefs', default='/mnt/user/appdata/plex/Preferences.xml')
    ARGS = ap.parse_args()

    token = ARGS.token or token_from_preferences(ARGS.prefs)
    if not token:
        print('need --token, PLEX_TOKEN, or a readable %s' % ARGS.prefs, file=sys.stderr)
        return 2

    base = ARGS.url.rstrip('/')
    meta = fetch_albums(base, token)
    root = fetch_tracks(base, token)

    # album ratingKey -> {artist, title, parts[(file,size,duration)]}
    albums = {}
    for tr in root.iter('Track'):
        key = tr.get('parentRatingKey')
        if not key:
            continue
        a = albums.setdefault(key, {
            'artist': tr.get('grandparentTitle') or '?',
            'title': tr.get('parentTitle') or tr.get('title') or '?',
            'parts': []})
        for part in tr.iter('Part'):
            a['parts'].append((part.get('file') or '',
                               int(part.get('size') or 0),
                               int(part.get('duration') or 0)))
    for a in albums.values():
        a['size'] = sum(p[1] for p in a['parts'])
        a['dur'] = sum(p[2] for p in a['parts'])
        a['exts'] = sorted({ext_of(p[0]) for p in a['parts'] if p[0]})
        a['path'] = a['parts'][0][0] if a['parts'] else ''
    print('albums: %d   tracks: %d\n' % (len(albums), sum(len(a['parts']) for a in albums.values())))

    def line(rk, a, indent='   ', guid=False):
        rel = re.sub(r'^.*?/audiobooks[^/]*/', '', a['path'])
        print('%srk=%-7s %-20s %5.1f h  %7.1f MB  %-9s %s'
              % (indent, rk, a['artist'][:20], hours(a['dur']), a['size'] / 1e6,
                 '+'.join(a['exts']), rel[:78]))
        if guid:
            print('%s%s guid: %s' % (indent, ' ' * 8, meta.get(rk, ('?', ''))[0]))

    # ---- 1. duplicate titles, ACROSS artists ------------------------------
    print('=' * 100)
    print('1. DUPLICATE TITLES  (same title, any artist)')
    print('=' * 100)
    by_title = collections.defaultdict(list)
    for rk, a in albums.items():
        by_title[norm_title(a['title'])].append((rk, a))
    dupes = {k: v for k, v in by_title.items() if len(v) > 1}
    for key in sorted(dupes, key=lambda k: dupes[k][0][1]['title'].lower()):
        group = sorted(dupes[key], key=lambda x: -x[1]['dur'])
        print('\n  "%s"  x%d' % (group[0][1]['title'][:60], len(group)))
        for rk, a in group:
            line(rk, a, guid=True)
        # The guid decides when we have it: same guid == same edition, and
        # DIFFERENT guids mean two real books however close their runtimes are.
        guids = {meta.get(rk, ('?', ''))[0] for rk, _ in group}
        sizes = {a['size'] for _, a in group}
        if len(guids) == 1 and '?' not in guids:
            print('      -> SAME guid: definitively one edition held twice')
        elif len(sizes) == 1 and 0 not in sizes:
            # A differing guid says only that PLEX matched the two rows to
            # different records -- which it readily does when one copy sits in a
            # "[Dramatized Adaptation]" folder and the other does not. Byte-
            # identical size is about the file itself, and two genuinely
            # different books never agree to the byte. Measured on Oathbringer:
            # two guids (B0718Z5K4C, B01N7ZEWLO), one 807,500,000-byte file.
            print('      -> IDENTICAL byte size: one FILE in two places, however '
                  'Plex matched them')
        elif len(guids) == len(group):
            print('      -> DIFFERENT guids and sizes: separate books that share a '
                  'title, not duplicates')
        else:
            durs = [a['dur'] for _, a in group if a['dur']]
            if len(durs) > 1 and max(durs):
                spread = (max(durs) - min(durs)) / float(max(durs))
                verdict = ('runtimes agree (%.1f%%): likely the SAME recording' % (spread * 100)
                           if spread <= SAME_RUNTIME_TOLERANCE
                           else 'runtimes differ by %.0f%%: probably DIFFERENT books' % (spread * 100))
                print('      -> guids inconclusive; %s' % verdict)
    print('\n  duplicate title groups: %d' % len(dupes))

    # ---- 2. mixed formats -------------------------------------------------
    print('\n' + '=' * 100)
    print('2. MIXED AUDIO FORMATS  (one book held as more than one format)')
    print('=' * 100)
    n = 0
    for rk, a in sorted(albums.items(), key=lambda x: (x[1]['artist'], x[1]['title'])):
        if len(a['exts']) > 1:
            n += 1
            print('\n  WITHIN one album: %s -- %s' % (a['artist'][:24], a['title'][:44]))
            line(rk, a)
    for key, group in sorted(dupes.items()):
        exts = {e for _, a in group for e in a['exts']}
        if len(exts) > 1:
            n += 1
            print('\n  ACROSS copies: "%s"  (%s)' % (group[0][1]['title'][:52], ' vs '.join(sorted(exts))))
            for rk, a in sorted(group, key=lambda x: -x[1]['size']):
                line(rk, a)
    print('\n  mixed-format cases: %d' % n)

    # ---- 3. junk series folders ------------------------------------------
    print('\n' + '=' * 100)
    print('3. SERIES FOLDERS that are not English series names')
    print('=' * 100)
    folders = collections.defaultdict(list)
    for rk, a in albums.items():
        parts = [p for p in a['path'].split('/') if p]
        if len(parts) >= 4:
            folders[(parts[-4], parts[-3])].append((rk, a))
    flagged = 0
    for (artist, folder), group in sorted(folders.items()):
        why = foreign_reasons(folder)
        if BLURB_RE.search(folder):
            why.append('blurb wording')
        if not why:
            continue
        flagged += 1
        print('\n  %s/%s/   [%s]' % (artist[:24], folder[:46], '; '.join(why)))
        for rk, a in sorted(group, key=lambda x: x[1]['title']):
            line(rk, a, indent='      ')
    print('\n  flagged folders: %d of %d' % (flagged, len(folders)))
    print('\nNothing was changed by this script.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
