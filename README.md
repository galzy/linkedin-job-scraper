## LinkedIn Job Scraper

Scrapes LinkedIn's guest job-listing API, dedupes and filters the results, and stores them in a
SQLite database. Built to run unattended on a schedule: it keeps every job it has ever seen, so a
job is scraped once and never re-fetched, and it fills in descriptions for the jobs your filters keep.

> **Note**
> LinkedIn does not permit scraping — see the [DISCLAIMER](DISCLAIMER.md). Use at your own risk.

### Setup

Needs Python 3.14+ and [uv](https://docs.astral.sh/uv/) (the project is uv-managed; `uv.lock` is
committed). Clone the repo, then:

```
uv sync
uv run linkedin-scraper init-config configs/config.yaml    # then edit it — see Configuration
```

`uv sync` installs the project editable — exposing the `linkedin-scraper` command used below — along
with the `dev` group (pytest, ruff); a cron box that only runs the scraper wants `uv sync --no-dev`.
Config files live in `configs/`, which git ignores bar the committed sample, so your searches and
exclude lists never reach a commit.

### Configuration

`configs/config.yaml` drives the scraper. It is validated on startup with Pydantic: a missing
required key, or a value out of range or not among the names below, raises a `ConfigurationError`
naming the field before any scraping begins. `configs/config.sample.yaml` is a worked example of
everything here.

> **Unknown keys are silently ignored, not rejected.**  Check
> spelling against the names below.

**Options**

- `search_queries` (array, required) — one or more searches, each with:
  - `keywords` (string, required) — a boolean expression of `OR`, `AND`, and parentheses. LinkedIn
    matches it against the **whole posting**, not just the title (see [How it works](#how-it-works)).
  - `location` (string, required)
  - `distance` (string) — max radius from `location`. Omit for none. Names: `ANY` (same as omitting),
    `KM_0` (the resolved point only), `KM_8`, `KM_16`, `KM_40`, `KM_80`, `KM_160`.
  - `timespan` (string) — how recent. Names: `DAY`, `WEEK`, `MONTH`, `ANY`.
  - `workplace_type` (array) — per-query keep-list of `on_site` / `remote` / `hybrid`. It is both the
    types searched and the types kept; `[]` or omit means all three (see [How it works](#how-it-works)).
- `title_include` (array) — keep only jobs whose title matches one of these. **Omit** and it becomes
  the terms of every query's `keywords` — usually what you want; set `[]` to disable.
- `title_exclude` (array) — then drop jobs whose title matches one of these. Entries may
  themselves be anchored lists, flattened on load, so you can group phrases into named sections
  (see [Reusing a value across queries](#reusing-a-value-across-queries)).
- `company_exclude` (array) — then drop jobs whose company name equals one of these.
- `http` (object) — request-layer tuning; every field optional. See [Tuning](#tuning).

The two title filters match **case-insensitively on substrings**, and a list matches if *any* entry
does: `title_exclude: [senior]` drops `Senior-Adjacent Engineer`. `company_exclude` matches the
**whole company name** (case-insensitively), so an entry must be the full name — `Turing`, not
`Tur` — and it won't catch `Hexagon Manufacturing`. An empty list disables that filter.

**Gotchas**

- `distance` and `timespan` take the **names** above (e.g. `KM_40`), not LinkedIn's numeric codes.
  Omitting a filter and setting it to `ANY` both mean "no filter"; an empty string (`distance: ""`)
  is an error. `KM_0` is a radius of *zero*, pinning results to the exact resolved point — not `ANY`.
- YAML types values implicitly, so a bare `location: NO` is the boolean `False`, not Norway. Quote
  anything that could read as a boolean, number, or date: `location: "NO"`.

There is no page-count setting: every query is paged to the end of its results, which LinkedIn caps
at 100 pages (1000 results) per query. Use `scrape --max-pages` for a quick, shallow run instead.

#### Reusing a value across queries

Share a long `keywords` expression across queries with a YAML anchor (`&name` / `*name`), and merge a
mapping of shared filters into a query with `<<: *name` (keys on the query win). Put the anchors under
any top-level key the schema doesn't declare — it's ignored. `configs/config.sample.yaml` shows both.

### Usage

The CLI is a set of subcommands. `scrape` is the one you run on a schedule; the rest set up a config
or act on already-stored jobs without re-scraping.

```
uv run linkedin-scraper scrape                                  # configs/config.yaml, every query to exhaustion
uv run linkedin-scraper scrape configs/other.yaml --max-pages 2 # another config, two pages per query
```

| Command | What it does |
| --- | --- |
| `scrape [config] [--max-pages N]` | Scrape, filter, and store jobs, then refresh the relevant ones — fetching descriptions and re-checking open-status. |
| `init-config <path>` | Write a starter config from the sample. Refuses to overwrite an existing file. |
| `recheck-relevance [config]` | Re-apply a config's filters to every stored job, flipping `is_relevant` — a filter edit's effect without waiting for the next scrape. |
| `refresh [config] [--reverify-after-days N]` | Fetch missing descriptions and re-check open-status for stored relevant jobs due for it — those older than N days (default 7) and not verified since. Also fills descriptions a blocked run missed. Uses the config's `http` settings only. |
| `status` | Print the last run — when, how it ended, its counts — and the stored-job totals. |

`config` defaults to `configs/config.yaml`. `--max-pages` caps every query for a quick run; it only
ever lowers LinkedIn's own 100-page ceiling, and lives on the command line so no checked-in file can
silently truncate a real scrape.

`uv run python -m linkedin_scraper` runs the same thing if you'd rather not use the console script.
Either way, `configs/`, `linkedin_jobs.db`, and `logs/` resolve against the repository root rather than
the working directory, so a cron job or systemd unit needs no `WorkingDirectory` of its own. The exit
status tells a scheduler how a run ended:

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | config or database error |
| `2` | usage error |
| `3` | blocked by LinkedIn |
| `4` | no filtering session drawn (aborted before scraping — see [How it works](#how-it-works)) |

### The database

There is no built-in viewer — the scraper only writes. `linkedin_jobs.db` is an ordinary SQLite file;
read it with whatever you already use.

Every run writes each job it scrapes to **`jobs_raw`** — raw in that it holds every job, filtered or not:

| Column | Contents |
| --- | --- |
| `job_url` | The posting URL; the primary key. Carries LinkedIn's posting id. |
| `title`, `company`, `location`, `date` | As scraped from the listing. |
| `country` | The country `location` names, normalized to English; `NULL` when it resolves to none. |
| `workplace_type` | `on_site` / `remote` / `hybrid`, read from which typed search surfaced the job; `untagged` until one does. |
| `first_seen` | When first stored. A run's new jobs share one timestamp. |
| `last_seen` | When last scraped. Moves on every sighting; `first_seen` doesn't. |
| `runs_seen` | How many runs surfaced this job — one per run, however many searches found it. |
| `is_relevant` | Whether the config's filters keep the job. `NULL` until first judged, then recomputed every run. |
| `job_description` | `NULL` until the job passes the filters, then its full text — or `Could not find job description` when the page genuinely has none. |
| `is_open` | Whether the posting still accepts applications, read from its page when refreshed. `NULL` until first checked; `0` once it closes. |
| `last_verified` | When `is_open` was last checked; `NULL` until the first check. |

Day to day, query the **`jobs_filtered`** view — `jobs_raw` with the rejected postings, and any that
have since closed, hidden (a not-yet-checked job stays visible). Reach for `jobs_raw` itself to audit:

```sql
-- what the filters keep
SELECT * FROM jobs_filtered;
-- what they threw away: do these read like postings you really don't want?
SELECT title, company FROM jobs_raw WHERE is_relevant = 0;
-- jobs first stored on a given day
SELECT * FROM jobs_raw WHERE first_seen LIKE '2026-07-08%';
-- the jobs that keep coming back, run after run
SELECT job_url, runs_seen FROM jobs_raw ORDER BY runs_seen DESC;
```

**Provenance tables.** Four more tables record where the jobs came from: `queries` is every distinct
search, keyed by a content hash; `job_queries` records which query found which job (with a per-query
sighting count); `runs` is one row per run — timestamps, status, counts, and the exact config file it
used; `run_queries` ties a run to its queries.

```sql
-- which searches surfaced a given job
SELECT q.label FROM queries q JOIN job_queries jq USING(query_id) WHERE jq.job_url = '...';
-- the config a run actually ran with
SELECT config_yaml FROM runs WHERE run_id = 1;
```

### How it works

**Storage and dedup.** `job_url` is the primary key and carries LinkedIn's posting id, so duplicates
collapse onto one row — two openings under one title stay two rows, one opening whose title drifts
between pages stays one. (A card with no posting id is skipped, with a warning.) Storing every job,
kept or not, is what stops the rejected ones being re-scraped.

**Relevance is cached, not intrinsic.** `is_relevant` answers a question about the *config*, not the
job, so editing the config stales every stored verdict. Each run re-decides the whole table, not just
the jobs it scraped, and rejected rows stay in `jobs_raw` so you can audit what the filters threw
away. (`recheck-relevance` does this without scraping.)

**Descriptions are backfilled.** Each run fetches descriptions for every relevant job that still
lacks one, not only newly-seen ones. A failed fetch (timeout, 429) leaves `job_description` `NULL`
and is retried next run, as long as the job keeps turning up. A page that loads with genuinely no
description stores the literal `Could not find job description` and is not retried.

**Why `title_include` exists.** LinkedIn matches `keywords` against the whole posting, not the title,
so a `python` search also returns sales roles that merely mention it. `title_include` is the only
filter that reads the title, which keeps `title_exclude` finite — the excludes only name the
near-misses the includes let through. It defaults to every query's `keywords` flattened to its terms;
a `NOT` expression can't be flattened safely and is rejected, so set `title_include` yourself then.

**Workplace type.** LinkedIn never returns a job's workplace type — it's only a search filter you
send in. So each search runs once per type in its `workplace_type` keep-list, and a job's type is
read from which variant surfaced it. This multiplies the cheap search-page fetches (~3× at most), not
the deduped description fetches. The keep-list is thus both a search plan and a filter: narrowing it
to `[remote]` stops searching the other types *and* rejects them.

**The session draw (why a run probes first).** LinkedIn's guest endpoint deals each fresh session one
of two pipelines, fixed for its life: one honors the workplace filter, the other ignores it and
serves every variant the same unfiltered list. The draw is roughly a coin flip, so each run probes —
remote page 1 vs. catch-all page 1; identical means non-filtering — and redraws up to 10 times. If
every draw misses, the run aborts with exit 4 rather than fake every label, leaving stored labels
untouched for a later run.

**Empty pages and blocks.** A block can also arrive as an empty `200`, indistinguishable from a query
that has run out. Two guards: an empty page is confirmed by two consecutive refetches (the endpoint
serves flaky empties), and when a query comes back empty on page 1 the scraper fires a **canary** —
the first query broadened to drop every narrowing filter, so it cannot honestly return nothing. An
empty canary ends the run with `Blocked by LinkedIn` and a non-zero exit; either way, the jobs
already scraped are stored first.

### Tuning

#### The `http` block

All requests flow through one rate-limited, connection-pooling client. A single shared limiter caps
the **global** request rate across both the search and description phases, so you can fetch in
parallel without ever exceeding a safe per-minute rate — concurrency and request rate are decoupled.
Every field is optional; the defaults (the `HttpConfig` fields in `linkedin_scraper/config.py`) are
conservative:

| field | default | purpose |
|---|---|---|
| `max_requests_per_minute` | `20` | Global cap across all threads. **The main safety dial.** |
| `rate_jitter` | `0.4` | How far each gap between requests strays from the mean (±40%). Even spacing is a bot tell; symmetric, so it does not change the rate above. Must be `>= 0` and `< 1`. |
| `search_workers` | `3` | Parallel search-page fetches (still under the rate cap). |
| `description_workers` | `3` | Parallel description fetches. |
| `timeout` | `20.0` | Per-request connect+read timeout. Higher = fewer false-timeout retries. |
| `retries` | `5` | Attempts per URL before the page is skipped. A 4xx other than 403/429 is not retried; a throttle consumes an attempt. |
| `backoff_base` / `backoff_max` | `2.0` / `60.0` | Exponential backoff between retries, capped. Applies to timeouts and network errors; throttles sleep per `retry_after_cap` instead. |
| `backoff_jitter` | `2.0` | Random jitter added to each backoff (breaks the retry fingerprint). |
| `retry_after_cap` | `120.0` | Ceiling on the post-throttle sleep, and the full sleep when the response carries no usable `Retry-After` (a 403 or 999 never does). |

If `Throttled` starts showing in the log, LinkedIn is turning you away — HTTP 429, 403, or the 999
authwall. All three lift on their own and are waited out and retried; the client honours
`Retry-After`, and every third throttle it halves its own rate for the rest of the run, down to one
request a minute. The durable fix is to **lower `max_requests_per_minute`** (try `10`–`15`).

Each query logs how it ended: `exhausted after N pages` is the healthy case; `page N failed; stopping
early` means a fetch gave up after all its retries (usually rate limiting), which ends the run —
lower `max_requests_per_minute`; `hit the 100-page ceiling` means the query has more than 1000
results and you are seeing only the first 1000, so narrow it with `timespan`, `distance`, or tighter
`keywords`.

#### Logging

Log levels and rotation are constants in `linkedin_scraper/logger.py`: `LOG_LEVEL_CONSOLE` (`INFO`)
and `LOG_LEVEL_FILE` (`DEBUG`) set what reaches the terminal and the file; `LOG_ROTATION` /
`LOG_RETENTION` (`00:00` / `10 days`) write one file per day to `logs/YYYY-MM-DD.log` and delete it
after ten days. Set `LINKEDIN_SCRAPER_LOG_DIR` to write the logs somewhere else.

### Layout

```
configs/
  config.sample.yaml     committed template — copy to configs/config.yaml
  config.yaml            your input (git-ignored, like any config here)
linkedin_jobs.db         output: jobs_raw, jobs_filtered, and the provenance tables
logs/2026-07-08.log      diagnostics, one file per day
tests/                   pytest suite
linkedin_scraper/
  __main__.py            the -m entry point; a shim over cli.py
  cli.py                 the CLI: the argument parser and the subcommands behind it
  main.py                a scrape run end to end: scrape, dedupe, store, filter, refresh
  config.py              config schema, validation, loading (Pydantic)
  filters.py             transforms: workplace types and the relevance predicate
  job.py                 the Job record, frozen; shared by parsing, filters, and the store
  geo.py                 the country a job's location names
  logger.py              loguru setup and its defaults
  constants.py           paths, LinkedIn endpoints, DB identifiers
  net/http.py            rate-limited, connection-pooling HTTP client
  scrape/
    scraping.py          drives the pagers: which pages to fetch, the session draw, the canary
    parsing.py           parse LinkedIn job pages (HTML -> Job)
  store/
    schema.py            the tables and view as declarative models
    db.py                the upserts, relevance refresh, and description backfill
```

### Tests

```
uv run pytest
```

The suite hits no network — `db.py` is exercised against an in-memory SQLite database.

### Origin

Forked from [cwwmbm/linkedinscraper](https://github.com/cwwmbm/linkedinscraper) around October 2024,
at commit [`8929765`](https://github.com/cwwmbm/linkedinscraper/commit/8929765f1cda26f3a1534813b63887e8f741aae8)
(upstream has no release tags, so the SHA is the only pin). Diverged since: uv-managed, Python 3.14,
code moved into a package, Pydantic config validation, a rate-limited client with parallel
description fetches, and upstream's Flask UI (`app.py`) dropped.

> **License**
> Released under the [MIT License](LICENSE) as original work, with thanks to `cwwmbm` for the
> original project this began from. Upstream itself ships no license. See also the
> [DISCLAIMER](DISCLAIMER.md).
