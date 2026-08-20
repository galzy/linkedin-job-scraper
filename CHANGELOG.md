# Changelog

Started as a fork of [cwwmbm/linkedinscraper](https://github.com/cwwmbm/linkedinscraper) @ `8929765` (2024-08-17), rewritten since.

## [Unreleased]

### Fixed
- The `fit` judge read an empty verdict as "no condition applies" — also what a judge that has
  stopped reading returns for a whole batch, which cleared 43 of 1,050 ads on 2026-08-20. A clean
  verdict is now `ok`, and empty text fails `is_wellformed`, so such a batch fails loudly instead.

### Added
- `description_lang_include`, a config keep-list of ISO 639-1 codes for the language a description is
  written in. A description too short to name a language is never dropped by it.

### Changed
- The language of a description is judged on its words of prose rather than its character count: a
  skeleton of bullet dashes clears twenty characters and still classifies confidently.

## [v0.7.0]

### Changed
- LinkedIn stopped honouring the `f_WT` workplace filter on its guest search: every value now
  returns the same unfiltered list, on the API and on the plain search page alike, for every
  session. Verified 2026-08-19, after 13 nights in which every run aborted rather than label a job
  from a filter that was being ignored. So a search no longer fans out into one variant per
  workplace type — it runs once, unfiltered — and the session probe that looked for a filtering
  session is gone, along with exit code 4.
- A job's workplace type is now read from what its description states outright, in `workplace.py`.
  Only wording that settles the question counts; anything weaker leaves the type `untagged`, and an
  `untagged` job is never judged against a keep-list. Against the 2,338 ads LinkedIn had already
  labelled, that finds 32% of the known-remote ones and calls none of them on-site. A row that
  already carries LinkedIn's own label keeps it.
- Relevance is judged a second time once descriptions land, since that is where a type can appear.
- A query that hits the page ceiling now logs a warning rather than a note: unfiltered searches
  return more, so being cut short at 1000 results went from a curiosity to something to act on.

### Added
- A `HALF_DAY` `timespan`. The endpoint turned out to honour any `f_TPR=r<seconds>`, not only the
  windows LinkedIn's own UI offers, so a scrape run twice a day can halve what each query returns
  and stay clear of the 1000-result cap.
- A `fit` command: judges the last days' unjudged rows against `configs/fit-criteria.md` through
  headless `claude -p` calls — a batch of 12 ads per call, each stored as it lands — then writes
  each day's clean-or-`?` arrivals to `--dest` as `new-jobs-<date>.csv`. So `fit_verdict` is no
  longer hand-only: the judge follows the same rubric, and `is_wellformed` (new in `verdicts`)
  gates its output to the verdict grammar before anything is stored. A day's file is written once,
  and only after every row of the day is judged; today's waits for `--export-today`, which the
  nightly passes right after its scrape ends the day. An unreachable `--dest` (an unmounted drive)
  warns and leaves the export to a later run, as does a day whose judging failed.

## [v0.6.0]

### Added
- Keywords syntax validation. A malformed boolean expression (dangling operator, unbalanced or empty
  parens, no terms) now fails config load instead of silently searching for nothing.
- `stated_locations` on `jobs_raw`: where the ad's own text says you must be, comma-separated and
  normalized to English, with `EU` standing in for a clause open to the whole region. Read from the
  description by the new `signals` module, so it can disagree with `country`, which comes from the
  card. Matching is anchored on the phrases that put a place in location position, looked for in the
  ad's own `description_lang`, and skips clauses about someone else — a pay band, a vendor's address,
  the firm's own seat. (One-off `ALTER TABLE` and backfill; new databases get it from the model.)
- `location_phrases.yaml`, holding the phrases each language puts a place after, so adding a language
  is a data edit rather than a regex one. The guards that decide whether a clause is about *you* stay
  in code. A test enforces that every language it marks `covered` appears in `tests/test_signals.py`.
- `work_eligibility` on `jobs_raw`, the bars an ad sets on who may take it: a security clearance or a
  refusal to sponsor a visa, which scope a role to wherever it already sits without naming a country.
  Read in English only, off the description at fetch time like the other signals.
- `fit_verdict` on `jobs_raw`, the judgment written by hand against the fit rubric — codes and their
  reasons both. Nothing in the scraper derives it: a judgment from a person and a guess from a signal
  would be indistinguishable once stored. `import_verdicts` writes it, carrying a stated code to the
  rest of the posting's `dup_group`, since LinkedIn mints a fresh URL per repost; a `?` stays on its
  own row, and a repost only fills where nothing was written.
- `fit_cohort`, the rows awaiting a verdict — relevant, not confirmed-closed, and carrying none yet —
  newest first, with `since` to scope a pass to one run's arrivals. `status` counts the same set.

### Changed
- The refresh phase skips a posting already turned down outright — whether it is still open stopped
  mattering the moment it was judged. Rows carrying nothing but `?` codes stay in the set, since a
  suspicion means revisit, not rejected.
- `is_english` is now `description_lang`, holding the ISO 639-1 code rather than a boolean, so an
  Italian ad and a German one are no longer both just "not English". (One-off `ALTER TABLE` and
  backfill; new databases get it from the model.)
- The session probe logs its query and says why a draw was inconclusive (fetch failed, no results
  even unfiltered) instead of the catch-all "probe unanswerable".
- A draw whose remote page is empty while the unfiltered one has cards now counts as the filtering
  pipeline, where it used to be inconclusive and cost a redraw: only a session applying `f_WT` can
  serve nothing remote while the unfiltered page has cards. The probe had been redrawing past the
  sessions it was looking for, and an hour with no remote postings could exhaust all ten draws and
  abort the run. The empty page is refetched once before it counts, since the endpoint serves flaky
  empties. The unfiltered page is fetched first and settles a dry query on its own, so a draw costs
  one to three requests rather than always two.
- The `jobs_filtered` view orders rows newest-first, then by company and title.
- The refresh due-check drops its posting-date condition and keys on `last_verified` alone.
- Lowered the default `RECHECK_DAYS` to 3.

## [v0.5.0]

### Added
- An `export` command. Writes stored jobs from the DB to a CSV so you can open them in a spreadsheet without a
  SQLite client — the kept (relevant, not closed) set by default, or every stored row with `--all`.
  `--no-descriptions` drops the `job_description` column for a leaner sheet. Defaults to `reports/jobs.csv`
  (git-ignored) and overwrites, since the export is a regenerable snapshot.

### Self-healing
- Database open. A corrupt `linkedin_jobs.db` is quarantined and rebuilt instead of crashing, and WAL mode plus a
  busy timeout let a read (`status`/`export`) run during a live scrape without `database is locked`.
- Graceful failures. An unexpected error exits `5` (logged, not a raw traceback); an unwritable log directory falls
  back to console-only instead of crashing at startup.
- Atomic CSV export. A target locked by another program (open in a spreadsheet) leaves the previous export intact
  and exits `1` rather than clobbering it.
- Workplace type on jobs recovered from an interrupted run. The harvest type is now staged with each card
  (`harvest_type` on `scrape_staging`), so a job recovered under a query the current config has dropped is
  still labeled instead of defaulting to `untagged`.

## [v0.4.0 ]

### Added
- A list `location` on a `search_queries` entry. The entry fans into one query per location, sharing
  its keywords and filters, so the config need not repeat them once per place. A string location still
  means a single query.
- Duplicate flagging. LinkedIn mints a fresh URL each time a company reposts a role or fans one out across
  cities, so one job lands as several `jobs_raw` rows. Two new columns mark them without merging anything, so
  every listing keeps its own `is_open`/`location`/`date`. `dup_group` is the posting's identity — its title
  and company, lowercased and trimmed (not its description, which is `NULL` until fetched and reworded on
  reposts, so keying on it would split true duplicates rather than merge them). It is a generated column, so
  SQLite keeps it in step with the two source columns for free. `dup_count` is how many *other* kept (relevant,
  not closed) rows share the group — `0` for a lone posting — recomputed by `refresh_dup_counts` at the end
  of each run, once relevance and open-status have settled. So `WHERE dup_count = 0` on `jobs_filtered` drops
  the repeats; on the current database ~300 of ~1,260 kept rows carry a twin, a 16% shorter list once
  collapsed. (The two columns were added to the existing database by a one-off `ALTER TABLE`, then `dup_count`
  filled on the next run; new databases get them from the model.) The `recompute` command triggers the same
  recount after re-judging relevance, since that moves the kept set.
- Description language. `is_english` on `jobs_raw` flags whether a fetched description reads as English — a
  rough proxy for a role open to non-local candidates, since a local-language posting almost always wants that
  language even when it's remote. Judged by `langdetect` (seeded for determinism) the moment a description is
  written in `record_postings`, so it costs no extra fetch; `NULL` while a row has no description or the text is
  too short to call, `1`/`0` otherwise. The `jobs_filtered` view carries it, so `WHERE is_english = 1` narrows a
  search to the English postings. (The column was added to the existing database by a one-off `ALTER TABLE` and
  backfilled over its stored descriptions; new databases get it from the model.)
- Job open-status. `is_open` and `last_verified` on `jobs_raw` record whether a posting still accepts
  applications, read from the guest posting page: a closed listing swaps its apply button for a
  `figure.closed-job` "No longer accepting applications" banner, which `parse_job_open` keys on. A removed
  posting (a `404`/`410`, surfaced by the new `HttpClient.fetch` as `gone`) is recorded closed too; any
  other failed fetch leaves the row unchanged for a retry. A freshly scraped job is presumed open — it
  just surfaced in search — with `last_verified` left `NULL` so a later fetch still settles it; the upsert
  never touches `is_open`, so a verified verdict outlives a later sighting. It costs no extra requests —
  the description fetch already loads that page, so one fetch now yields both (`fetch_description` →
  `fetch_posting`, `describe_jobs` → `fetch_postings`, storing via `record_postings`). The
  `jobs_filtered` view hides confirmed-closed jobs (`is_open IS NOT 0`), keeping the not-yet-checked ones.
  `fetch-descriptions` is now `refresh`: it fetches missing descriptions and re-checks open-status for
  relevant jobs due for it — older than `--recheck-days` (default 7, by posting date, or
  `first_seen` when the card had none) and not verified since — so a fresh posting is left alone and a
  checked one isn't hammered. A scrape run refreshes the same worklist. (The two columns were added to
  existing databases by a one-off `ALTER TABLE`; new databases get them from the model.)
- Crash durability for the scrape. Each query's cards are flushed to a `scrape_staging` table the moment
  the query finishes, and the end-of-run pipeline reads the run back from it. A crash mid-scrape now leaves
  the finished queries' jobs on disk instead of losing the whole run; the table is cleared only after its rows
  reach `jobs_raw`, so the next run recovers any left by a crash rather than wiping them.
  `scrape_jobs` stages through a callback rather than returning its jobs in memory, so `BlockedError` no
  longer carries a payload. Within-run dedupe moved from `filters.remove_duplicates` (removed) to the staging
  read, keyed on `job_url`.
- `LICENSE` (MIT) and `DISCLAIMER.md`. The last routine carried over from upstream, the description
  parser (`parse_job_description`), was reimplemented first, so the project is wholly the author's own.
- A `status` subcommand: the last run — timestamps, completed or blocked, its counts — and the stored
  totals (jobs, relevant, missing descriptions), via two new `JobsDb` readers (`last_run`, `totals`).
  The morning-after check on a cron box, without opening the DB.
- The session pipeline draw. LinkedIn's guest endpoint deals each fresh session one of two sticky
  serving pipelines: one honors the `f_WT` workplace filter, the other ignores it and serves every
  variant the identical unfiltered list to the 1000-result cap (measured; see the README's
  "pipeline draw" section). A run now probes its session — remote page 1 vs catch-all page 1,
  back-to-back — and renews the session until a draw lands the filtering pipeline (up to 10 tries).
  When none does, the run aborts with exit 4 rather than scrape a session that would fake every
  workplace label; a later run draws afresh.
- `HttpClient.renew_session()`: a fresh cookie jar and browser profile on demand, for redrawing
  the pipeline without giving up the shared rate limiter or connection-pool settings.
- `country`, derived from each job's free-text `location` by `db._row` (not stored on `Job`, so it
  cannot drift from the string it reads). It is the rightmost comma segment `pycountry` recognizes,
  normalized to English; a small alias map covers colloquial names `pycountry` misses ("Russia",
  "UK", "Kosovo", ...). A comma-less metro-area label ("Greater Milan Metropolitan Area") resolves
  through the city its affixes wrap, looked up offline in GeoNames (`geonamescache`) and bounded to
  the countries the run's queries search, so a namesake abroad (Geneva, Illinois) can't win. A
  location that still names no country takes the single country its search queries named, if they
  agree; otherwise it is `NULL` rather than a guess. `location` is untouched. (City and region were
  dropped: positional splitting made them unreliable — a region read as a city, a city missing its
  region — and nothing consumed them.)
- `is_relevant`, and a `jobs_filtered` view over the rows it keeps. Irrelevant jobs were already
  stored, but nothing on a row said the filters had rejected it, so there was no way to check the
  filters against what they threw away. The verdict is a fact about `config.yaml`, not about the
  job, so every run re-decides it for the whole table — not just for the jobs it scraped, which
  would leave rows that aged out of the search results carrying an older config's answer. The
  filter itself has one implementation, `filters.relevance_predicate`, called by the scrape and by
  the refresh alike; a second copy in SQL could disagree with the scraper it exists to audit. A new
  row is stored `NULL` (unjudged) and settled on the next refresh, so the refresh is the one place a
  verdict is ever written. The run's `flipped` count is reversals of a prior verdict only — a first
  judgment, which is every row on a clean run, is not a flip.
- `runs_seen` and `last_seen`, so a row records how many runs have surfaced the job and when it was
  last surfaced. It counts runs, not cards: one search fans into a workplace-filter variant per kept
  type, and they re-serve the same posting, so a card tally would grade a job by the filter mechanism
  rather than its staying power. Per-query card counts still live in `job_queries`. Duplicate rows are deliberately not
  stored: a duplicate card is identical to the row it collapses into, and keeping them would cost the
  unique index.
- `sqlalchemy>=2.0.51` (the first release with CPython 3.14 wheels). `db.py` declares the schema as
  a `JobRow` model instead of a `CREATE TABLE` string, and builds its statements instead of
  interpolating identifiers into f-strings. Writes stay bulk — one `INSERT .. ON CONFLICT` and one
  `UPDATE` per run, not one per row — so this buys the declaration, not the unit of work.
- `job_url` is the primary key of `jobs_raw`, which is where its unique index now comes from. The
  table had no index at all: every description `UPDATE` scanned it end to end, once per description,
  and each run read every stored key into memory to dedupe against. SQLite now does the dedupe — an
  insert conflicting on `job_url` updates `runs_seen`/`last_seen` instead of adding a row — and the
  backfill is an index seek. The `NOT NULL` that `Mapped[str]` emits is load-bearing and the primary
  key is not: SQLite tolerates a NULL there, and two NULLs would be distinct.
- `Job`, a frozen pydantic model (`app/job.py`), replaces the bare dicts passed between parsing,
  filtering and storage. Frozen, so a job already handed to the database cannot be written through.
  It carries no persistence: `db.py` owns the `JobRow` model and converts at its own boundary, so
  parsing and filtering stay testable without a database.
- `rate_jitter` (default `0.4`): how far the limiter varies its own request spacing.
- `python -m linkedin_scraper` exits non-zero when the config or the database fails.
- Two distinct openings a company lists under the same title are kept apart. `remove_duplicates`
  collapsed jobs on (title, company), and the table keyed rows on (title, company, date), so
  the second posting was discarded in memory and again at insert. Both now key on `job_url` alone —
  LinkedIn's own posting id — so the in-memory dedupe and the index cannot disagree about what one
  job is, and a posting whose title text drifts between pages stays one row rather than two.
- Failed description fetches are retried on the next run. A fetch that gave up (rate limiting, a
  timeout) used to store the literal `Could not find job description`; because the job row already
  existed, it was never looked at again, so one bad run poisoned those rows permanently. The
  description phase now selects the relevant jobs with no description stored, rather than the jobs
  it has never seen, and a failed fetch leaves `job_description` NULL so the next run picks it up.
  A page that loads with no description still stores the placeholder — that is an answer, not a
  failure, and must not be refetched forever.
- Automatic paging: every query is now scraped until LinkedIn stops returning results (an empty
  200), rather than for a page count guessed in advance. A failed fetch is distinguished from a
  genuinely empty page, so rate limiting can no longer masquerade as "no more results". Paging stops
  at 100 pages regardless — LinkedIn 400s on `start >= 1000`.
- Rate-limited, connection-pooling HTTP client (`app/http.py`): one shared `RateLimiter` caps the
  global request rate across all threads, so search and description fetches run in parallel
  without exceeding a safe req/min ceiling. Keep-alive `Session` reuse, coherent stable headers,
  exponential backoff with jitter, and 429-aware handling (honours `Retry-After`, then auto-slows).
- Tunable request layer via an optional `http` block in `config.yaml` (`HttpConfig`): rate cap and
  its jitter, worker counts, timeout, and backoff.
  Absent block falls back to conservative defaults on the `HttpConfig` fields. Fully backward-compatible.
- `config.sample.yaml`: a committed, ready-to-copy template (`cp config.sample.yaml config.yaml`), since
  `config.yaml` itself is git-ignored.
- pydantic v2 config validation — `ConfigurationError` on missing keys, bad enum names, empty
  `search_queries`, or `rounds` < 1.
- Parallel job-description fetching (`description_workers`, default 3). The queries within a round
  also run bounded-parallel (`search_workers`, default 3) behind the shared rate cap.
- Dated logs: `logs/YYYY-MM-DD.log`, rotated at midnight, kept 10 days.
- A pytest suite (70 tests, no network, in-memory SQLite; the HTTP client is exercised with a mocked
  session). `uv run pytest`.
- `CHANGELOG.md`; provenance comment in `pyproject.toml`; example config, layout and tuning docs in README.

### Changed
- Dependency floors are loosened to the earliest versions that actually work on Python 3.14 — the
  compiled ones bound by 3.14 support (pydantic 2.12, SQLAlchemy 2.0.41, PyYAML 6.0.3, typer 0.20),
  the pure-Python ones far looser (rich 12.3.0, beautifulsoup4 4.12.0, requests 2.31.0, pycountry
  22.3.5, geonamescache 1.2.0) — instead of whatever was newest when each pin was written. Proven by
  running the suite with every direct dependency at its floor (`uv pip install --resolution
  lowest-direct`). Python 3.14+ stays: the code would run on 3.11 with two annotation fixes (made
  anyway — `-> Job`/`-> HttpClient` on methods of those classes only import under 3.14's lazy
  annotations, and are the more precise `-> Self` now), but nothing enforces the wider claim, so it
  would rot silently. The console log sink strips and re-adds each line's trailing newline instead of
  relying on rich 15 to keep it — under the old `rich>=13` floor, rich 13/14 rendered every log line
  glued to the previous one.
- The project is renamed to `linkedin_job_scraper`. The console script is now `linkedin-job-scraper`
  (was `linkedin-scraper`), the module runs as `python -m linkedin_job_scraper`, and the log-directory
  override reads `LINKEDIN_JOB_SCRAPER_LOG_DIR` (was `LINKEDIN_SCRAPER_LOG_DIR`) — update any cron job,
  shell profile, or scheduled task that pins the old names.
- The package is flat. `cli`, `config`, `filters`, `geo`, `job`, and `main` moved out of the deleted
  `app/` layer up to the `linkedin_scraper/` root, beside `constants` and `logger`; the `net`,
  `scrape`, and `store` subpackages moved up intact. Imports drop the meaningless `.app` qualifier.
  No behavior change.
- A search fans out into one variant per type in its `workplace_type` keep-list — all three when the
  list is empty — instead of always four (the three tagged types plus an unfiltered catch-all). The
  catch-all is no longer scraped: its only unique yield was jobs no tagged variant surfaced, and a
  full run's data showed those were truncation artifacts of the 1000-result cap, not a real fourth
  type — 0 of that run's relevant jobs came only from it. The keep-list is now the search plan as well
  as the relevance filter, so it also narrows what is fetched; for this config it cut search-page
  fetches ~82%. `untagged` is rejected as a `workplace_type` value (`WorkplaceType.UNTAGGED` stays —
  the session probe and canary use it, and legacy rows keep it, their never-downgrade upsert
  protection unchanged).
- A query is declared exhausted only after two consecutive confirming refetches of the empty page,
  not one. The endpoint serves flaky empty `200`s, and dropping the catch-all removed the backstop
  that used to re-surface a job its flaky-empty tagged variant missed, so the confirmation is
  stricter to compensate — one extra request per query ending.
- A blocked run exits 3 instead of 1, which now means a config or database error (argparse's usage
  errors stay 2). A cron job has nothing but the exit status to go on, so the retryable block gets
  a code of its own. Exit codes are documented in the README.
- The refresh phase reads its worklist back from the DB — the relevant rows due for a fetch, the same
  query behind the `refresh` command — instead of carrying this run's list in memory. The in-memory
  list was judged before `refresh_relevance` re-judged the table, so the two could disagree, and a row
  left undescribed by a blocked run or a failed fetch was retried only when a later search surfaced it
  again; now every stray is picked up on the next run. `main` and the `refresh` command share one
  fetch-and-store step (`fetch_postings`), and `JobsDb.described_keys` is gone, subsumed by the
  worklist query.
- HTTP 403 and LinkedIn's 999 authwall are retried like a 429 rather than treated as a verdict on
  the request. All three are temporary — the guest endpoint serves them to a caller it has soured
  on, and lifts them unprompted — so `TooManyRequests` is now `Throttled` and covers the three.
  Giving up on a 999 after one look ended each query the moment a block began, silently and for the
  rest of the run.
- The post-throttle slowdown repeats every third throttle instead of firing once. A run that gets
  rate-limited for three hours could previously halve its rate exactly once, then hold that rate
  however hard LinkedIn pushed back. The limiter now has a `max_interval` floor of one request a
  minute, so repeated halvings converge instead of pacing the run down to nothing.
- A blocked scrape raises `BlockedError` and exits non-zero instead of reporting success on a
  fraction of the jobs. Two things trip it. A query that gives up mid-page — five failed attempts
  is LinkedIn turning us away — ends the run even when other queries in the round succeeded, since
  the rounds still to come would only walk into the same wall; `scrape_query` now returns its
  outcome alongside its jobs so the caller can tell a give-up from a clean exhaustion. The subtler
  trip is an empty first page, which a block and a genuinely dry query produce alike: LinkedIn
  serves the blocked caller empty `200`s that parse exactly like a query run dry. Any round with an
  empty first page is now checked against a canary — the first query, broadened to drop every
  narrowing filter — whose keywords and location cannot honestly return nothing. Cards back means
  the empties are real and the run proceeds; an empty or failed canary means the block is real and
  the run ends. This catches a partial block, where early queries in a round return jobs and later
  ones are quietly walled, that a whole-round-empty check would miss. Either way the jobs of the
  rounds that did run are stored before the raise, and descriptions are skipped rather than fetched
  into the same wall.
- An omitted `title_include` now defaults to the terms of every query's `keywords`, deduped, with
  the operators and parentheses stripped. The list was a hand-copied flattening of that expression
  and the two had already drifted. LinkedIn matches `keywords` against the whole posting, so
  `title_include` is the only filter that reads the title — which is why it defaults to something
  rather than to "no filter". An explicit `[]` still disables it, and an explicit list still wins.
- The config is YAML (`config.yaml`), not JSON. A `keywords` expression runs to 200 characters and
  several queries share one; JSON has no way to say that once, so the string was pasted five times
  and drifted. An anchor (`&name`) and its aliases (`*name`) name it once. Anchors live under a
  top-level key the schema doesn't declare, which `extra="ignore"` drops. Adds `pyyaml`; the schema,
  the validators, and `load_and_validate_config` are untouched — only `load_config`'s parser changes.
  YAML types values implicitly, so a bare `location: NO` is the boolean `False`: quote a value that
  could read as a bool, a number, or a date.
- Every search now returns the jobs it skipped. `PAGE_SIZE` was 25, but the guest endpoint serves
  10 cards per request and reads `&start=` as an exact job index, not a page number — so paging by
  25 fetched jobs 0-9, then 25-34, then 50-59, silently dropping 15 of every 25. `PAGE_SIZE` is 10
  and `MAX_PAGES` 100, which walks `start` to 990 and consumes all 1000 records LinkedIn will serve.
  The old pair multiplied to the same 1000, so the arithmetic looked right while the run collected
  400 jobs at most. A recorded results page now pins `PAGE_SIZE` to the card count LinkedIn actually
  returns; the ceiling test asserts the last offset is 990 rather than restating the constants.
- `max_requests_per_minute` is now the rate you actually get. `HttpClient.get` called
  `limiter.acquire()` and *then* slept `uniform(min_delay, max_delay)` on every attempt, so the cap
  was a ceiling never reached: at 600 req/min it measured 211 with one worker and 462 with three.
  Retries paid that sleep on top of backoff too. The jitter now lives in the limiter's own interval,
  symmetric about the mean.
- `config.yaml` is read as UTF-8. The locale default is cp1252 on Windows, so a location like
  "München" mis-decoded or raised. A missing file, malformed YAML, and a bad encoding now all
  surface as `ConfigurationError`.
- A database that cannot be opened raises instead of logging and returning `None`, which had `main()`
  return early and the process exit `0`. `create_engine` is lazy, so the failure surfaces on first
  use; `__main__` catches `SQLAlchemyError` and exits non-zero.
- `keywords`/`location` are held as typed and encoded once in `page_url`, rather than URL-encoded
  in a validator that `label` then had to undo. Rebuilding a query from its own fields used to
  double-encode (`%20` → `%2520`).
- `parse_job_description` no longer takes `None`. `fetch_description` already guarded, so the branch
  was unreachable, but it let one function return either `None` or the not-found placeholder for a
  page that never loaded — the two cases the description-retry logic exists to tell apart.
- `fetch_description` returns a copy rather than writing into the job it was given: `main()` still
  holds those, and had already stored them.
- `settings.py` folded into `logger.py`. `remove_irrelevant_jobs` filters in one pass. Typing moved
  to PEP 585 builtins.
- `BASE_URL` is the LinkedIn host, and `SEARCH_URL`, `JOB_VIEW_URL` and `SEARCH_REFERER` are built
  from it. The host was written out three times: the search endpoint in `constants.py`, the posting
  URL in `parsing.py`, and the `Referer` header in `http.py`.
- Schema: `A_jobs_deduped`/`B_jobs_new`/`C_jobs_description` → a single `jobs_raw` table. B was a
  strict subset of A, C was B with one more column filled, and `job_description` was a dead column in
  both A and B. B and C were never read back. `job_description IS NOT NULL` and `first_seen` recover
  what the extra tables encoded. No migration: the old DB is discarded and rebuilt by the next run.
- The table is `jobs_raw`, pairing with the `jobs_filtered` view: raw as in unfiltered, since a job
  scraped twice is still one row.
- `db.py` is a `JobsDb` class owning one engine. Every function used to take a `table_name` for a
  database with one table, and `main()` threaded a `sqlite3.Connection` through five calls and never
  closed it. Both are gone. The ten column-name constants in `constants.py` went with them — they
  existed only to be interpolated into SQL.
- Column types read `VARCHAR`/`BOOLEAN` rather than `TEXT`/`INTEGER`, which SQLite gives the same
  affinity, and `location` is now `NOT NULL` — `Job.location` defaults to `""` and was never None.
  The unique index is SQLite's implicit `sqlite_autoindex_jobs_raw_1` rather than a named one.
- The DB moved from `data/my_database.db` to `linkedin_jobs.db` in the repository root. There is no migration;
  the next run builds it from scratch. Delete `my_database.db` by hand once you no longer want the
  old rows.
- `date_loaded` → `first_seen`, which is what it always meant, and which now pairs with `last_seen`.
- The seen-set read selects only the key columns rather than `SELECT *`, so the bulky description
  text is no longer loaded into memory on every run just to decide what to fetch.
- Python 3.11 → 3.14. pydantic → 2.13, requests → 2.34, bs4 → 4.15,
  loguru → 0.7.3, black → 26 (moved to a dev group).
- `get_with_retry` (module-level, per-request `requests.get`) → `HttpClient.get` (shared `Session`,
  global rate limiter). Retry timeout 5s → configurable 20s; flat 10s retry delay → exponential
  backoff with jitter.
- `USER_AGENTS` (bare strings) → `BROWSER_PROFILES` (a UA with the headers that belong to it). The
  header block was fixed while the UA rotated, so a Chrome UA went out with no `Sec-CH-UA` client
  hints — which real Chrome always sends, unprompted — and the Safari UA went out wearing Chrome's
  header set. Each profile now carries its own client hints (none, for Safari) and `Accept-Language`.
  Versions refreshed off two-year-old builds: Chrome 125 → 150, Safari 17.4 → 26.
- `Accept-Language` requests English (`en-US,en;q=0.9`) on every profile, so LinkedIn serves place
  names in English and `pycountry` reads `country` straight, with no localized-name map to maintain.
- `Accept-Encoding` is no longer hardcoded. It claimed `br`, but no Brotli decoder is installed, so
  any Brotli response would have soup'd compressed bytes into an empty page with no error. `requests`
  now derives the header from the decoders actually present (`gzip, deflate, zstd` — Python 3.14
  ships `compression.zstd`).
- `DESCRIPTION_WORKERS` (hardcoded 5 in `settings.py`) → `config.http.description_workers` (default 3).
- Config path decluttered: the 11 `HTTP_*` default constants moved off `settings.py` onto the
  `HttpConfig` fields directly (no more double declaration); `load_config(path)` now reads *and*
  validates in one call, so `main` no longer threads a raw dict and validation runs before the DB
  opens; search-page URL assembly moved onto `SearchQuery.page_url()` next to the encoding it depends
  on (the `25` page size is now `constants.PAGE_SIZE`).
- `scrape_jobs` no longer fans out over a precomputed list of every page URL; it drives one
  `scrape_query` pager per query, so a query can stop as soon as its results run out. Rounds now run
  sequentially (queries within a round still run in parallel), so the same URL is never in flight twice.
- `HttpClient.get` no longer retries a 4xx. A `400` never succeeds on retry, and spending five
  attempts plus backoff on one only burns requests against the rate limit.
- Restructured into a real package. Run with `python -m linkedin_scraper`, no longer `python main.py`.
- Core application logic moved into an `app/` subpackage (`main`, `http`, `parsing`, `jobs`, `db`,
  `config`); `settings.py`, `logging.py`, and `__main__.py` stay at the top level as infrastructure.
- `init_logging.py` → `logging.py`. `http_requests.py` → `requests.py` → `app/parsing.py`: the module
  is now parse-only and its old `requests` name no longer shadows the stdlib library. Networking and
  the `TooManyRequests` type live in `app/http.py`.
- Pure transforms split out of `main.py` into `filters.py`, so they are testable without a database.
  The module is named for what it does rather than for `jobs`, the noun every module handles.
  `first_seen` is declared on the `JobRow` model; `db.py` stamps it at insert.
- Console log level WARNING → INFO; INFO/SUCCESS lines previously never printed.
- `get_with_retry`: 19 retries × 10s with an unconditional pre-sleep → 5 retries, one polite delay
  after success. Returns `None` on give-up instead of falling off the end.
- HTML parsing is None-safe; malformed job cards are skipped instead of crashing the page. A card
  with no posting id is skipped too, with a warning: `job_url` is the job's identity and the only
  way to reach its description, so storing one would mean a row nothing could ever fill in. No such
  card is known to occur — the guard was inherited, never observed firing.
- `db.py`: hand-rolled type map + row-by-row inserts → one bulk statement; concat-based anti-join →
  a unique index; `create_table`/`update_table` → `JobsDb.create_schema`/`JobsDb.insert_jobs`.
- `remove_duplicates` is O(n) and order-preserving.
- `parse_job_description` no longer returns a trailing newline; stripping used to happen before the
  `Show less`/`Show more` text was removed.
- `main.py` holds the run and nothing else. The pagers — `scrape_query`, `scrape_jobs` and
  `fetch_description` — now live in `app/scraping.py` alongside the `http.py` that fetches the pages
  and the `parsing.py` that reads them; `tests/test_main.py` becomes `tests/test_scraping.py`, which
  is what every test in it already tested. `build_client` becomes `HttpClient.from_config`, next to
  the constructor it mirrors: every `HttpConfig` field exists to feed it, so forwarding them through
  `main` spelled each name a third time.
- The `Relevant this run` log line — and the run summary's `relevant` and `added` counts — now count
  only the jobs new to this run (`X of Y new`), not every relevant listing the search re-surfaced. The
  re-seen ones were fetched in earlier runs, so tallying them read as a mismatch against the smaller
  fetch worklist. `insert_jobs` returns the URLs it added, not a bare count, to key the tally on.

### Removed
- The `label` column on `queries`. It stored a human-readable rendering of each query, but every
  field it was built from already sits in the same row, so it duplicated data; the `SearchQuery.label`
  property still exists for log lines. It was dropped from the existing database by a one-off
  `ALTER TABLE`; new databases never get it.
- `scrape_jobs`'s `tagged=False` fallback, which scraped only the catch-all variants when no
  filtering session was drawn — and the catch-all harvest variant it depended on. The fallback
  harvested every job under a fabricated workplace label; a run with no filtering session now aborts
  with exit 4 instead (see the keep-list change above).
- The `rounds` config key, the round loop, and the `rounds` column on `runs`. Repeating every query
  blindly was a workaround for result variance that is now understood: LinkedIn deals each fresh
  session one of two sticky serving pipelines — one honors the `f_WT` workplace filter, one ignores
  it — so the variance was sessions landing on different pipelines, not LinkedIn shuffling results.
  A config still setting `rounds` keeps loading (`extra="ignore"`); the key no longer does anything.
  A database created while `runs` still had the column needs deleting (or the column dropped)
  before the next run.
- The `desc_words` config key. It was parsed and validated but never read — no code filtered on it.
  A leftover `desc_words` key is ignored rather than rejected (`extra="ignore"`), so it costs nothing
  to leave behind.
- The `pandas` dependency (and with it numpy and three transitive packages). It never performed a
  dataframe operation: it built frames only to take them apart again. Its one piece of real work,
  the dedupe `merge()`, is what the unique index replaced. `to_storable_frame`, `read_table_to_df`
  and `table_exists` go with it.
- `min_delay` / `max_delay`. Folded into `rate_jitter`. A config still setting them keeps
  loading — `extra="ignore"` — but the two keys no longer do anything. Lower
  `max_requests_per_minute` instead.
- `settings.py`. Six constants with one consumer, now at the top of `logging.py`.
- The `pages_to_scrape` query setting. Every query is now paged to exhaustion, so there is nothing
  to guess: too low silently truncated the results, too high spent requests on empty pages, and
  anything above 40 spent them on LinkedIn's 400. A leftover `pages_to_scrape` key is ignored rather
  than rejected, so it costs nothing to leave behind.
- The three CSV exports and the `data/` folder. Nothing ever read the CSVs back (`to_csv` × 3,
  `read_csv` × 0) and each run overwrote them with only that run's rows, so they were a stale
  partial snapshot of data the DB already held in full. Export from a SQLite client if needed.
- `helpers.py`, `exceptions.py` — folded into `config.py`, `filters.py`, and the parsing module.
- The `user-agent` dependency (`generate_navigator`). The HTTP client now uses a small curated pool
  of realistic desktop browser profiles, picking one stable profile per run.
- `safe_len`, `job_exists`, `create_table`, `update_table`.
- `find_new_jobs`. The description phase asks the DB which relevant jobs lack a description
  (`JobsDb.described_keys`) instead of which jobs are unseen, which subsumes it.
- `vpn.py` — NordVPN rotation, never wired up.
- `digest.txt` — purged from git history.
- The explicit `AUTOINCREMENT id` column on *newly created* tables; SQLite's rowid is used instead.
  Tables created before this change keep their `id` column and still append correctly.

### Notes
- Released under the MIT License (`LICENSE`) as the author's own work; the last routine carried over
  from upstream, a description parser, was reimplemented first.
