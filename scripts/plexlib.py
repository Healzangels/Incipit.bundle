#!/usr/bin/env python3
"""
Shared plumbing for the read-only Plex report scripts in this directory.

WHY THIS EXISTS
    poison_sweep.py and library_audit.py answer different questions but reach
    Plex the same way, and each grew its own copy of the connection code. They
    had already drifted: the same family of tool defaulted to
    http://10.0.1.99:32400 in one script and http://127.0.0.1:32400 in the
    other, so "the default" depended on which report you happened to run. The
    token reader was duplicated verbatim, which is the shape where a fix lands
    in one copy and not the other.

    Nothing here is Plex-agent code. These scripts are operator tools that live
    beside the bundle for convenience; Plex only ever loads Contents/Code, so
    this module is inert as far as the plugin is concerned.
"""

import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# The scripts run ON the Plex box -- that is the only host with both
# Preferences.xml (for the token) and the media mount -- so loopback is the
# honest default. --url overrides for a remote probe.
DEFAULT_URL = 'http://127.0.0.1:32400'
DEFAULT_PREFS = '/mnt/user/appdata/plex/Preferences.xml'
DEFAULT_SECTION = '33'
DEFAULT_TIMEOUT = 60


def add_common_args(ap):
    """Register the connection flags every report shares."""
    ap.add_argument('--url', default=DEFAULT_URL, help='Plex base URL')
    ap.add_argument('--token', default=os.environ.get('PLEX_TOKEN', ''),
                    help='Plex token (default: read from --prefs)')
    ap.add_argument('--section', default=DEFAULT_SECTION,
                    help='library section key (default: the audiobook library)')
    ap.add_argument('--prefs', default=DEFAULT_PREFS,
                    help='Plex Preferences.xml, read for the token when none is given')


def token_from_preferences(path):
    """
    The server's own Plex token, read from Preferences.xml, or ''.

    Saves pasting a credential onto a command line, where it lands in shell
    history and scrollback. The file is root/plex-readable only, which is
    exactly who runs these on the Plex box. Returns '' when it is not readable
    -- e.g. running from a workstation -- so --token and PLEX_TOKEN still work.
    """
    try:
        with open(path) as fh:
            import re
            m = re.search(r'PlexOnlineToken="([^"]+)"', fh.read())
        return m.group(1) if m else ''
    except Exception:
        return ''


def resolve_token(args):
    """The token to use, or None after printing why there isn't one."""
    token = args.token or token_from_preferences(args.prefs)
    if not token:
        print('need --token, PLEX_TOKEN, or a readable %s' % args.prefs, file=sys.stderr)
        return None
    return token


def get_bytes(url, timeout=DEFAULT_TIMEOUT, quiet=False):
    """Raw bytes for a Plex URL, or None. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        if not quiet:
            print('    ! fetch failed: %s' % e, file=sys.stderr)
        return None


def thumb_bytes(base, rk, token, timeout=DEFAULT_TIMEOUT):
    """An item's CURRENT poster bytes, or None when it has none."""
    return get_bytes('%s/library/metadata/%s/thumb?X-Plex-Token=%s' % (base, rk, token),
                     timeout=timeout)


def api_text(base, path, token, timeout=DEFAULT_TIMEOUT, **params):
    """XML text for a Plex API path ('' on failure)."""
    params['X-Plex-Token'] = token
    url = '%s%s?%s' % (base, path, urllib.parse.urlencode(params))
    data = get_bytes(url, timeout=timeout)
    return data.decode('utf-8', 'replace') if data else ''


def api_xml(base, path, token, timeout=DEFAULT_TIMEOUT, **params):
    """Parsed XML root for a Plex API path, or None."""
    params['X-Plex-Token'] = token
    url = '%s%s?%s' % (base, path, urllib.parse.urlencode(params))
    data = get_bytes(url, timeout=timeout)
    return ET.fromstring(data) if data else None
