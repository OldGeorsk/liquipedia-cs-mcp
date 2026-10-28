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


class TeamInfo(BaseModel):
    """Identity of a team page, as resolved by the wiki."""

    name: str
    page_id: int | None = None
    url: str


class RosterRelation(StrEnum):
    CONFIRMED = "confirmed"
    SUBSTITUTE = "substitute"


class RosterMember(BaseModel):
    """One active-roster player on a team page."""

    player_id: str                     # page title used for further lookups
    nickname: str
    real_name: str | None = None
    nationality: str | None = None
    age: int | None = None
    roles: list[str] = Field(default_factory=list)
    joined_at: DateValue | None = None
    relation: RosterRelation = RosterRelation.CONFIRMED
    source: Source | None = None


class StaffMember(BaseModel):
    """One active staff member; kept apart from the playing roster."""

    nickname: str
    real_name: str | None = None
    nationality: str | None = None
    role: str | None = None
    joined_at: DateValue | None = None


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


class CareerStint(BaseModel):
    """One entry of a player's full recorded team history (PRD feature 1)."""

    team: TeamRef                      # name exactly as written on the page
    canonical_team: str | None = None  # rename-resolved name, when known
    game: str = "unknown"              # CS / CS:GO / CS2 / unknown
    start: DateValue | None = None
    end: DateValue | None = None       # None + ongoing=True means "Present"
    ongoing: bool = False
    identity: str | None = None        # annotated role: inactive, stand-in, ...; None = not stated
    note: str | None = None


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


class CareerResult(BaseModel):
    """The payload of get_player_career (PRD feature 1)."""

    player: Player
    stints: list[CareerStint] = Field(default_factory=list)


class TeamRosterResult(BaseModel):
    """The payload of get_team_roster (PRD 2026-10-26 issue-fix P0-01)."""

    team: TeamInfo
    roster: list[RosterMember] = Field(default_factory=list)
    staff: list[StaffMember] = Field(default_factory=list)


class TeamCandidate(BaseModel):
    """One team page returned by search_teams."""

    team_id: str                     # canonical page title, safe for team_id
    name: str
    page_id: int | None = None
    url: str
    snippet: str = ""


class TeamSearchResult(BaseModel):
    """The payload of search_teams."""

    query: str
    candidates: list[TeamCandidate] = Field(default_factory=list)


class AchievementEntry(BaseModel):
    """One row of a team's /Results table, as rendered by LPDB."""

    date: str | None = None
    place: str
    tier: str                     # S-Tier / A-Tier / B-Tier / Qualifier ...
    type: str | None = None       # Offline / Online
    tournament: str
    score: str | None = None      # e.g. "3 : 0"; group stages may be "0/2"
    opponent: str | None = None   # may be a group label like "Grp S."
    prize: float | None = None    # USD; None when not a plain number


class TierSummary(BaseModel):
    """Placements over the returned (possibly tier-filtered) rows."""

    tier: str
    events: int
    wins: int = 0                 # place exactly "1st"
    runner_up: int = 0            # place exactly "2nd"
    top4: int = 0                 # places 1st/2nd/3rd/4th incl. "3rd - 4th"
    prize_total: float = 0.0


class TeamAchievementsResult(BaseModel):
    """The payload of get_team_achievements."""

    team: TeamInfo
    tier_filter: str | None = None
    results: list[AchievementEntry] = Field(default_factory=list)
    stats: list[TierSummary] = Field(default_factory=list)


class WikiHit(BaseModel):
    """One page returned by the generic search_wiki fallback."""

    title: str
    page_id: int | None = None
    url: str
    snippet: str = ""


class WikiSearchResult(BaseModel):
    """The payload of search_wiki: any page type, unfiltered."""

    query: str
    hits: list[WikiHit] = Field(default_factory=list)


class WikiPageResult(BaseModel):
    """The payload of read_wiki_page: raw content, capped for transport."""

    title: str
    page_id: int | None = None
    format: str                   # "wikitext" | "html"
    section: str | None = None
    content: str
    content_chars: int
    truncated: bool


class OverlapWindow(BaseModel):
    """Verified overlap between two stints (PRD feature 2)."""

    verdict: str                     # confirmed / unconfirmed / none
    start: DateValue | None = None
    end: DateValue | None = None
    reason: str | None = None        # why it could not be confirmed


class SharedTeamRecord(BaseModel):
    """One team where both players have recorded stints."""

    team: TeamRef
    canonical_team: str | None = None
    a_stint: CareerStint
    b_stint: CareerStint
    overlap: OverlapWindow
    playing_together: bool | None = None  # None = identities not recorded
    games: list[str] = Field(default_factory=list)


class KinshipRecord(BaseModel):
    """An explicitly stated family relation; never inferred."""

    relation: str      # cousin / brother / ...
    person: str        # the mentioned person, as written
    evidence: str      # the exact sentence from the page
    source_page: str


class OmittedRecords(BaseModel):
    """Shared stints hidden from the default relationship answer."""

    count: int
    reason: str


class RelationshipResult(BaseModel):
    """The payload of get_player_relationship (PRD feature 2)."""

    player_a: Player
    player_b: Player
    shared_teams: list[SharedTeamRecord] = Field(default_factory=list)
    omitted: OmittedRecords | None = None
    kinship: KinshipRecord | None = None
    summary: str


class MatrixTeam(BaseModel):
    """One team taking part in a relationship matrix."""

    team: TeamInfo
    players: list[str] = Field(default_factory=list)  # player page titles compared
    staff: list[str] = Field(default_factory=list)    # shown only when requested


class MatrixConnection(BaseModel):
    """A player pair worth reporting: shared stints, kinship, or both."""

    players: list[str] = Field(default_factory=list)
    from_teams: list[str] = Field(default_factory=list)  # canonical team names
    summary: str
    shared_teams: list[SharedTeamRecord] = Field(default_factory=list)
    omitted: OmittedRecords | None = None
    kinship: KinshipRecord | None = None


class SameTeamNoOverlap(BaseModel):
    """A pair whose only shared teams were played at different times."""

    players: list[str] = Field(default_factory=list)
    from_teams: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)   # shared team names
    count: int                                       # shared-team records


class MatrixStats(BaseModel):
    """Pair counts that make a matrix result auditable."""

    connections: int = 0
    confirmed_pairs: int = 0
    unconfirmed_only_pairs: int = 0
    kinship_pairs: int = 0
    same_team_no_overlap_pairs: int = 0


class SkippedPlayer(BaseModel):
    """A roster player that could not be compared, with the reason."""

    player: str
    given: str | None = None
    reason: str


class MatrixResult(BaseModel):
    """The payload of get_relationship_matrix (teams or players mode)."""

    mode: str                        # teams / players
    teams: list[MatrixTeam] = Field(default_factory=list)
    players_checked: int = 0
    pairs_checked: int = 0
    connections: list[MatrixConnection] = Field(default_factory=list)
    same_team_no_overlap: list[SameTeamNoOverlap] = Field(default_factory=list)
    stats: MatrixStats = Field(default_factory=MatrixStats)
    skipped: list[SkippedPlayer] = Field(default_factory=list)


class ErrorInfo(BaseModel):
    """Machine-readable failure, separate from data-missing warnings."""

    code: str
    message: str
    given: str | None = None
    resolved_as: str | None = None
    suggestion: str | None = None


class Envelope(BaseModel):
    """Unified response wrapper returned by every MCP tool."""

    status: JobStatus | str
    job_id: str | None = None
    player_id: str | None = None
    result: (
        NetworkResult | CareerResult | RelationshipResult | TeamRosterResult
        | TeamSearchResult | MatrixResult | TeamAchievementsResult
        | WikiSearchResult | WikiPageResult | None
    ) = None
    progress: str | None = None
    fetched_at: datetime | None = None
    served_at: datetime = Field(default_factory=utcnow)
    sources: list[Source] = Field(default_factory=list)
    license: str = LICENSE_NAME
    license_url: str = LICENSE_URL
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: ErrorInfo | None = None
