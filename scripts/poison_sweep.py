#!/usr/bin/env python3
"""
Find albums whose poster is actually the ARTIST's photo ("poster poison").

WHY THIS EXISTS
    A book with no cover of its own inherits its artist's art in Plex, and a
    metadata pass that mirrors "whatever is currently selected" can then bake
    that author photo in as the book's cover. The agent guards against this, but
    the guard compared EXACT bytes -- and upload_and_select_poster deliberately
    re-POSTs image+RESELECT_PAD to force a re-selection, so any poisoned album
    that had been through one re-select cycle wore a 20-byte pad and was
    invisible to it. Found live on Kyle Mills / "Fade" (bundle v1.3.103 closed
    the detection hole). This sweep answers the follow-up question: what ELSE
    slipped through while the guard was blind to the padded form?

READ-ONLY BY DESIGN
    It reports and never writes. Covers are hand-curated, and a bulk poster
    "fix" has destroyed curated art here before -- so the output is a worklist
    for a human, not an action. There is deliberately no --fix flag.

USAGE
    python3 poison_sweep.py --url http://10.0.1.99:32400 --token XXXX --section 33
    (--token also read from PLEX_TOKEN; --section defaults to the artist library)
"""

import argparse
import hashlib
import os
import re
import sys
import urllib.parse
import urllib.request

# Must match RESELECT_PAD in Contents/Code/__init__.py -- the suffix the agent
# appends to re-POST pixel-identical bytes as "new content" so Plex re-selects.
RESELECT_PAD = b'\nincipit-reselect-v1'

TIMEOUT = 30


def fetch(url):
    """Raw bytes for a Plex URL, or None."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.read()
    except Exception as e:
        print('    ! fetch failed: %s' % e, file=sys.stderr)
        return None


def api(base, path, token, **params):
    """XML text for a Plex API path."""
    params['X-Plex-Token'] = token
    url = '%s%s?%s' % (base, path, urllib.parse.urlencode(params))
    data = fetch(url)
    return data.decode('utf-8', 'replace') if data else ''


def directories(xml):
    """(ratingKey, title) for each <Directory> in a listing."""
    out = []
    for tag in re.findall(r'<Directory\b[^>]*>', xml):
        rk = re.search(r'ratingKey="(\d+)"', tag)
        title = re.search(r'title="([^"]*)"', tag)
        if rk:
            out.append((rk.group(1), (title.group(1) if title else '?')))
    return out


def thumb_bytes(base, rk, token):
    """The item's CURRENT poster bytes, or None when it has none."""
    return fetch('%s/library/metadata/%s/thumb?X-Plex-Token=%s' % (base, rk, token))


def classify(album, artist):
    """
    How the album's poster relates to the artist's, or None when unrelated.

    Both forms matter: the plain copy is the original poisoning, and the padded
    copy is the same image after one agent re-select cycle -- the form the old
    exact-byte guard could not see.
    """
    if not album or not artist:
        return None
    if album == artist:
        return 'PLAIN'
    if album == artist + RESELECT_PAD:
        return 'PADDED'
    return None


def main():
    ap = argparse.ArgumentParser(description='Report albums whose poster is the artist photo.')
    ap.add_argument('--url', default='http://10.0.1.99:32400', help='Plex base URL')
    ap.add_argument('--token', default=os.environ.get('PLEX_TOKEN', ''), help='Plex token')
    ap.add_argument('--section', default='33', help='library section key (artist library)')
    ap.add_argument('--limit', type=int, default=0, help='stop after N artists (for a quick probe)')
    args = ap.parse_args()

    if not args.token:
        print('need --token or PLEX_TOKEN', file=sys.stderr)
        return 2

    base = args.url.rstrip('/')
    artists = directories(api(base, '/library/sections/%s/all' % args.section, args.token, type='8'))
    if args.limit:
        artists = artists[: args.limit]
    print('scanning %d artists in section %s\n' % (len(artists), args.section))

    poisoned = []
    scanned = 0
    no_artist_art = 0

    for i, (artist_rk, artist_name) in enumerate(artists, 1):
        art = thumb_bytes(base, artist_rk, args.token)
        if not art:
            # No artist photo means nothing to be poisoned BY.
            no_artist_art += 1
            continue
        albums = directories(
            api(base, '/library/metadata/%s/children' % artist_rk, args.token)
        )
        for album_rk, album_title in albums:
            scanned += 1
            kind = classify(thumb_bytes(base, album_rk, args.token), art)
            if kind:
                poisoned.append((artist_name, album_title, album_rk, kind))
                print('  POISONED [%s] %s -- %s (rk %s)' % (kind, artist_name, album_title, album_rk))
        if i % 25 == 0:
            print('  ... %d/%d artists, %d albums checked' % (i, len(artists), scanned))

    print('\n' + '=' * 72)
    print('albums checked      : %d' % scanned)
    print('artists with no art : %d' % no_artist_art)
    print('POISONED            : %d' % len(poisoned))
    if poisoned:
        plain = len([p for p in poisoned if p[3] == 'PLAIN'])
        padded = len([p for p in poisoned if p[3] == 'PADDED'])
        print('  plain: %d   padded: %d  (padded were invisible to the pre-1.3.103 guard)' % (plain, padded))
        print('\nworklist:')
        for artist_name, album_title, album_rk, kind in poisoned:
            print('  %-7s %-28s %-38s rk=%s' % (kind, artist_name[:28], album_title[:38], album_rk))
        print('\nTo repair: replace the book folder\'s cover.jpg with real art, then')
        print('Refresh Metadata on that album. Nothing is changed by this script.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
