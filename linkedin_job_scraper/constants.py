from pathlib import Path

NO_DESCRIPTION = "Could not find job description"  # an answer, unlike the NULL a failed fetch leaves

# --- Paths -------------------------------------------------------------------
# Anchored to the repo root, not the working directory: cron and systemd start elsewhere.
PROJECT_ROOT = Path(__file__).parent.parent
CONFIGS_PATH = PROJECT_ROOT / "configs"  # every config file; git-ignored bar the sample
CONFIG_PATH = CONFIGS_PATH / "config.yaml"
DB_PATH = PROJECT_ROOT / "linkedin_jobs.db"
LOGS_PATH = PROJECT_ROOT / "logs"
REPORTS_PATH = PROJECT_ROOT / "reports"

# --- LinkedIn endpoints ------------------------------------------------------
BASE_URL = "https://www.linkedin.com"
SEARCH_URL = f"{BASE_URL}/jobs-guest/jobs/api/seeMoreJobPostings/search"
JOB_VIEW_URL = f"{BASE_URL}/jobs/view"  # one posting, by its id
JOB_POSTING_URL = f"{BASE_URL}/jobs-guest/jobs/api/jobPosting"  # the description; /jobs/view 999s a guest
SEARCH_REFERER = f"{BASE_URL}/jobs/search/"
PAGE_SIZE = 10  # 10 is the max LinkedIn serves per request; &start= is an exact job index
MAX_PAGES = 100  # start >= 1000 always 400s; page 99 (start=990) is the last that exists
SESSION_DRAWS = 10  # tries at drawing a filtering session; at the observed ~50% odds, 10 misses are ~0.1%
RECHECK_DAYS = 3  # leave a posting alone this long before re-checking that it is still open

# --- SQLite ------------------------------------------------------------------
TABLE_JOBS_RAW = "jobs_raw"  # every scraped job, filtered or not; duplicates counted, not stored
VIEW_JOBS_FILTERED = "jobs_filtered"  # the jobs the config's filters keep
TABLE_QUERIES = "queries"  # the distinct search queries ever run, keyed by content hash
TABLE_JOB_QUERIES = "job_queries"  # which query found which job, with sighting counts
TABLE_RUNS = "runs"  # one row per run: timestamps, counts, and the config used
TABLE_RUN_QUERIES = "run_queries"  # which queries each run used
TABLE_SCRAPE_STAGING = "scrape_staging"  # this run's raw cards, flushed per query; cleared once promoted
