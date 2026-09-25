# MediaNomenclator

**An intelligent toolkit for media file identification, metadata organization, filename normalization and renaming.**

MediaNomenclator takes a messy media library — files ripped, named and organized in dozens of different ways —
and turns it into a **consistent, scraper-friendly, long-term maintainable** library:
identify the work → fetch and reconcile metadata → normalize filenames → rename videos → rename subtitles and sidecar files in sync.

The project is organized as **Skills** (capability packs an AI assistant can load and execute) and ships runnable
Python tooling. It has exactly one governing principle:

> **Prefer leaving a file untouched over guessing wrong.**

When information cannot be reliably confirmed, the tools keep the original name and flag it as `NEED_REVIEW`
instead of filling the filename with guesses.

- 中文说明：[README.md](README.md)

---

## Why this project exists

It started from one very concrete problem.

To get danmaku (bullet comments) in a local media library, I deployed a danmaku API server —
[danmu_api](https://github.com/huangxd-/danmu_api), a self-hosted danmaku API server compatible with the
dandanplay API specification. Its matching endpoint **parses the work title, season number and episode number
out of the filename**, then uses that to perform title comparison and season/episode resolution before
locating the exact episode to fetch comments for.

That is exactly where it breaks: **if the video filename is not normalized, the parse fails.**

The failure modes I ran into:

- Filenames buried under release-group, subtitle-group, resolution and codec noise, with the title
  chopped into dot-separated fragments that no longer read as a title.
- The same work written as an English name, a romanization, or an alias in the filename, which no longer
  matches the title on the matching side.
- Season and episode numbering written a dozen different ways — `1x01`, `EP01`, `第01集`, `S1E1`, or
  missing entirely.
- Sequels named as standalone works, with the season number reset.
- External subtitles carrying assorted language suffixes that no longer pair with their video.

The result: **danmaku either fails to resolve, or resolves to the wrong episode.** With dozens of episodes
per work, renaming everything by hand is not realistic — and a name collision or an overwrite can lose the
original file for good.

So this project exists to **get the filenames right first, so that downstream matching can hit.**

Anime is by far the worst offender — many aliases, and season/episode divisions that frequently disagree
between reference databases. That is why this project **plans** to integrate the **dandanplay open API** as
one of the data sources for anime work identification and episode matching: an additional line of evidence
raises the accuracy of anime filenames, so downstream danmaku matching lands on the right episode.

> ⚠️ To be explicit: **this project does not fetch, store or distribute danmaku**, and contains no danmaku
> data whatsoever. It does one thing — normalize the **names** of media files so that downstream services
> matching by name can match them. Fetching and serving comments is the downstream service's own concern.

---

## In scope / out of scope

| | |
|---|---|
| ✅ **In scope** | Media file identification, metadata retrieval and reconciliation, filename normalization, video renaming, subtitle and sidecar renaming, folder structure normalization |
| ❌ **Out of scope** | Fetching / storing / serving danmaku; media download or indexing; NFO / poster / thumbnail generation (that is a scraper's job); transcoding / compression / muxing / cutting |

Being explicit about the boundary matters, so the project is not mistaken for a danmaku tool.

---

## Core capabilities

| Capability | Description |
|---|---|
| **Media file identification** | Determine work identity, media type (movie / series / anime / documentary / variety), year, season and episode from file and folder names |
| **Metadata retrieval & reconciliation** | Fetch work identity, season structure, season names, episode titles and regional aliases from external data sources, with per-field acceptance rules |
| **Filename normalization** | Unify field order, separators, casing and punctuation; strip release groups and noise; handle illegal characters |
| **Video renaming** | Rename videos using a single template, preserving the original container extension — never transcoding |
| **Subtitle / sidecar sync** | Pair external subtitles by identical basename; apply language policy; probe but never modify embedded subtitles |
| **Per-media-type rules** | Movies and episodic content use different naming templates, tech-info policies and folder structures, all configuration-driven |
| **Pluggable external data sources** | Data sources are adapters; the primary source can differ per media category |
| **Downstream-parseable filenames** | The produced form (`Title.Year.SxxExx.「Episode Title」.ext`) is easy for downstream services to parse by title + season/episode |
| **Swappable storage backends** | Local filesystem / Alist mount / cloud-drive MCP / CloudDrive2 gRPC API — naming rules are backend-independent |

---

## What is implemented today

1. **Configuration-driven naming rules engine** covering movies, series, anime, documentaries and variety shows.
2. **Chinese-title-first language policy** — a Chinese title is used when it can be reliably confirmed from
   trustworthy sources, otherwise the work's official original title is kept. Machine translation is forbidden.
3. **Season / episode detection**, including multi-season and sequel handling based on the data source's season
   structure rather than filename heuristics.
4. **Episode title retrieval with fake-title detection** — placeholder numbering (`Episode 1`, `第1集`) and
   site-invented episode blurbs are rejected; when no reliable title exists the segment is omitted entirely.
5. **TMDB data source adapter (implemented)** — read-only API v3 client; one call returns the full season structure
   and all episode titles. Includes placeholder detection, masked credential echo, and a DoH-based workaround for
   DNS poisoning (pinned real IP + correct SNI, certificate validation kept on).
6. **Folder normalization** — work folders renamed to the official title (no year), season folders normalized to
   `第X季`, no redundant season level for single-season works, specials/extras folders left untouched.
7. **External subtitle synchronization** with pairwise video↔subtitle verification; ambiguous cases are flagged for
   human review instead of being resolved automatically.
8. **Safe execution pipeline** — plan generation (dry-run) → plan linting → user confirmation → batched execution →
   directory re-listing verification → content-integrity snapshot comparison.
9. **Content integrity proof** — snapshots of `name + size + MD5` prove that only names changed and file contents
   were not modified by a single byte.
10. **Multiple storage backends** sharing the same naming rules.

---

## What is NOT implemented (planned only)

| Item | Status |
|---|---|
| **dandanplay data source integration** | **Planned.** API access has **not** been granted; no AppId/AppSecret/API key exists, and no dandanplay calling code is included |
| Switching the anime category's primary source to dandanplay | Planned |
| Plugin-style data source registry | Planned |
| Additional media types (music, audiobooks, …) | Planned |
| End-to-end verification against a downstream matcher | Planned |
| Scraping / NFO / poster generation | **Out of scope** — this project only handles names |
| Danmaku fetching / storage / serving | **Out of scope** — the downstream service's concern; this project contains no danmaku functionality or data |
| Transcoding / compression / muxing / cutting | **Out of scope** — explicitly forbidden |

This project has **no** hosted service, **no** commercial offering and claims **no** user base.
It is a personal media-library organization toolkit maintained as open source.

---

## Project layout

```text
media-nomenclator/
├── README.md / README.en.md
├── LICENSE                     # MIT
├── .gitignore
├── docs/                       # architecture, naming rules, data sources, roadmap
├── skills/
│   └── media-library-auto-rename/   # core capability pack
│       ├── SKILL.md
│       └── naming-config.example.json
├── src/
│   ├── naming/techinfo_reorder.py       # tech-info field normalization
│   ├── sources/tmdb/tmdb_api.py         # TMDB read-only data source
│   └── storage/cd2/                     # CloudDrive2 gRPC backend + plan executor
└── examples/rename-plan.example.json
```

---

## Data sources

| Data source | Used for | Status |
|---|---|---|
| **TMDB** | Work identity, year, season structure, season names, episode titles, regional aliases | **Implemented** |
| Official platforms / official subtitles / official release names | Authoritative single source for Chinese titles and episode titles | Implemented (verification workflow) |
| Douban | Supplementary source for Chinese titles and episode titles | Implemented (verification workflow) |
| Wikipedia | Supplementary source | Implemented (verification workflow) |
| **dandanplay open API** | **Planned**: one of the data sources for *anime* work identification and episode matching | **Not integrated**, access not granted |

Acceptance thresholds are **per field**, not all-or-nothing:

| Field | Threshold |
|---|---|
| Chinese drama title | Must still be corroborated by **1 independent source** (official sources count directly); otherwise fall back to the official original title |
| Year / season structure / season name / episode title | **Single source is sufficient** |

> Multiple language variants inside the same data source (e.g. TMDB `zh-CN` vs `zh-TW`) **do not count as two
> independent sources**.

---

## Where dandanplay fits

The dandanplay open API is **planned** to serve as **one of the data sources** for **anime** media identification
and episode matching, helping to determine anime works and their specific episode information.

**Why anime specifically needs it**: anime is the category that benefits most from an extra line of evidence.
The same anime may carry different aliases and translated titles across reference databases, and its
season/episode divisions frequently disagree between them. If a downstream name-matching service receives
season/episode numbers that follow a different convention than its own, it will match the wrong episode.
An additional anime-focused source acts as cross-validation and materially reduces identification and
season-mapping errors.

It is intended to help with two things:

1. **Anime work identification** — locating the work's identity (Chinese and original titles) from messy filenames.
2. **Episode matching** — determining which season and episode a file corresponds to.

On integration, only the anime category changes: anime's **primary** data source would switch from TMDB to
dandanplay, with TMDB / Douban / Wikipedia demoted to supplementary sources. All other categories
(series / movies / documentaries / variety) are unaffected, and the two-source threshold for Chinese drama titles stays in place.

**Current status (to avoid misunderstanding):**

- This project has **not** been granted dandanplay API access, and holds **no** AppId / AppSecret / API key.
- The repository contains **no** dandanplay calling code and **no** call results.
- dandanplay is **not** the driving force behind this project, and this project is **not** a dandanplay client or
  a dandanplay alternative.
- It is a single optional data-source slot, equal in standing to the other sources, covering the anime category only.

Accordingly, this project does not include — and does not plan to include — dandanplay danmaku data, player
functionality, account systems or community features.

---

## Usage (currently available parts only)

Requirements: Python 3.11+ (`tomllib`); `src/storage/cd2/` additionally needs `grpcio` and `protobuf`.

**As a Skill** — load `skills/media-library-auto-rename/`, copy and adapt
`naming-config.example.json`, then let the assistant run the pipeline defined in `SKILL.md`:

```text
scan → pair video/subtitle → identify work → query data source → resolve episode titles
     → read tech info → build plan → Dry Run → (user confirmation) → execute → verify
```

Nothing is written to disk without explicit confirmation.

**Command line:**

```bash
python src/naming/techinfo_reorder.py                      # self-tested, zero dependencies

python src/sources/tmdb/tmdb_api.py selftest               # key / DoH / connectivity / query / placeholder check
python src/sources/tmdb/tmdb_api.py search tv "query"
python src/sources/tmdb/tmdb_api.py tv <id>
python src/sources/tmdb/tmdb_api.py season <id> <season>

cd src/storage/cd2
python selftest_api.py
python v2_plan.py --root /media/tv/shows --out plans/tv.json
python apply_plan.py plans/tv.json                         # dry-run (default)
python apply_plan.py plans/tv.json --apply --yes --verify  # execute + verify
python snapshot.py --plan plans/tv.json --save baseline.json
python snapshot.py --plan plans/tv.json --diff baseline.json
```

Credentials are read from environment variables (`TMDB_API_KEY`, `CD2_API_TOKEN`) or from files under
`~/.config/media-nomenclator/`. They are never committed, never written into configuration, and only ever echoed masked.

---

## Safety design

This tooling operates on real files, so safety outranks feature richness:

- **Dry Run is mandatory** — the executor only prints unless explicitly given `--apply --yes`.
- **Never overwrite** — an existing target yields `SKIPPED_CONFLICT`; move operations always use the `skip` policy.
- **Never guess** — unverifiable items become `NEED_REVIEW` and stay untouched.
- **No general deletion** — the shared client and executor contain no delete capability. Deletion is isolated in a
  dedicated script that accepts only subtitle files or provably empty directories, always lists the plan first,
  defaults to dry-run, and routes through the cloud recycle bin.
- **Names only** — no transcoding, no editing, no modification of subtitle content or timing.
- **Path guard** — the cloud execution path rejects drive letters, backslashes and UNC paths outright.
- **Idempotent retries** with backoff for rate limiting; completed items are skipped on re-run.
- **Integrity snapshots** (name + size + MD5) prove contents were untouched.
- **Batched execution** to avoid throttling and allow interruption at any time.

---

## Roadmap

1. Integrate the dandanplay data source (request access → implement a read-only adapter → switch the anime category)
2. End-to-end verification against a downstream matcher: confirm the service reliably resolves the right episode
   after a work has been normalized
3. Plugin-style data source registry with a unified `search` / `detail` / `season` / `episodes` interface
4. Diff tooling for season structure and episode titles when changing or adding a source
5. Extend the rules engine to further media types
6. Bring the cloud-drive MCP and local filesystem backends up to parity with the CloudDrive2 backend
7. Fill in unit tests and runnable examples

---

## Acknowledgements

- **[TMDB](https://www.themoviedb.org/)** — primary source for work identity, season structure and episode titles.
  *This product uses the TMDB API but is not endorsed or certified by TMDB.*
- **[dandanplay](https://www.dandanplay.com/)** — planned anime identification data source (access not yet granted).
- **Douban / Wikipedia / official distribution platforms** — supplementary sources for Chinese titles and episode titles.
- **[CloudDrive2](https://www.clouddrive2.com/)** — cloud-drive mounting and gRPC API; its `clouddrive.proto`
  is used to generate this project's gRPC client.
- **[danmu_api](https://github.com/huangxd-/danmu_api)** — the independently maintained downstream danmaku API
  server that motivated this project. This project includes **none** of its code and offers no danmaku functionality.

---

## License

[MIT](LICENSE). Media files themselves and any metadata obtained from the sources above remain the property of
their respective rights holders. This project provides naming and organization **tooling** only.
