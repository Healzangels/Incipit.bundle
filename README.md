<p align="center">
  <a href="" rel="noopener">
 <img width=200px height=200px src="../assets/logos/logo.png?raw=true" alt="Project logo"></a>
</p>

<h3 align="center">Incipit.bundle</h3>

<div align="center">

[![Status](https://img.shields.io/badge/status-active-success.svg)]()
[![GitHub Issues](https://img.shields.io/github/issues/Healzangels/Incipit.bundle.svg)](https://github.com/Healzangels/Incipit.bundle/issues)
[![GitHub Pull Requests](https://img.shields.io/github/issues-pr/Healzangels/Incipit.bundle.svg)](https://github.com/Healzangels/Incipit.bundle/pulls)
[![License](https://img.shields.io/badge/license-GNUGPL-blue.svg)](/LICENSE)

</div>

---

<p align="center"> Incipit is a Plex metadata agent for audiobooks that matches by content — title, author, and runtime — across multiple sources, so books that never had an Audible release still get matched. It is a fork of <a href="https://github.com/djdembeck/Audnexus.bundle">Audnexus.bundle</a> and talks to an <a href="https://github.com/Healzangels/incipit-api">incipit-api</a> instance (a fork of <a href="https://github.com/djdembeck/audnexus">audnexus</a>).
    <br> 
</p>

## 📝 Table of Contents

- [About](#about)
- [Getting Started](#getting_started)
- [Configuring](#config)
- [Usage](#usage)
- [Contributing](CONTRIBUTING.md)

## 🧐 About <a name = "about"></a>

Incipit matches audiobooks on their CONTENT — title, author and runtime — rather than
on an Audible catalogue id. That is the whole reason the fork exists: a book with no
Audible release, or one whose ASIN never made it into your tags, still gets matched.

**It is self-hosted, and that is not optional.** The agent talks to your own
[incipit-api](https://github.com/Healzangels/incipit-api) instance, set in
`api_base_url` below. There is no shared public aggregator behind it — unlike upstream
Audnexus, nothing here gets faster because other people use it, and no credentials
leave your network.

The API fans a search out across six providers and reconciles the answers — **Audible,
Hardcover, Chaptarr, Apple, OverDrive and OpenLibrary** — so the match, the series, the
genres and the cover can each come from whichever source actually has them.

### What Incipit adds over Audnexus

- **Runtime-corroborated matching** — the file's duration is evidence, so a wrong
  edition or a differently-narrated recording can be demoted or vetoed.
- **Series from a source of truth**, with franchise umbrellas rejected: a book shelves
  under *The Elric Saga*, not *The Eternal Champion Sequence*.
- **Genres merged** across community sources instead of Audible's alone.
- **Cover handling that will not fight you**: local `cover.jpg` support, alternate
  marketplace art offered as extra pickable posters, duplicate tiles suppressed by
  perceptual comparison, and — the rail that matters — a poster you picked by hand is
  never replaced or pruned.

Available regions:
- `[au]` - `.com.au`
- `[ca]` - `.ca`
- `[de]` - `.de`
- `[es]` - `.es`
- `[fr]` - `.fr`
- `[in]` - `.in`
- `[it]` - `.it`
- `[jp]` - `.co.jp`
- `[us]` - `.com`
- `[uk]` - `.co.uk`

***NOTE***: The agent was built for English-based regions. If you find an issue with your region, please open a new issue or PR.

## 🏁 Getting Started <a name = "getting_started"></a>

Getting the agent up and running is a very smooth process, whether this is your first foray into audiobooks or you are migrating a library from another audiobooks agent. We look forward to getting you high quality data!

### Prerequisites

- Plex Media Server `v1.24.4.5081` or greater.
- A running [incipit-api](https://github.com/Healzangels/incipit-api) instance, and its
  URL to put in `api_base_url`. The agent degrades to Audible-only matching without it —
  no Hardcover, no OpenLibrary, no duration matching.
- `git` installed on system, as this is the preferred method of installing/updating the agent. You can also extract the zip instead.
- Files are expected to be in/tested with common audiobook [file structure](https://support.plex.tv/articles/200265296-adding-music-media-from-folders/) and tags, specifically from either [Bragi Books](https://github.com/djdembeck/bragibooks) or [Seanap's guide](https://github.com/seanap/Plex-Audiobook-Guide). In particular, you are expected to have the following structure: `Author Name/Book Name/Book Name: Subtitle.m4b` with `album` and `albumartist` tags. This is imperative for proper matching!

### Installing

If you are new to getting plugins on your system or do not have access to `git`, go through this Plex documentation: [How do I manually install a plugin?
](https://support.plex.tv/articles/201187656-how-do-i-manually-install-a-plugin/) If you are already familiar with the plugins system, and have `git`, follow the below steps.

1. Clone (or unzip) this project into your Plex `Plug-ins` directory:

```
git clone https://github.com/Healzangels/Incipit.bundle.git
```

2. Restart your Plex Media Server.

**If Plex runs in a container**, the plugin files must be readable by the user Plex runs
as — usually `nobody:users`. A `git pull` as root leaves them owned by root and the
plugin simply will not load:

```
chown -R nobody:users .
```

**Plex does not hot-reload a bundle.** After updating the files, a resident plugin keeps
serving the OLD code from memory indefinitely. Either restart Plex, or reload just the
plugin — which takes seconds and does not interrupt playback:

```
curl -s "http://<plex-host>:32400/:/plugins/com.plexapp.agents.incipit/restart?X-Plex-Token=<token>"
```

For future updates, run the below command from within the `Incipit.bundle` folder.

```
git pull
```

## 🔧 Configuring the agent <a name = "config"></a>

If you wish to use local tags/images, you can follow the directions [here](https://github.com/seanap/Plex-Audiobook-Guide#configure-metadata-agent-in-plex), but this agent assumes you will not.

### Settings that matter

Set in Plex under the library's agent settings.

| setting | what it does |
|---|---|
| **`api_base_url`** | **Required.** Your incipit-api, e.g. `http://10.0.1.99:3737`. Blank falls back to Audible-only matching, losing Hardcover, OpenLibrary and duration matching. |
| `region` | Search region. The agent was built for English-based regions. |
| `prefer_local_cover` | Use the book folder's `cover.jpg` as the poster. |
| `cover_mirror_mode` | Whether the selected poster is written back to `cover.jpg` — `Off`, `Seed only` (write only where none exists), or `Curation` (the pick replaces it). |
| `online_perceptual_dedupe` | Hide a poster when another source already shows the same picture in different bytes. Uncheck to keep every variant. |
| `series_from_folder_wins` | Trust the folder tree for series and book number over the provider. Off by default: the provider is usually right, and the folder is a fallback. |
| `prefer_sidecar_metadata` | Trust a `metadata.json` sidecar next to the book. |
| `keep_existing_genres` | Leave genres alone instead of merging in the community ones. |
| `logging_level` | `WARN` by default. Cover decisions log at INFO, so raise it to INFO or DEBUG when diagnosing a poster, then put it back — a full sweep at DEBUG writes a lot. |

`series_from_folder_authors` and `authors_prefer_hardcover` take comma-separated
names, for the cases where one author needs the opposite of your default.

### Using quick match

There are currently 2 quick match/search override options:
- **ASIN**: Bypasses search and explicitly uses the ASIN Provided
- **Region** (ie `[uk]`): Searches the given region instead of your set region.

Quick match supports filename and manual search.

This works for both authors and books. By default, the ASIN is searched in your library's `region` (from agent settings).

You may override region on a per author/book basis using the region code in brackets, such as `[uk]` either before or after the other search terms.

Here are some quick match examples:

- Override region
```
[uk] NAME
```
- Override asin and region
```
[uk] B01234ABCD
```
- Override ASIN and Region from filename
```
Author Name/Book Name B01234ABCD [uk]/Book Name: Subtitle.m4b
```

***NOTE***: Authors cannot be quick matched from filenames.

### Create an audiobook library

- From within Plex Web, create a new library, with the MUSIC type, and name it Audiobooks.
- Add your folders.

In the ADVANCED tab:
- Scanner: `Plex Music Scanner`
- Agent: `Incipit Agent`
- Toggle agent settings as you please.
- Uncheck all boxes except `Store track progress`
- Genres: `Embedded tags`
- Album Art: `Local Files Only`

Add the library and go do anything but read a physical book while the magic happens :)

### Migrate an existing audiobook library

If you are coming from another Audiobooks agent, such as Audiobooks.bundle, then upgrading is super easy!

- First, follow the steps for the ADVANCED tab above and save the settings.
- Second, go to the Audiobooks library settings, `Manage Library > Refresh All Metadata`. This will programmatically upgrade authors, and then every album under those authors.

Just like adding a new library, upgrading one can take some time to switch all your data over.

## 🎈 Usage <a name="usage"></a>

### Manually fixing matches
There are a few tricks to know about using fix match for books and authors:
- You may use [Quick Match](#using-quick-match) if you already know the ASIN.
- Some authors have no Audible profile. Incipit still matches the books, and can take the author portrait from Hardcover instead (`authors_prefer_hardcover`).
- You may need to modify author names in search to find them (for example, removing a middle initial). This is a search limitation.
- Book results come back in the format of: `"TITLE" by AUTHOR_FIRSTINITIAL.AUTHOR_LASTNAME w/ NARRATOR_FIRSTINITIAL.NARRATOR_LASTNAME`
- Year field cannot be used by music agents (what we use), so it's an irrelevant parameter.
- Scoring starts from title and author similarity ([Levenshtein distance](https://en.wikipedia.org/wiki/Levenshtein_distance)), then weighs evidence upstream does not have:
  - **RUNTIME.** A candidate whose stated length disagrees with the file's real duration is demoted, and can be vetoed outright. This is what lets a wrong edition lose to a right one that scores lower on text alone.
  - Language mismatch against the library's language, bundle/omnibus listings, volume numbering and AI-narrated editions are each demoted.
  - An ASIN you supply via Quick Match pins the result rather than competing with it.
- Near-identical results are normal and usually REAL: separate editions of one recording. They are deliberately kept separate rather than merged, so you can pick the one you want. The highest score is generally right; where two differ only by cover art, either will match the same book.

### Data that the agent brings to your library:

#### Authors (Artists)
- High resolution image.
- Text description/bio.
- Genres
- Sorted by `Last Name, First Name`
- Combines books with multiple authors into the first author, reducing duplicate author entries/pages.
- Similar authors

#### Books (Albums)
- High resolution cover (up to 3200x3200).
- Rating (currently based on Audible user rating).
- Release date.
- Record label (publisher)
- Review (plot summary)
- Genres merged across sources rather than taken from Audible alone, then normalised to drop shelf noise and cross-source duplicates. Measured library-wide: ~7.8 genres per album, where Audible alone gives at most 6.
- Narrator as `Style` tag.
- Authors as `Mood` tag.
- Series as `Mood` tag (prefixed by `Series:`)
- Sorted by Series number and then book title.

**This agent cannot create collections for your series'. 
If you would like to set up automatic collections for book series', you can do so with the guide here: [Audnexus + Kometa: Audiobook Series Collections](https://github.com/book-tools/audnexus-kometa-series)**

## ✍️ Credits

Incipit is a fork of [Audnexus.bundle](https://github.com/djdembeck/Audnexus.bundle) by
[@djdembeck](https://github.com/djdembeck), and keeps its GPL-3.0 licence. The search,
update and Plex-agent scaffolding are upstream's work; what this fork adds is described
in [About](#about). The inherited release history is in
[CHANGELOG.md](CHANGELOG.md).

Series data comes via [rreading-glasses](https://github.com/blampe/rreading-glasses)
(GPL-3.0), which is what the `GOODREADS_SERIES_URL` setting on the API points at.

- [Contributing](CONTRIBUTING.md)
