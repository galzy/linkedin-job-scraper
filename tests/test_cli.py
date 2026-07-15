import os
import subprocess
import sys
from pathlib import Path

import pytest

from linkedin_scraper.constants import MAX_PAGES
from linkedin_scraper.logger import LOG_DIR_ENV

PROJECT_ROOT = Path(__file__).parent.parent

INVALID = """\
search_queries: []
"""


def run_cli(*args, log_dir=None):
    """Run the module as a user would, with the log dir pointed away from the project's real logs/."""
    env = os.environ.copy()
    if log_dir is not None:
        env[LOG_DIR_ENV] = str(log_dir)
    return subprocess.run(
        [sys.executable, "-m", "linkedin_scraper", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def scrape(*args, log_dir=None):
    """Run the ``scrape`` subcommand. Every config here fails to load, so nothing is scraped."""
    return run_cli("scrape", *args, log_dir=log_dir)


def test_the_config_path_argument_is_the_config_that_gets_loaded(tmp_path):
    absent = tmp_path / "absent.yaml"

    result = scrape(str(absent), log_dir=tmp_path)

    assert result.returncode == 1
    assert f"Starting scrape with {absent}" in result.stdout  # logged before the load, so a bad one is still named
    assert "Could not load config" in result.stdout


def test_an_invalid_config_named_on_the_command_line_exits_non_zero(tmp_path):
    config = tmp_path / "invalid.yaml"
    config.write_text(INVALID, encoding="utf-8")

    result = scrape(str(config), log_dir=tmp_path)

    assert result.returncode == 1
    assert "search_queries must contain at least one query" in result.stdout


@pytest.mark.parametrize("pages", ["0", str(MAX_PAGES + 1), "not-a-number"])
def test_a_page_cap_outside_the_range_linkedin_serves_is_refused(pages):
    """Typer rejects it before any scraping, with exit 2 — not the 1 a bad config gives."""
    result = scrape("--max-pages", pages)

    assert result.returncode == 2
    assert "--max-pages" in result.stderr


def test_no_subcommand_is_an_error():
    """The verb is explicit: a bare invocation names the choices and exits non-zero."""
    result = run_cli()

    assert result.returncode == 2
    assert "scrape" in result.stdout  # Typer lists the commands on a bare call


SAMPLE = PROJECT_ROOT / "configs" / "config.sample.yaml"


@pytest.mark.parametrize(
    ("command", "args"),
    [("recompute", [SAMPLE]), ("refresh", [SAMPLE]), ("status", []), ("prune", [90])],
)
def test_stored_job_commands_bail_without_a_db(command, args, tmp_path, monkeypatch):
    """With no database, they warn and return rather than creating an empty one."""
    from linkedin_scraper import cli

    db = tmp_path / "linkedin_jobs.db"
    monkeypatch.setattr(cli, "DB_PATH", db)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))

    getattr(cli, command)(*args)

    assert not db.exists()


@pytest.mark.parametrize("days", ["0", "not-a-number"])
def test_prune_rejects_a_non_positive_day_count(days):
    """Typer refuses it with exit 2 before any handler runs, naming the offending argument."""
    result = run_cli("prune", days)

    assert result.returncode == 2
    assert "days" in result.stderr


def test_init_config_writes_a_starter_and_refuses_to_overwrite(tmp_path):
    dest = tmp_path / "new.yaml"

    written = run_cli("init-config", str(dest))
    assert written.returncode == 0
    assert dest.read_text(encoding="utf-8") == SAMPLE.read_text(encoding="utf-8")

    again = run_cli("init-config", str(dest))
    assert again.returncode == 1
    assert "refusing to overwrite" in again.stderr


def test_a_blocked_run_exits_3_so_cron_can_tell_it_from_a_real_failure(monkeypatch):
    from linkedin_scraper import cli

    def blocked(config, max_pages):
        raise cli.BlockedError("blocked mid-run")

    monkeypatch.setattr(cli, "run_scrape", blocked)
    monkeypatch.setattr(sys, "argv", ["linkedin_scraper", "scrape", str(SAMPLE)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 3


def test_a_run_without_a_filtering_session_exits_4(monkeypatch):
    from linkedin_scraper import cli

    def no_session(config, max_pages):
        raise cli.NoFilteringSessionError("no filtering session")

    monkeypatch.setattr(cli, "run_scrape", no_session)
    monkeypatch.setattr(sys, "argv", ["linkedin_scraper", "scrape", str(SAMPLE)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 4


def test_status_prints_the_last_run_and_the_stored_totals(tmp_path, monkeypatch, capsys):
    from linkedin_scraper import cli
    from linkedin_scraper.job import Job
    from linkedin_scraper.store.db import JobsDb

    db_path = tmp_path / "linkedin_jobs.db"
    db = JobsDb(path=str(db_path))
    db.create_schema()
    db.insert_jobs(
        [
            Job(title="Engineer", company="ACME", date="2024-01-01", job_url="https://x/1/"),
            Job(title="Chef", company="ACME", date="2024-01-01", job_url="https://x/2/"),
        ]
    )
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")
    db.record_run(
        started_at="2024-01-01 09:00:00",
        finished_at="2024-01-01 09:05:00",
        status="completed",
        counts={"scraped": 2, "deduped": 2, "relevant": 1, "added": 2, "flipped": 0},
        config_yaml="",
        query_ids=[],
    )
    db.close()
    monkeypatch.setattr(cli, "DB_PATH", db_path)

    cli.status()

    out = capsys.readouterr().out
    assert "Last run: 2024-01-01 09:00:00 -> 2024-01-01 09:05:00 (completed)" in out
    assert "scraped 2, deduped 2, relevant 1, added 2, flipped 0" in out
    assert "Stored: 2 jobs, 1 relevant, 1 missing descriptions" in out


def test_status_before_any_recorded_run_still_reports_totals(tmp_path, monkeypatch, capsys):
    from linkedin_scraper import cli
    from linkedin_scraper.store.db import JobsDb

    db_path = tmp_path / "linkedin_jobs.db"
    db = JobsDb(path=str(db_path))
    db.create_schema()
    db.close()
    monkeypatch.setattr(cli, "DB_PATH", db_path)

    cli.status()

    out = capsys.readouterr().out
    assert "No runs recorded yet" in out
    assert "Stored: 0 jobs, 0 relevant, 0 missing descriptions" in out


def _stored_count(db_path):
    from linkedin_scraper.store.db import JobsDb

    db = JobsDb(path=str(db_path))
    total = db.totals()["stored"]
    db.close()
    return total


def _seed_one_relevant_one_irrelevant(db_path):
    """Two old jobs: a relevant Engineer (kept) and an irrelevant Chef (prunable)."""
    from linkedin_scraper.job import Job
    from linkedin_scraper.store.db import JobsDb

    db = JobsDb(path=str(db_path))
    db.create_schema()
    db.insert_jobs(
        [
            Job(title="Engineer", company="ACME", date="2020-01-01", job_url="https://x/1/"),
            Job(title="Chef", company="ACME", date="2020-01-01", job_url="https://x/2/"),
        ]
    )
    db.refresh_relevance(lambda title, company, workplace, qids: title == "Engineer")
    db.close()


def test_prune_deletes_matching_rows_after_both_confirmations(tmp_path, monkeypatch):
    from linkedin_scraper import cli

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["yes", "1"])  # one prunable row: the irrelevant Chef
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    cli.prune(365)  # cutoff lands in 2025; the 2020-dated rows are older

    assert _stored_count(db_path) == 1  # only the relevant Engineer survives


def test_prune_a_wrong_second_confirmation_deletes_nothing(tmp_path, monkeypatch, capsys):
    from linkedin_scraper import cli

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["yes", "nope"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    cli.prune(365)  # cutoff lands in 2025; the 2020-dated rows are older

    assert "Aborted; nothing was deleted" in capsys.readouterr().out
    assert _stored_count(db_path) == 2


def test_prune_without_matching_rows_never_prompts(tmp_path, monkeypatch):
    from linkedin_scraper import cli
    from linkedin_scraper.job import Job
    from linkedin_scraper.store.db import JobsDb

    db_path = tmp_path / "linkedin_jobs.db"
    db = JobsDb(path=str(db_path))
    db.create_schema()
    db.insert_jobs([Job(title="Engineer", company="ACME", date="2020-01-01", job_url="https://x/1/")])
    db.refresh_relevance(lambda title, company, workplace, qids: True)  # relevant and open -> not prunable
    db.close()
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt when nothing matches"))

    cli.prune(3650)

    assert _stored_count(db_path) == 1
