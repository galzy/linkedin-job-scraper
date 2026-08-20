import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linkedin_job_scraper import fit
from linkedin_job_scraper.cli import _export_fit_days
from linkedin_job_scraper.constants import NO_DESCRIPTION
from linkedin_job_scraper.fit import FitJudgeError, _ad, _parse, judge_batches
from linkedin_job_scraper.job import Job
from linkedin_job_scraper.store.db import JobsDb
from linkedin_job_scraper.verdicts import PASS


def row(jid="101", description="We build backend services in Python.", **overrides):
    fields = dict(
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        title="Engineer",
        company="ACME",
        location="Bologna",
        country="Italy",
        workplace_type="remote",
        date="2026-08-01",
        description_lang="en",
        stated_locations=None,
        work_eligibility=None,
        dup_count=0,
        job_description=description,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_an_ad_carries_its_jid_hits_and_a_capped_description():
    text = _ad(row(description="Salary €30k RAL.\n" + "x" * 5000))
    assert "### jid 101" in text
    assert "salary hits: Salary €30k RAL." in text
    assert len(text) < 4500


def test_a_missing_description_is_said_not_faked():
    assert "no description available" in _ad(row(description=None))
    assert "no description available" in _ad(row(description=NO_DESCRIPTION))


def test_parse_reads_the_object_out_of_fences_and_prose():
    reply = 'Here you go:\n```json\n{"101": "b: fluent German required", "102": "ok"}\n```'
    assert _parse(reply, {"101", "102"}) == {"101": "b: fluent German required", "102": "ok"}


def test_parse_fails_when_a_jid_is_missing():
    with pytest.raises(FitJudgeError, match="missing"):
        _parse('{"101": "ok"}', {"101", "102"})


def test_parse_fails_on_a_malformed_verdict():
    with pytest.raises(FitJudgeError, match="malformed"):
        _parse('{"101": "sounds nice"}', {"101"})


def test_parse_fails_on_an_empty_verdict():
    """Silence is what a judge that lost track returns; it must not import as a clean bill."""
    with pytest.raises(FitJudgeError, match="malformed"):
        _parse('{"101": ""}', {"101"})


def test_parse_fails_without_json():
    with pytest.raises(FitJudgeError, match="no JSON"):
        _parse("I could not judge these.", {"101"})


def test_judge_batches_retries_a_bad_reply_once(monkeypatch):
    replies = iter(["not json at all", '{"101": "g?: java-leaning ad"}'])
    monkeypatch.setattr(fit, "_ask", lambda prompt, claude, model: next(replies))

    batches = list(judge_batches([row()], rubric="rubric", claude="claude"))
    assert batches == [{"https://www.linkedin.com/jobs/view/101/": "g?: java-leaning ad"}]


def test_judge_batches_gives_up_after_two_failures(monkeypatch):
    monkeypatch.setattr(fit, "_ask", lambda prompt, claude, model: "still not json")

    with pytest.raises(FitJudgeError, match="failed twice"):
        list(judge_batches([row()], rubric="rubric", claude="claude"))


def test_a_failing_batch_keeps_what_earlier_batches_won(monkeypatch):
    """13 rows are two batches; the second dying must not take the first down with it."""
    replies = iter([json.dumps({str(n): PASS for n in range(100, 112)}), "boom", "boom"])
    monkeypatch.setattr(fit, "_ask", lambda prompt, claude, model: next(replies))

    won = []
    with pytest.raises(FitJudgeError):
        for verdicts in judge_batches([row(jid=str(n)) for n in range(100, 113)], rubric="rubric", claude="claude"):
            won.append(verdicts)
    assert len(won) == 1 and len(won[0]) == 12


def test_a_rubric_with_braces_survives_prompt_building(monkeypatch):
    captured = {}

    def ask(prompt, claude, model):
        captured["prompt"] = prompt
        return '{"101": "ok"}'

    monkeypatch.setattr(fit, "_ask", ask)
    list(judge_batches([row()], rubric="writing `{jid: verdict}` JSON", claude="claude"))
    assert "{jid: verdict}" in captured["prompt"]


# --- the per-day export ------------------------------------------------------


def day_db(*jobs_by_day, judge=None):
    """A DB whose rows landed on given days: each argument is (day, [Job]) in insert order."""
    db = JobsDb(":memory:")
    db.create_schema()
    for day, jobs in jobs_by_day:
        with patch("linkedin_job_scraper.store.db.datetime") as dt:
            dt.now.return_value.isoformat.return_value = f"{day} 09:00:00"
            db.insert_jobs(jobs)
    db.refresh_relevance(lambda title, company, workplace, lang, qids: True)
    if judge:
        db.import_verdicts(judge)
    return db


def a_job(url="https://x/1/", title="Engineer"):
    return Job(title=title, company="ACME", date="2024-01-01", job_url=url)


def test_export_writes_a_days_survivors_but_not_todays_file(tmp_path):
    db = day_db(
        ("2024-01-01", [a_job(), a_job(url="https://x/2/", title="Other")]),
        judge={"https://x/1/": "c?: UK listing", "https://x/2/": "g: Java backend"},
    )
    _export_fit_days(db, tmp_path, ["2024-01-01", "2024-01-02"], export_today=False)
    db.close()

    exported = (tmp_path / "new-jobs-2024-01-01.csv").read_text(encoding="utf-8-sig").splitlines()
    assert len(exported) == 2  # the header and the "?" row; the firm rejection stays out
    assert exported[1].startswith("https://x/1/")
    assert not (tmp_path / "new-jobs-2024-01-02.csv").exists()  # today waits for --export-today


def test_a_day_still_carrying_unjudged_rows_is_not_exported(tmp_path):
    db = day_db(
        ("2024-01-01", [a_job(), a_job(url="https://x/2/", title="Other")]),
        judge={"https://x/1/": PASS},
    )
    _export_fit_days(db, tmp_path, ["2024-01-01"], export_today=True)
    db.close()

    assert not (tmp_path / "new-jobs-2024-01-01.csv").exists()


def test_a_days_file_is_written_once(tmp_path):
    db = day_db(("2024-01-01", [a_job()]), judge={"https://x/1/": PASS})
    (tmp_path / "new-jobs-2024-01-01.csv").write_text("sentinel", encoding="utf-8")
    _export_fit_days(db, tmp_path, ["2024-01-01"], export_today=True)
    db.close()

    assert (tmp_path / "new-jobs-2024-01-01.csv").read_text(encoding="utf-8") == "sentinel"
