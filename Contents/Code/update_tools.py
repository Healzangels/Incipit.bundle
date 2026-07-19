# Import internal tools
from logging import Logging
from region_tools import RegionTool
import re
import struct
import urllib
import os

# Setup logger
log = Logging()

# A subtitle that is really just a series/format descriptor rather than a genuine
# subtitle. Providers (esp. Hardcover) stash the series in the subtitle field, so
# appending it clutters the display title ("Night Watch: Discworld Novel 26") and
# can even contradict the series position used for sorting. Matches "Book 29",
# "Discworld Novel 26", "(Xanth #3)", or "...Novel" at the end ("A Discworld
# Novel"); a real subtitle like "A Novel of the Cosmere" ends in a word, not
# "Novel", so it is kept. (No leading-underscore name — the Plex sandbox forbids
# it.)
SERIES_SUBTITLE_RE = re.compile(
    r'\b(?:book|novel|volume|vol|part|episode)\s+\d+\b|#\s*\d+|\bnovel\s*$',
    re.IGNORECASE
)

# A trailing series descriptor a provider baked into the TITLE itself, e.g.
# "Changes: The Dresden Files, Book 12" -> "Changes". Each pattern requires an
# explicit number marker ("Book N" / "#N"), so a plain subtitle ("Changes: A
# Novel", "The Law: A Dresden Files Novel") has no number and is never stripped
# -- only an unambiguous series+number tail. Mirrors the incipit-api
# SERIES_SUFFIX so the DISPLAY title matches how the book was matched.
SERIES_TITLE_SUFFIX_RE = [
    re.compile(r'\s*[:\-–—]\s*[^:]*?\bbook\s+\d+\s*$', re.IGNORECASE),
    re.compile(r'\s*\([^)]*#\s*\d+\s*\)\s*$'),
    re.compile(r'\s*,\s*book\s+\d+\s*$', re.IGNORECASE),
]


def strip_trailing_series(title):
    """
        Strip a trailing "<Series>, Book N" / "(<Series> #N)" that a provider
        baked into the book title, so display + sort titles show just the book
        name. Number-marked only, so real subtitles are safe. Never empties the
        title (guards a pathological all-series title).
    """
    if not title:
        return title
    stripped = title
    for pat in SERIES_TITLE_SUFFIX_RE:
        stripped = pat.sub('', stripped)
    stripped = stripped.strip()
    return stripped or title


class UpdateTool:
    def __init__(self, content_type, force, lang, media, metadata, prefs):
        self.content_type = content_type
        self.force = force
        self.lang = lang
        self.media = media
        self.metadata = metadata
        self.prefs = prefs
        self.region = self.extract_region_from_id()

    def build_url(self):
        """
            Builds the URL for the API request.
        """
        # Get the current region
        self.region_override = self.get_preferred_region()
        # Set the region helper
        region_helper = RegionTool(
            region=self.region_override, content_type=self.content_type, id=self.extract_asin_from_id())

        update_url = region_helper.get_id_url()
        log.debug('Update URL: ' + update_url)
        return update_url

    def cleanup_html(self):
        """
            Cleans up HTML in either the description or synopsis.
        """
        html_tags = '<[^<]+?>'

        # Clean up HTML in the description
        if self.content_type == 'authors':
            # First handle special cases
            self.description = self.replace_html_special(self.description)
            self.description = re.sub(
                html_tags, '', self.description)
        # Clean up HTML in the synopsis
        if self.content_type == 'books':
            # First handle special cases
            self.synopsis = self.replace_html_special(self.synopsis)
            self.synopsis = re.sub(
                html_tags, '', self.synopsis)

    def replace_html_special(self, input_html):
        """
            Replaces HTML lists with a bullet point.
            Replaces HTML paragraphs with a newline.
            Replaces HTML line breaks with a newline.
        """
        return (
            input_html.replace("<ul>", "")
            .replace("</ul>", "\n")
            .replace("<ol>", "")
            .replace("</ol>", "\n")
            .replace("<li>", " • ")
            .replace("</li>", "\n")
            .replace("<br />", "")
            .replace("<p>", "")
            .replace("</p>", "\n")
            .strip()
        )

    def collect_metadata_to_log(self):
        """
            Collects the metadata to log.
        """
        # Start an array with common metadata
        data_to_log = [{'ASIN': self.metadata.id}]

        # Determine which metadata to log
        if self.content_type == 'books':
            data_to_log.extend(
                [
                    {'Book poster URL': self.thumb},
                    {'Book publisher': self.metadata.studio},
                    {'Book release date': str(
                        self.metadata.originally_available_at)},
                    {'Book sort title': self.metadata.title_sort},
                    {'Book summary': self.metadata.summary},
                    {'Book title': self.metadata.title},
                ]
            )
        elif self.content_type == 'authors':
            data_to_log.extend(
                [
                    {'Author bio': self.metadata.summary},
                    {'Author name': self.metadata.title},
                    {'Author poster URL': self.thumb},
                    {'Author sort name': self.metadata.title_sort},
                    {'Similar Authors': ', '.join([author['name']
                                                   for author in self.similar] if self.similar else [])},
                ]
            )

        return data_to_log

    def collect_metadata_arrs_to_log(self):
        """
            Collects the metadata arrays to log.
        """
        # Start an array with common metadata
        multi_arr = [{'Genres': self.metadata.genres}]

        # Book metadata
        book_multi_arr = [
            {'Moods (Authors)': self.metadata.moods},
            {'Styles (Narrators)': self.metadata.styles},
        ]

        # Determine which metadata to log
        if self.content_type == 'book':
            multi_arr.extend(book_multi_arr)

        return multi_arr

    def extract_asin_from_id(self):
        """
            Extracts the ASIN from the ID.
        """
        # Get the ASIN from the ID
        asin = self.metadata.id.split('_')[0]
        log.debug('Extracted ASIN from ID: ' + asin)
        return asin

    def extract_region_from_id(self):
        """
            Extracts the region from the ASIN and sets the region.
        """
        # Get the region from the ASIN
        try:
            region = self.metadata.id.split('_')[1]
            log.debug('Extracted region from ASIN: ' + region)
        except IndexError:
            log.info('No region found in ID, using default region.')
            region = self.get_preferred_region()
            # Save the region to the ID
            self.metadata.id = self.metadata.id + '_' + region
        # Set region and ASIN
        return region

    def get_preferred_region(self):
        """
            Get the preferred region from class or preferences.
        """
        try:
            region = self.region
        except AttributeError:
            region = self.prefs['region']
        log.info('Preferred region: ' + region)
        return region

    def log_update_metadata(self):
        """
            Writes metadata information to log.
        """
        # Send a separator to the log
        log.separator(
            msg=(
                'FINALIZED: ' + self.metadata.title +
                ', ID: ' + self.metadata.id
            ),
            log_level="info"
        )

        # Collect metadata to log
        data_to_log = self.collect_metadata_to_log()
        log.metadata(data_to_log, log_level="info")

        # Collect metadata arrays to log
        multi_arr = self.collect_metadata_arrs_to_log()
        log.metadata_arrs(multi_arr, log_level="info")

        log.separator(log_level="info")


class AlbumUpdateTool(UpdateTool):
    def parse_api_response(self, response):
        """
            Parses keys from API into helper variables if they exist.
        """
        self.set_empty_variables()

        if 'authors' in response:
            self.author = response['authors']
        if 'releaseDate' in response:
            self.date = response['releaseDate']
        if 'genres' in response:
            self.genres = response['genres']
        if 'narrators' in response:
            self.narrator = response['narrators']
        if 'rating' in response:
            self.rating = response['rating']
        if 'seriesPrimary' in response:
            self.series = response['seriesPrimary']['name']
            if 'position' in response['seriesPrimary']:
                self.volume = self.volume_prefix(
                    response['seriesPrimary']['position']
                )
        if 'seriesSecondary' in response:
            self.series2 = response['seriesSecondary']['name']
            if 'position' in response['seriesSecondary']:
                self.volume2 = self.volume_prefix(
                    response['seriesSecondary']['position']
                )
        if 'publisherName' in response:
            self.studio = response['publisherName']
        if 'summary' in response:
            self.synopsis = response['summary']
        if 'image' in response:
            self.thumb = response['image']
        if 'imageSquare' in response and response['imageSquare']:
            # Plex music art is square. Prefer the native square cover (Apple)
            # as the default poster, keeping the original as a secondary option
            # rather than dropping it.
            self.thumb_secondary = self.thumb
            self.thumb = response['imageSquare']
        if 'similar' in response:
            self.similar = response['similar']
        if 'subtitle' in response:
            self.subtitle = response['subtitle']
        if 'title' in response:
            # Strip a series descriptor a provider baked into the title
            # ("Changes: The Dresden Files, Book 12" -> "Changes") so both the
            # display and sort titles show just the book name.
            self.title = strip_trailing_series(response['title'])

    def set_metadata_date(self):
        """
            Sets the date.
        """
        if self.date is not None:
            if not self.metadata.originally_available_at or self.force:
                self.metadata.originally_available_at = self.date

    def set_empty_variables(self):
        """
            Sets empty variables.

            Every field parse_api_response fills conditionally must be reset
            here, because the metadata setters read them unconditionally. Audible
            records always carry a summary/publisher/author, but multi-provider
            (Hardcover/OpenLibrary) records often don't — a missing one used to
            crash the update with AttributeError (e.g. no 'synopsis').
        """
        # List-typed fields default to [] (not None) so the tag helpers can
        # iterate them safely even when a provider record omits them.
        self.author = []
        self.date = None
        self.genres = []
        self.narrator = []
        self.rating = None
        self.series = ''
        self.series2 = ''
        self.similar = []
        self.studio = ''
        self.subtitle = ''
        self.synopsis = ''
        self.thumb = ''
        # The original (usually portrait) cover, kept as a secondary poster when
        # a native square cover is used as the default.
        self.thumb_secondary = ''
        self.title = ''
        self.volume = ''
        self.volume2 = ''

    def set_metadata_rating(self):
        """
            Sets the rating.
        """
        # We always want to refresh the rating. Providers rate on a 0-5 scale,
        # doubled to Plex's 0-10; clamp to [0, 10] so a stray already-0-10 value
        # from a provider can't produce an out-of-range rating.
        if self.rating:
            self.metadata.rating = max(0.0, min(float(self.rating) * 2, 10.0))

    def set_metadata_summary(self):
        """
            Sets the summary.
        """
        if not self.metadata.summary or self.force:
            self.cleanup_html()
            self.metadata.summary = self.synopsis

    def set_metadata_studio(self):
        """
            Sets the studio.
        """
        if not self.metadata.studio or self.force:
            self.metadata.studio = self.studio

    def set_metadata_sort_title(self):
        """
            Sets the sort title.
        """
        # Add series/volume to sort title where possible.
        series_with_volume = ''
        if self.series and self.volume:
            series_with_volume = self.series + ', ' + self.volume
        # Only include subtitle in sort if not in a series
        if not self.volume:
            self.title = self.metadata.title
        if not self.metadata.title_sort or self.force:
            self.metadata.title_sort = ' - '.join(
                filter(
                    None, [(series_with_volume), self.title]
                )
            )

    def set_metadata_tags(self):
        """
            Set tags of artist
        """
        # Create tagger.
        tagger = TagTool(self, self.prefs)

        # Clears moods if force (refresh) is true.
        if self.force:
            tagger.clear_moods()
            tagger.clear_styles()

        # Genres.
        tagger.add_genres()
        # Narrators.
        tagger.add_narrators_to_styles()
        # Authors.
        if self.prefs['store_author_as_mood']:
            tagger.add_authors_to_moods()
        # Series.
        tagger.add_series_to_moods()
        # Similar.
        tagger.add_similar()

    def set_metadata_title(self):
        """
            Sets the title.
        """
        # If the `simplify_title` option is selected, don't append subtitle
        # and remove extra endings on the title
        if self.prefs['simplify_title']:
            album_title = self.simplify_title()
        elif self.subtitle and not SERIES_SUBTITLE_RE.search(self.subtitle):
            # Append a genuine subtitle, but not a series descriptor like
            # "Discworld Novel 26" / "A Discworld Novel" / "(Xanth #3)".
            album_title = self.title + ': ' + self.subtitle
        else:
            album_title = self.title
        album_title = self.strip_trailing_by_contributor(album_title)
        if not self.metadata.title or self.force:
            self.metadata.title = album_title

    def strip_trailing_by_contributor(self, title):
        """
            Remove a trailing " by <Name>" from a title, but ONLY when <Name>
            matches a credited author or narrator of this book. Audible sometimes
            appends the ghostwriter ("Code Red: A Mitch Rapp Novel by Kyle Mills").
            Matching against real contributors keeps legit titles like "Death by
            Black Hole" or "Murder by the Book" intact.
        """
        matched = re.search(r'\s+by\s+(.+?)\s*$', title, flags=re.IGNORECASE)
        if not matched:
            return title
        trailing = matched.group(1).strip().lower()
        names = []
        for person in (self.author or []):
            name = person.get('name') if isinstance(person, dict) else None
            if name:
                names.append(name.strip().lower())
        for person in (self.narrator or []):
            name = person.get('name') if isinstance(person, dict) else None
            if name:
                names.append(name.strip().lower())
        if trailing in names:
            return title[:matched.start()].strip()
        return title

    def simplify_title(self):
        """
            Remove extra description text from the title
        """
        # If the title ends with a series part, remove it
        # works for "Book 1" and "Book One"
        album_title = re.sub(
            r", book [\w\s-]+\s*$", "", self.title, flags=re.IGNORECASE)
        # If the title ends with "unabridged"/"abridged", with or without parenthesis
        # remove them; case insensitive
        album_title = re.sub(r" *\(?(un)?abridged\)?$", "",
                             album_title, flags=re.IGNORECASE)
        # Trim any leading/trailing spaces just in case
        album_title = album_title.strip()

        return album_title

    def volume_prefix(self, string):
        """
            Prefixes volume number with 'Book' if it doesn't exist.
        """
        # incipit-api (Hardcover/OpenLibrary) can return a numeric series
        # position; coerce to str so the regex/concatenation don't raise. A
        # missing/None position yields no volume rather than "Book None".
        if string is None:
            return ''
        string = str(string)
        if not string.strip():
            return ''
        book_regex = '(Book ?(\d*\.)?\d+[+-]?[\d]?)'
        if not re.match(book_regex, string):
            prefixed_string = ('Book ' + string)
            return prefixed_string
        return string


class ArtistUpdateTool(UpdateTool):
    def measure_image(self, image_url):
        """
            Read a JPEG's (height, width) from the first bytes, or None on any
            failure (non-JPEG, unreachable, truncated).
        """
        try:
            image_file_dl = urllib.urlopen(image_url)
            image_file_dl_contents = image_file_dl.read()
            image_file_dl.close()

            image_file = os.tmpfile()
            image_file.write(image_file_dl_contents)
            image_file.seek(0)

            head = image_file.read(24)
            if len(head) != 24:
                image_file.close()
                return None

            image_file.seek(0)  # Read 0xff next
            size = 2
            ftype = 0
            while not 0xc0 <= ftype <= 0xcf:
                image_file.seek(size, 1)
                byte = image_file.read(1)
                while ord(byte) == 0xff:
                    byte = image_file.read(1)
                ftype = ord(byte)
                size = struct.unpack('>H', image_file.read(2))[0] - 2
            # We are at a SOFn block
            image_file.seek(1, 1)  # Skip `precision' byte.
            height, width = struct.unpack('>HH', image_file.read(4))
            image_file.close()
            return (height, width)
        except Exception as err:
            log.error('Could not measure image %s: %s' % (image_url, err))
            return None

    def get_square_image(self, image_url):
        """
            Crop an Audible (Amazon) author photo to a square via Amazon's
            crop-in-URL syntax: portrait centered at the top, landscape at the
            horizontal center. Non-Amazon URLs (e.g. Hardcover) don't support
            that syntax, so they are returned unchanged.
        """
        if 'amazon' not in image_url:
            return image_url

        dimensions = self.measure_image(image_url)
        if not dimensions:
            return image_url
        height, width = dimensions

        if (height > width):
            # Return a portrait image centered at the top
            w_str = str(width)
            return image_url.replace(
                '.jpg',
                '.__01_SX'+w_str+'_CR0,0,'+w_str+','+w_str+'__.jpg'
            )

        if (width > height):
            # Return a landscape image centered at the horizontal middle
            h_str = str(height)
            padding = str((width - height) / 2)
            return image_url.replace(
                '.jpg',
                '.__01_SY'+h_str+'_CR'+padding+',0,'+h_str+','+h_str+'__.jpg'
            )

        return image_url

    def parse_api_response(self, response):
        """
            Parses keys from API into helper variables if they exist.
        """
        self.set_empty_variables()

        if 'description' in response:
            self.description = response['description']
        if 'genres' in response:
            self.genres = response['genres']
        if 'name' in response:
            self.name = response['name']
        if 'image' in response:
            # The API already picks the best author image (preferring Hardcover's
            # curated portrait over Audible's — which is often the book cover, not
            # a photo), so use it as the default poster.
            squared_image = self.get_square_image(response['image'])
            log.debug('Author image: ' + squared_image)
            self.thumb = squared_image
        if 'imageAlt' in response and response['imageAlt']:
            # Offer the alternate (Audible) image as a secondary poster option.
            self.thumb_secondary = response['imageAlt']
        if 'similar' in response:
            self.similar = response['similar']

    def set_metadata_description(self):
        """
            Set description of artist
        """
        if not self.metadata.summary or self.force:
            self.cleanup_html()
            self.metadata.summary = self.description

    def set_empty_variables(self):
        """
            Sets empty variables. name/description are read unconditionally by
            the metadata setters, and description is often absent (author with no
            bio), so both must be initialised or the update raises AttributeError.
        """
        self.date = None
        self.description = ''
        self.genres = []
        self.name = ''
        self.similar = []
        self.thumb = ''
        # The alternate (Audible) author image, offered as a secondary poster.
        self.thumb_secondary = ''

    def set_metadata_sort_title(self):
        """
            Set sort title of artist
        """
        if not self.metadata.title_sort or self.force:
            single_word_name = re.match(r'\A[\w-]+\Z', self.name)
            if self.prefs['sort_author_by_last_name'] and not single_word_name:
                split_author_surname = re.match(
                    '^(.+?).([^\s,]+)(,?.(?:[JS]r\.?|III?|IV))?$',
                    self.name,
                )
                if split_author_surname:
                    self.metadata.title_sort = ', '.join(
                        filter(
                            None,
                            [
                                (split_author_surname.group(2) + ', ' +
                                    split_author_surname.group(1)),
                                split_author_surname.group(3)
                            ]
                        )
                    )
                else:
                    # The name didn't fit "First Last [suffix]" (e.g. empty or
                    # an unusual form). Fall back to the plain name/title rather
                    # than crash on .group() of a None match.
                    self.metadata.title_sort = self.name or self.metadata.title
            else:
                self.metadata.title_sort = self.metadata.title

    def set_metadata_tags(self):
        """
            Set tags of artist
        """
        # Create tagger.
        tagger = TagTool(self, self.prefs)
        # Genres.
        tagger.add_genres()
        # Similar.
        tagger.add_similar()

    def set_metadata_title(self):
        """
            Set title of artist
        """
        if not self.metadata.title or self.force:
            self.metadata.title = self.name


class TagTool:
    def __init__(self, helper, Prefs):
        self.helper = helper
        self.prefs = Prefs

    def add_genres(self):
        """
            Add genre(s) to Plex where available and depending on preference.
        """
        if not self.prefs['keep_existing_genres'] and self.helper.genres:
            if not self.helper.metadata.genres or self.helper.force:
                self.helper.metadata.genres.clear()
                for genre in self.helper.genres:
                    if genre['name']:
                        self.helper.metadata.genres.add(genre['name'])

    def add_narrators_to_styles(self):
        """
            Adds narrators to styles.
        """
        if not self.helper.metadata.styles or self.helper.force:
            self.helper.metadata.styles.clear()
            for narrator in (self.helper.narrator or []):
                self.helper.metadata.styles.add(narrator['name'].strip())

    def add_authors_to_moods(self):
        """
            Adds authors to moods, except for cases in contibutors list.
        """
        contributor_regex = '.+?(?= -)'
        if not self.helper.metadata.moods or self.helper.force:
            # Loop through authors to check if it has contributor wording
            for author in (self.helper.author or []):
                if not re.match(contributor_regex, author['name']):
                    self.helper.metadata.moods.add(author['name'].strip())

    def add_series_to_moods(self):
        """
            Adds book series' to moods, since collections are not supported
        """
        if self.helper.series:
            self.helper.metadata.moods.add("Series: " + self.helper.series)
        if self.helper.series2:
            self.helper.metadata.moods.add("Series: " + self.helper.series2)

    def add_similar(self):
        """
            Adds similar items.
        """
        if self.helper.similar:
            for item in self.helper.similar:
                self.helper.metadata.similar.add(item['name'])

    def clear_moods(self):
        """
            Clears moods.
        """
        self.helper.metadata.moods.clear()

    def clear_styles(self):
        """
            Clears styles.
        """
        self.helper.metadata.styles.clear()
