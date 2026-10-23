"""Pydantic models shared across the whole service.

Design principle (PRD section 3): every fact is traceable to a source.
Missing data is represented as null plus a human-readable reason — never guessed.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field

from app.config import LICENSE_NAME, LICENSE_URL


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DateValue(BaseModel):
    """A date that keeps its original precision; day/month may be absent.

    ``year_max`` covers wildcard years like ``201?`` (2010 through 2019).
    """

    raw: str
    year: int | None = None
    year_max: int | None = None
    month: int | None = None
    day: int | None = None

    @property
    def complete(self) -> bool:
        return self.year is not None and self.month is not None and self.day is not None


class Source(BaseModel):
    """Where a fact came from: page, revision and fetch time when available."""

    page: str
    page_id: int | None = None
    url: str
    revision_id: int | None = None
    fetched_at: datetime | None = None


class TeamRef(BaseModel):
    """A team identified by its canonical wiki page."""

    name: str
    page_id: int | None = None
    url: str | None = None


class TeamStatus(StrEnum):
    ACTIVE = "active"
    NONE = "none"          # page explicitly states the player has no team
    RETIRED = "retired"
    SUBSTITUTE = "substitute"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class Player(BaseModel):
    """Basic profile of a player (PRD section 3)."""

    page_id: int | None = None
    nickname: str
    real_name: str | None = None
    nationality: str | None = None
    birth_date: DateValue | None = None
    age: int | None = None
    current_team: TeamRef | None = None
    team_status: TeamStatus = TeamStatus.UNKNOWN
    roles: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # reasons for null fields


class Tenure(BaseModel):
    """One stint of the target player at a team."""

    team: TeamRef
    start: DateValue | None = None
    end: DateValue | None = None


class RelationConfidence(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"  # overlap could not be pinned down from sources


class Teammate(BaseModel):
    """A teammate plus the context of the shared roster."""

    player: Player
    team: TeamRef
    relation: RelationConfidence = RelationConfidence.CONFIRMED
    note: str | None = None  # e.g. "substitute", "month-precision overlap"


class PreviousTeamGroup(BaseModel):
    """Target player's stint at a historical team and its overlapping members."""

    tenure: Tenure
    teammates: list[Teammate] = Field(default_factory=list)
    unconfirmed: list[Teammate] = Field(default_factory=list)
    note: str | None = None  # why history could not be confirmed, when it can't


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    PARTIAL = "partial"  # stopped by page budget; resumable
    FAILED = "failed"
    PAUSED = "paused"    # upstream throttling; resumable


class NetworkResult(BaseModel):
    """The payload of get_player_network (PRD section 5)."""

    player: Player
    current_teammates: list[Teammate] = Field(default_factory=list)
    previous_teams: list[PreviousTeamGroup] = Field(default_factory=list)


class Envelope(BaseModel):
    """Unified response wrapper returned by every MCP tool."""

    status: JobStatus | str
    job_id: str | None = None
    player_id: str | None = None
    result: NetworkResult | None = None
    progress: str | None = None
    fetched_at: datetime | None = None
    served_at: datetime = Field(default_factory=utcnow)
    sources: list[Source] = Field(default_factory=list)
    license: str = LICENSE_NAME
    license_url: str = LICENSE_URL
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None
