import hashlib
import re
from enum import Enum, StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Self
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from linkedin_job_scraper.constants import PAGE_SIZE, SEARCH_URL


class ConfigurationError(Exception):
    """Raised when config.yaml is missing keys, or holds values the scraper can't use."""


class TimeFilterType(StrEnum):
    ANY = ""
    DAY = "r86400"
    WEEK = "r604800"
    MONTH = "r2592000"


class WorkplaceType(StrEnum):
    ON_SITE = "on_site"
    REMOTE = "remote"
    HYBRID = "hybrid"
    UNTAGGED = "untagged"  # never harvested; kept for the session probe and the canary


# LinkedIn's f_WT search code per type. UNTAGGED has none — its variant is the unfiltered search.
_F_WT = {WorkplaceType.ON_SITE: "1", WorkplaceType.REMOTE: "2", WorkplaceType.HYBRID: "3"}


class DistanceType(StrEnum):
    ANY = ""
    KM_0 = "0"
    KM_8 = "5"
    KM_16 = "10"
    KM_40 = "25"
    KM_80 = "50"
    KM_160 = "100"


def _map_option(enum_cls: type[Enum], value: str | list[str] | None) -> str:
    """Map an enum member name (or list of names) to its LinkedIn query value(s)."""
    if value is None:
        return ""
    names = value if isinstance(value, list) else [value]
    try:
        return ",".join(enum_cls[name].value for name in names)
    except KeyError as e:
        valid = ", ".join(enum_cls.__members__)
        raise ValueError(f"invalid {enum_cls.__name__} value {e}; valid options: {valid}") from e


_KEYWORD_TOKEN = re.compile(r'"[^"]*"|[^\s()]+')  # a quoted phrase is one term; parens are separators
_KEYWORD_LEXER = re.compile(r'"[^"]*"|[()]|[^\s()]+')  # like _KEYWORD_TOKEN, but keeps the parens
_OPERATORS = {"AND", "OR", "NOT"}


def _check_keywords_syntax(expression: str) -> None:
    """Raise on boolean syntax LinkedIn cannot parse: dangling operators, bad parens, no terms."""
    tokens = _KEYWORD_LEXER.findall(expression)
    kinds = [t if t in "()" else "op" if t.upper() in _OPERATORS else "term" for t in tokens]
    if "term" not in kinds:
        raise ValueError("keywords hold no search terms")

    depth = 0
    for kind in kinds:
        depth += (kind == "(") - (kind == ")")
        if depth < 0:
            raise ValueError('")" without a matching "("')
    if depth:
        raise ValueError('unclosed "("')

    # Wrapped in one outer group, every remaining misplacement is a bad neighbor pair.
    for left, right in pairwise(["(", *kinds, ")"]):
        if (left, right) == ("(", ")"):
            raise ValueError('empty "()" group')
        if left in ("(", "op") and right == "op":
            raise ValueError("an operator with no term before it")
        if left == "op" and right == ")":
            raise ValueError("an operator with no term after it")


def _keyword_atoms(expression: str) -> list[str]:
    """The lowercased search terms of a boolean keywords expression, without its operators or parens.

    ``NOT`` is rejected rather than dropped: flattening ``a NOT b`` to ``[a, b]`` would turn an
    exclusion into a way in.
    """
    tokens = _KEYWORD_TOKEN.findall(expression)
    if any(token.upper() == "NOT" for token in tokens):
        raise ValueError("a keywords expression using NOT cannot derive title_include; set it explicitly")
    atoms = (token.strip('"').lower() for token in tokens if token.upper() not in {"AND", "OR"})
    return [atom for atom in atoms if atom]


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keywords: str
    location: str
    distance: str = ""
    timespan: str = ""
    workplace_type: list[str] = []  # keep-list: the variants harvested and the types kept; empty means all three
    harvest_type: str = WorkplaceType.UNTAGGED.value  # the type this variant searches; set by harvest_variants

    @field_validator("keywords")
    @classmethod
    def _valid_boolean(cls, v: str) -> str:
        """Reject a boolean expression LinkedIn cannot parse."""
        _check_keywords_syntax(v)
        return v

    @field_validator("distance", mode="before")
    @classmethod
    def _map_distance(cls, v):
        """Turn a DistanceType name into its LinkedIn query value."""
        return _map_option(DistanceType, v)

    @field_validator("timespan", mode="before")
    @classmethod
    def _map_timespan(cls, v):
        """Turn a TimeFilterType name into its LinkedIn query value."""
        return _map_option(TimeFilterType, v)

    @field_validator("workplace_type", mode="before")
    @classmethod
    def _normalize_workplace_type(cls, v):
        """Normalize each keep-list entry to a tagged WorkplaceType token, case-insensitively.

        The keep-list's domain is the three tagged types; untagged is harvest-only and rejected here.
        """
        if not v:
            return []
        tokens = [item.lower() for item in v]
        keepable = [t.value for t in _F_WT]
        invalid = [t for t in tokens if t not in keepable]
        if invalid:
            raise ValueError(f"invalid workplace_type {invalid}; valid options: {', '.join(keepable)}")
        return tokens

    @property
    def label(self) -> str:
        """This query as a human would write it, for log lines; tags the type a variant searches."""
        base = f"{self.keywords} @ {self.location}"
        return base if self.harvest_type == WorkplaceType.UNTAGGED else f"{base} [{self.harvest_type}]"

    @property
    def query_id(self) -> str:
        """A stable id for this query, the same across runs and config edits.

        Hashes the five LinkedIn-normalized filter fields, so two configs spelling the same
        search share one id in the queries table.
        """
        fields = "\n".join([self.keywords, self.location, self.distance, self.harvest_type, self.timespan])
        return hashlib.sha1(fields.encode("utf-8")).hexdigest()[:12]

    def page_url(self, page: int) -> str:
        """The results-page URL for this query's zero-indexed page ``page``."""
        f_wt = _F_WT.get(WorkplaceType(self.harvest_type), "")  # UNTAGGED searches unfiltered
        distance = f"&distance={self.distance}" if self.distance else ""  # empty distance= now 500s the endpoint
        return (
            f"{SEARCH_URL}?keywords={quote(self.keywords)}{distance}"
            f"&location={quote(self.location)}&f_WT={f_wt}"
            f"&geoId=&f_TPR={self.timespan}&start={PAGE_SIZE * page}"
        )

    def broadened(self) -> Self:
        """The same keywords and location with every narrowing filter dropped: the widest this gets.

        Used as a canary — a query this broad cannot honestly return nothing.
        """
        return self.model_copy(update={"distance": "", "harvest_type": WorkplaceType.UNTAGGED.value, "timespan": ""})

    def harvest_variants(self) -> list[Self]:
        """One copy of this query per keep-list workplace type; an empty keep-list means all three."""
        types = self.workplace_type or [t.value for t in _F_WT]
        return [self.model_copy(update={"harvest_type": t}) for t in types]


class HttpConfig(BaseModel):
    """Request-layer tuning, overridable from config.yaml's "http" block.

    Every field defaults to a conservative value, so an absent block still validates.
    """

    model_config = ConfigDict(extra="ignore")

    max_requests_per_minute: float = 20  # global cap across search + description phases
    # How far each request gap strays from the mean, as a fraction of it. Spreads the
    # requests instead of firing them in lockstep, without changing the rate above.
    rate_jitter: float = 0.4
    search_workers: int = 3  # parallel search-page fetches (behind the shared cap)
    description_workers: int = 3  # parallel job-description fetches (behind the shared cap)
    timeout: float = 20.0  # per-request connect+read timeout; high enough to avoid false retries
    retries: int = 5  # attempts per URL before giving up; a 429 consumes one
    backoff_base: float = 2.0  # exponential backoff base between retries
    backoff_max: float = 60.0  # cap on a single backoff sleep (seconds)
    backoff_jitter: float = 2.0  # random jitter added to each backoff, so parallel retries don't sync up
    retry_after_cap: float = 120.0  # most we'll honour from a 429 Retry-After header (seconds)

    @field_validator("max_requests_per_minute")
    @classmethod
    def _positive_rate(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("max_requests_per_minute must be > 0")
        return v

    @field_validator("rate_jitter")
    @classmethod
    def _fractional_jitter(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:  # at 1.0 a gap can be zero; above it, negative
            raise ValueError("rate_jitter must be >= 0 and < 1")
        return v

    @field_validator("search_workers", "description_workers")
    @classmethod
    def _positive_workers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("worker counts must be >= 1")
        return v


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    search_queries: list[SearchQuery]
    http: HttpConfig = Field(default_factory=HttpConfig)
    title_exclude: list[str] = []
    title_include: list[str] | None = None
    company_exclude: list[str] = []

    @property
    def scrape_queries(self) -> list[SearchQuery]:
        """The queries actually fetched: each search fanned out into one variant per keep-list type."""
        return [variant for query in self.search_queries for variant in query.harvest_variants()]

    @field_validator("search_queries", mode="before")
    @classmethod
    def _expand_locations(cls, v):
        """Fan a query whose ``location`` is a list into one otherwise-identical query per location."""
        if not isinstance(v, list):
            return v
        expanded = []
        for entry in v:
            locations = entry.get("location") if isinstance(entry, dict) else None
            if isinstance(locations, list):
                expanded.extend({**entry, "location": location} for location in locations)
            else:
                expanded.append(entry)
        return expanded

    @field_validator("title_exclude", "title_include", "company_exclude", mode="before")
    @classmethod
    def _flatten_groups(cls, v):
        """Flatten one level of nesting, so the YAML can group phrases into aliased sub-lists."""
        if not isinstance(v, list):
            return v
        return [phrase for item in v for phrase in (item if isinstance(item, list) else [item])]

    @model_validator(mode="after")
    def _default_title_include(self) -> Self:
        """An omitted title_include is every query's keywords, deduped, applied to the title alone.

        LinkedIn matches ``keywords`` against the description too, so a search for "python" returns
        sales roles at companies that write Python. Nothing but this gate reads the title. An
        expression yielding no terms would silently disable it, so it raises; write ``[]`` to mean that.
        """
        if self.title_include is None:
            atoms = (atom for q in self.search_queries for atom in _keyword_atoms(q.keywords))
            self.title_include = list(dict.fromkeys(atoms))
            if not self.title_include:
                raise ValueError("keywords hold no terms to derive title_include from; set it to [] to disable")
        return self

    @field_validator("search_queries")
    @classmethod
    def _non_empty(cls, v: list[SearchQuery]) -> list[SearchQuery]:
        """Reject a config with nothing to search for."""
        if not v:
            raise ValueError("search_queries must contain at least one query")
        return v


def load_config(path: str | Path) -> Config:
    """Read config.yaml off disk and validate it into a Config."""
    try:
        with open(path, encoding="utf-8") as f:
            config_dict = yaml.safe_load(f)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise ConfigurationError(f"Could not load config at {path}: {e}") from e
    return load_and_validate_config(config_dict)


def load_and_validate_config(config_dict: dict) -> Config:
    """Validate a raw config dict, raising a clear ConfigurationError on any problem."""
    try:
        return Config.model_validate(config_dict)
    except ValidationError as e:
        raise ConfigurationError(f"Invalid config:\n{e}") from e
