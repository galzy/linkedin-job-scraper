from typing import Self

from pydantic import BaseModel, ConfigDict

from linkedin_job_scraper.constants import JOB_POSTING_URL


class Job(BaseModel):
    """One job posting as scraped. ``job_description`` and ``is_open`` are None until the posting page is fetched.

    Its fields are the scraped columns of ``schema.JobRow``; the city/region/country columns are derived
    from ``location`` by ``statements._row``, so they cannot drift from the string they read.

    Frozen, so a job already handed to the database cannot be written through: attach what the posting
    page yielded with :meth:`with_posting`, which returns a copy.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    company: str
    date: str
    job_url: str
    location: str = ""
    job_description: str | None = None
    is_open: bool | None = None  # None until the posting page is fetched; False once it stops accepting applications
    workplace_type: str = "untagged"  # on_site / remote / hybrid / untagged, inferred across queries

    @property
    def key(self) -> str:
        """Identity in the ``jobs_raw`` table, whose primary key is ``job_url``."""
        return self.job_url

    @property
    def description_url(self) -> str:
        """Where the posting page — description and open-status — is fetched from: the guest API, by posting id."""
        return f"{JOB_POSTING_URL}/{self.job_url.rstrip('/').rsplit('/', 1)[-1]}"

    def with_posting(self, description: str | None, is_open: bool | None) -> Self:
        """A copy carrying what one fetch of the posting page yielded: its description and open-status."""
        return self.model_copy(update={"job_description": description, "is_open": is_open})

    def with_workplace_type(self, workplace_type: str) -> Self:
        return self.model_copy(update={"workplace_type": workplace_type})
