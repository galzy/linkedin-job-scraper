import os
import subprocess
import sys
from pathlib import Path

import pytest

from linkedin_job_scraper.constants import MAX_PAGES
from linkedin_job_scraper.logger import LOG_DIR_ENV

PROJECT_ROOT = Path(__file__).parent.parent

INVALID = """\
search_queries: []
"""


def run_cli(*args, log_dir=None):
    """Run the module as a user would, with the log dir pointed away from the project's real logs/."""
    env = os.environ.copy()
    env["TERM"] = "dumb"  # keeps Rich's color off: it splits flag names with escape codes, breaking text asserts
    env["PYTHONIOENCODING"] = "utf-8"  # the child would otherwise write the console codepage, which we decode as utf-8
    if log_dir is not None:
        env[LOG_DIR_ENV] = str(log_dir)
    return subprocess.run(
        [sys.executable, "-m", "linkedin_job_scraper", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # Typer's error boxes break the locale codepage (e.g. cp1255)
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
    [("recompute", [SAMPLE]), ("refresh", [SAMPLE]), ("status", []), ("export", []), ("prune", [90])],
)
def test_stored_job_commands_bail_without_a_db(command, args, tmp_path, monkeypatch):
    """With no database, they warn and return rather than creating an empty one."""
    from linkedin_job_scraper import cli

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
    from linkedin_job_scraper import cli

    def blocked(config, max_pages):
        raise cli.BlockedError("blocked mid-run")

    monkeypatch.setattr(cli, "run_scrape", blocked)
    monkeypatch.setattr(sys, "argv", ["linkedin_job_scraper", "scrape", str(SAMPLE)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 3


def test_a_run_without_a_filtering_session_exits_4(monkeypatch):
    from linkedin_job_scraper import cli

    def no_session(config, max_pages):
        raise cli.NoFilteringSessionError("no filtering session")

    monkeypatch.setattr(cli, "run_scrape", no_session)
    monkeypatch.setattr(sys, "argv", ["linkedin_job_scraper", "scrape", str(SAMPLE)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 4


def test_an_unexpected_error_exits_5_rather_than_dumping_a_traceback(monkeypatch):
    from linkedin_job_scraper import cli

    def boom(config, max_pages):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(cli, "run_scrape", boom)
    monkeypatch.setattr(sys, "argv", ["linkedin_job_scraper", "scrape", str(SAMPLE)])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 5


def test_init_logging_falls_back_to_console_when_the_log_dir_is_unwritable(tmp_path, monkeypatch):
    """An unwritable log dir must degrade to console logging, not crash the run before any work."""
    from linkedin_job_scraper.logger import init_logging

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv(LOG_DIR_ENV, str(blocker / "logs"))  # a directory cannot live under a file

    init_logging()  # must not raise


def test_status_prints_the_last_run_and_the_stored_totals(tmp_path, monkeypatch, capsys):
    from linkedin_job_scraper import cli
    from linkedin_job_scraper.job import Job
    from linkedin_job_scraper.store.db import JobsDb

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
    assert "Stored: 2 jobs, 1 relevant, 1 missing descriptions, 1 unjudged" in out


def test_status_before_any_recorded_run_still_reports_totals(tmp_path, monkeypatch, capsys):
    from linkedin_job_scraper import cli
    from linkedin_job_scraper.store.db import JobsDb

    db_path = tmp_path / "linkedin_jobs.db"
    db = JobsDb(path=str(db_path))
    db.create_schema()
    db.close()
    monkeypatch.setattr(cli, "DB_PATH", db_path)

    cli.status()

    out = capsys.readouterr().out
    assert "No runs recorded yet" in out
    assert "Stored: 0 jobs, 0 relevant, 0 missing descriptions, 0 unjudged" in out


def _read_csv(path):
    import csv

    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_export_writes_the_kept_jobs_to_csv(tmp_path, monkeypatch):
    from linkedin_job_scraper import cli
    from linkedin_job_scraper.store.schema import JobRow

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)  # a relevant Engineer (kept), an irrelevant Chef
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    dest = tmp_path / "jobs.csv"

    cli.export(dest)

    header, *data = _read_csv(dest)
    assert header == list(JobRow.__table__.columns.keys())  # every column, in schema order
    titles = {row[header.index("title")] for row in data}
    assert titles == {"Engineer"}  # the kept set only: the irrelevant Chef is absent


def test_export_all_includes_the_rejected_rows_and_no_descriptions_drops_the_column(tmp_path, monkeypatch):
    from linkedin_job_scraper import cli

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    dest = tmp_path / "jobs.csv"

    cli.export(dest, all_rows=True, descriptions=False)

    header, *data = _read_csv(dest)
    assert "job_description" not in header
    titles = {row[header.index("title")] for row in data}
    assert titles == {"Engineer", "Chef"}  # --all keeps the irrelevant row too


def test_export_normalizes_unicode_line_separators(tmp_path, monkeypatch):
    """U+2028/U+2029 in a description become plain newlines, so no consumer mis-splits the row."""
    from linkedin_job_scraper import cli
    from linkedin_job_scraper.job import Job
    from linkedin_job_scraper.store.db import JobsDb

    db_path = tmp_path / "linkedin_jobs.db"
    db = JobsDb(path=str(db_path))
    db.create_schema()
    seen = Job(title="Engineer", company="ACME", date="2024-01-01", job_url="https://x/1/")
    db.insert_jobs([seen])
    db.refresh_relevance(lambda title, company, workplace, qids: True)
    db.record_postings([seen.with_posting("line1" + chr(0x2028) + "line2", True)])
    db.close()
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    dest = tmp_path / "jobs.csv"

    cli.export(dest)

    assert chr(0x2028) not in dest.read_text(encoding="utf-8-sig")
    header, *data = _read_csv(dest)
    assert data[0][header.index("job_description")] == "line1\nline2"  # the break survives, as a normal newline


def test_export_failure_leaves_the_previous_file_intact_and_exits_1(tmp_path, monkeypatch, capsys):
    """A failed swap (e.g. the target open in Excel) never clobbers a good export; it exits 1 with a hint."""
    from linkedin_job_scraper import cli

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    dest = tmp_path / "jobs.csv"
    dest.write_text("PRIOR EXPORT", encoding="utf-8")

    def locked(*args, **kwargs):
        raise PermissionError("the file is open in another program")

    monkeypatch.setattr(cli.os, "replace", locked)  # the atomic swap fails

    with pytest.raises(SystemExit) as excinfo:
        cli.export(dest)

    assert excinfo.value.code == 1
    assert dest.read_text(encoding="utf-8") == "PRIOR EXPORT"  # the good export is untouched
    assert not list(tmp_path.glob("jobs.csv.tmp"))  # the temp sibling is cleaned up
    assert "close it and retry" in capsys.readouterr().err  # an actionable message, not a traceback


def test_export_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    """A successful export swaps the temp in and leaves only the CSV, never a stray .tmp."""
    from linkedin_job_scraper import cli

    db_path = tmp_path / "linkedin_jobs.db"
    _seed_one_relevant_one_irrelevant(db_path)
    monkeypatch.setattr(cli, "DB_PATH", db_path)
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path / "logs"))
    dest = tmp_path / "jobs.csv"

    cli.export(dest)

    assert dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def _stored_count(db_path):
    from linkedin_job_scraper.store.db import JobsDb

    db = JobsDb(path=str(db_path))
    total = db.totals()["stored"]
    db.close()
    return total


def _seed_one_relevant_one_irrelevant(db_path):
    """Two old jobs: a relevant Engineer (kept) and an irrelevant Chef (prunable)."""
    from linkedin_job_scraper.job import Job
    from linkedin_job_scraper.store.db import JobsDb

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
    from linkedin_job_scraper import cli

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
    from linkedin_job_scraper import cli

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
    from linkedin_job_scraper import cli
    from linkedin_job_scraper.job import Job
    from linkedin_job_scraper.store.db import JobsDb

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
