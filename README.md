# Movie Poster Studio

A Windows desktop tool for scraping, converting, and organizing movie posters for use with **Movie Genre Workshop**. It pulls posters from The Movie Database (TMDB), composites them onto a fixed canvas with an overlay strip, lets you browse and curate the results, and exports the slot JSON files Movie Genre Workshop reads.

Everything runs from one window with five tabs. The app is fully portable — copy the folder anywhere, or onto a USB drive, and your posters, config, and exports go with it.

---

## First-time setup

You need three things before the app is useful:

1. **A TMDB API key.** Sign up at [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) and grab a v3 key. Paste it into the API Key field on the TMDB Fetch tab.

2. **Required Mod** Movie Genre Expansion (plus Workshop_Exporter_Full Covers) https://www.nexusmods.com/retrorewindvideostoresimulator/mods/130?tab=description
	Make sure to get the 18 Genre Revamp under Miscellaneous files.

3. Nothing else. All folders are created automatically on first use.

---

## Typical workflow

1. **TMDB Fetch** — pick genres, limits, and year range → download raw posters.
2. **Gallery** — browse, delete unwanted posters, fix genres, set ratings.
3. **Duplicate Checker** — find and resolve posters that landed in more than one place.
4. **Export** — write the four JSON files Movie Genre Workshop needs.

---

## Tab 1 — TMDB Fetch & Convert

Downloads poster images from TMDB into genre folders. The posters are automactically converted.

### Fields

| Field | Description |
|---|---|
| **TMDB API Key** | Your v3 API key from themoviedb.org. Saved between sessions. |
| **Output Folder** | Root folder for downloaded posters. Defaults to `posters/` next to the exe — leave it alone unless you have a reason. |
| **Save Posters To** | **Generic** — main library. **New Releases** — separate set for NR standee slots. |
| **Poster Size** | TMDB CDN resolution. `w780` is recommended — high enough for conversion without huge downloads. |
| **Start Year / End Year** | Restrict results to this release-year window. Leave blank for no restriction. |
| **Production Country** | Filter to films produced in a specific country. **Any** skips this filter. |
| **Original Language** | Filter to films in a specific original language. **Any** skips this filter. |
| **Rating** | **Any** — no filter. **Good Critic** — high-rated films (maps to the 4–4.5★ RR bucket). **Bad Critic** — low-rated films (maps to the 0–1.5★ bucket). |

### Per-genre limits

Below the main fields is a row of genres, each with a number box. That number is the maximum posters to download for that genre (blank = 100, `0` = skip). Available genres depend on which set you selected:

- **Generic:** Action, Adult, Adventure, Comedy, Crime, Documentary, Drama, Fantasy, History, Horror, Holiday, Family, Music, Romance, Sci-Fi, Sports, Thriller, Western.
- **New Releases:** Action, Comedy, Crime, Drama, Fantasy, Horror, Family, Romance, Sci-Fi, Western, Holiday.

### Running a fetch

Click **Start TMDB Fetch**. Pages are fetched in parallel and poster downloads run concurrently. Files that already exist on disk are skipped automatically, so re-running a fetch is safe — it only picks up new results.

Progress and any errors appear in the status panel below the button. A per-genre CSV is also written to `metadata/csv_lists/` recording what was pulled.

---

## Tab 2 — Gallery

Browse and manage your converted posters.

### Layout

Two tabs at the top: **Generic Posters** and **New Release Posters**. Each has a genre shelf on the left and a poster grid on the right.

**Genre shelf:** Every RR genre is listed even if empty (shows `(0)`). Click a genre to filter the grid; click **ALL** to clear the filter. The search box below filters by title within the selected genre.
The sort dropdown below that controls grid order: Title A→Z (default), Title Z→A, Year Newest, Year Oldest, Stars High→Low, Stars Low→High.

**Poster tiles** show:
- The converted poster image
- A colored genre chip and release year along the bottom
- A star rating (0–5★ in 0.5-star steps) in the bottom-right — gold if rated, dimmed if no rating is on file
- A gold **OLD** badge in the top-right if the poster is flagged as old

### Selecting posters

Click a tile to select it (magenta border + checkmark); click again to deselect. Selection is independent on each tab — switching between Generic and New Release tabs doesn't clear either selection, so you can select from both and act on each set separately.

When one or more tiles are selected, a toolbar appears at the top of the grid:

| Button | What it does |
|---|---|
| **Genre** | Move selected posters to a different genre folder |
| **Rating** | Set the RR star rating for selected posters (updates `movies.json`) |
| **→ NR** / **→ Generic** | Re-convert from the raw TMDB source using the other profile and move to the other set |
| **Mark Old** / **Unmark Old** | Toggle the OLD flag — adds a gold badge and includes the poster in `edited_slots.json` on export |
| **Delete** | Permanently delete selected files from disk |
| **Select All** | Select all currently visible posters (respects genre/search filter) |
| **Clear** | Deselect everything |

### Right-click menu

Right-clicking any tile opens a context menu with the same actions. If the tile is part of the current selection, the action applies to the whole selection. If not, it applies to that tile alone.

### Set-swap note

**→ NR / → Generic** re-converts from the original raw TMDB poster (in `posters/<set>/<genre>/`). If you deleted the raw posters to save space, this will log a warning per missing file — re-scrape those movies to restore the source files.

---

## Tab 3 — Duplicate Checker

Finds posters that appear in more than one place and lets you resolve conflicts.

Click **Rescan** to start. Both converted sets are walked concurrently. Results are split into two sections:

### Cross-set duplicates

The same filename exists in both the Generic and New Release sets. Use the bulk buttons **Keep All Generic** or **Keep All NR** to resolve everything at once, or use the individual **Keep** button on each card to decide per-movie. Keeping one side deletes the other.

### Cross-genre duplicates

The same filename exists in two or more genre folders within one set — for example, a movie that was scraped under both Action and Sci-Fi. Tap the genre chip you want to keep; copies in all other genre folders are deleted. 

**Clean All** bulk-resolves by keeping the alphabetically first genre for every group.

The genre filter chips across the top let you focus on one genre at a time.

---

## Tab 4 — Export

Writes the four JSON files Movie Genre Workshop reads.

### Fields

| Field | Description |
|---|---|
| **Library Root** | Folder containing your converted poster sets. Defaults to `posters/converted/` next to the exe. |
| **Output Folder** | Where the JSON files are written. Defaults to `RR_Export/` next to the exe. |
| **Rarity** | **Common** — all slots are common. **Common (Old)** — OLD-flagged slots export as old-common. **Limited Edition (holo)** — holo rarity by SKU. **Random** — deterministic rarity formula (same movie always gets the same rarity). |
| **NR Standee Shape** | Standee shape for New Release slots: **A**, **B**, or **C**. |

### Output files

| File | Contents |
|---|---|
| `custom_slots.json` | Generic poster slots grouped by RR DataTable name |
| `nr_custom_slots.json` | New Release poster slots as a flat list |
| `replacements.json` | Full slot definitions including absolute file paths |
| `edited_slots.json` | Posters flagged as OLD, for Movie Genre Workshop's edited-slot system |

Click **Export to Movie Genre Workshop**. The result panel shows counts for each file, the output paths, and any warnings about posters whose files are missing from disk. Poster paths in `replacements.json` are absolute, so the file works regardless of where Movie Genre Workshop is launched from.

---

## Where everything lives

All data is stored relative to `movie_poster_studio.exe`:

```
movie_poster_studio.exe

posters/
  generic/<genre>/              raw scraped posters from TMDB
  new_releases/<genre>/         raw scraped NR posters
  converted/
    generic/<genre>/            composited generic posters  ← Gallery reads from here
    new_releases/<genre>/       composited NR posters

overlays/
  generic/overlay1.png          required dark variant
  generic/overlay2.png          optional light variant (auto-generated if absent)
  new_releases/overlay1.png
  new_releases/overlay2.png

metadata/
  movies.json                   TMDB ratings, titles, OLD flags — keyed by filename
  csv_lists/<genre>_<years>.csv per-scrape download record

config.json                     saved settings (paths stored relative to exe)

RR_Export/
  custom_slots.json
  nr_custom_slots.json
  replacements.json
  edited_slots.json
```

---

## Common problems

**"TMDB API Key Required" popup.** Paste your TMDB v3 key into the API Key field on the TMDB Fetch tab and try again.

**Converter says "overlay1.png is missing".** Drop an `overlay1.png` into `overlays/<set>/` next to the exe. `overlay2.png` is optional.

**Export warnings: "no metadata for X.png".** That poster has no entry in `movies.json`, so its rating defaulted to 0.0★. Either re-scrape to pull the TMDB rating, or right-click the tile in Gallery → Set Rating to assign one manually.

**Set-swap says "no raw TMDB source".** The → NR / → Generic action needs the original file from `posters/<set>/<genre>/`. Re-scrape the movie to restore the raw source.

**Posters outside my year range keep appearing.** The scraper doesn't delete existing files when you change the year filter — it just won't download new ones outside the window. Delete the genre folder manually before re-scraping if you want a clean slate.
