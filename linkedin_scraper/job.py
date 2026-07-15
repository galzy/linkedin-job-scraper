from pydantic import BaseModel, ConfigDict

from linkedin_scraper.constants import JOB_POSTING_URL


class Job(BaseModel):
    """One job posting as scraped. ``job_description`` is None until the posting page is fetched.

    Its fields are the scraped columns of ``db.JobRow``; the city/region/country columns are derived
    from ``location`` by ``db._row``, so they cannot drift from the string they read.

    Frozen, so a job already handed to the database cannot be written through: attach a
    description with :meth:`with_description`, which returns a copy.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    company: str
    date: str
    job_url: str
    location: str = ""
    job_description: str | None = None
    workplace_type: str = "untagged"  # on_site / remote / hybrid / untagged, inferred across queries

    @property
    def key(self) -> str:
        """Identity in the ``jobs_raw`` table, whose primary key is ``job_url``."""
        return self.job_url

    @property
    def description_url(self) -> str:
        """Where the description is fetched from: the guest API, keyed on the posting id."""
        return f"{JOB_POSTING_URL}/{self.job_url.rstrip('/').rsplit('/', 1)[-1]}"

    def with_description(self, description: str | None) -> Job:
        return self.model_copy(update={"job_description": description})

    def with_workplace_type(self, workplace_type: str) -> Job:
        return self.model_copy(update={"workplace_type": workplace_type})
