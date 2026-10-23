"""Turn Liquipedia wikitext into structured facts.

Two page shapes matter here:
- player pages: an ``{{Infobox player}}`` whose ``team_history`` lists
  ``{{TH|start — end|Team}}`` stints;
- team pages: ``{{Squad}}`` tables of ``{{Person|...}}`` rows carrying
  join/leave dates, used for current and historical teammates.

Every function returns what it can actually see in the text — unknown
stays unknown; nothing is inferred from outside knowledge.
"""

import datetime as dt
import re
from dataclasses import dataclass, field

from app.config import Settings
from app.models import (
    DateValue, Player, Source, TeamRef, TeamStatus, Tenure,
)

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WIKI_MARKUP = re.compile(r"'{2,3}|<[^>]+>")
_TH_LINE = re.compile(r"\{\{TH\|([^|}]+)\|([^|}]+?)(?:\|[^|}]*)?\}\}")
_PERSON_ROW = re.compile(r"\{\{Person\|(.*?)\}\}")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Staff roles are excluded from teammate lists per PRD section 4. Matched by
# keyword containment because wikis write "Interim Coach", "Assistant Coach",
# "Data Analyst" and similar variants.
_STAFF_KEYWORDS = (
    "coach", "manager", "analyst", "ceo", "owner", "streamer",
    "content creator", "caster", "host",
)
_SUBSTITUTE_ROLES = {"substitute", "sub", "stand-in", "standin"}


def _is_staff_role(role_lower: str) -> bool:
    return any(keyword in role_lower for keyword in _STAFF_KEYWORDS)

_ROLE_ALIASES = {
    "in-game leader": "IGL", "igl": "IGL", "captain": "IGL",
    "awp": "AWPer", "awper": "AWPer", "sniper": "AWPer",
    "entry fragger": "entry", "entry": "entry", "fragger": "entry",
    "rifler": "rifler", "lurker": "lurker", "support": "support",
}

# Fallback nationality for {{Person|flag=..}} codes; a teammate's own page
# is preferred and overrides this map.
_FLAG_NAMES = {
    "ar": "Argentina", "at": "Austria", "au": "Australia", "ba": "Bosnia and Herzegovina",
    "be": "Belgium", "bg": "Bulgaria", "br": "Brazil", "ca": "Canada",
    "ch": "Switzerland", "cl": "Chile", "cn": "China", "cz": "Czechia",
    "de": "Germany", "dk": "Denmark", "ee": "Estonia", "es": "Spain",
    "fi": "Finland", "fr": "France", "gb": "United Kingdom", "gr": "Greece",
    "hr": "Croatia", "hu": "Hungary", "id": "Indonesia", "il": "Israel",
    "in": "India", "is": "Iceland", "it": "Italy", "jp": "Japan",
    "kr": "South Korea", "lt": "Lithuania", "lv": "Latvia", "mk": "North Macedonia",
    "mn": "Mongolia", "mx": "Mexico", "nl": "Netherlands", "no": "Norway",
    "nz": "New Zealand", "pl": "Poland", "pt": "Portugal", "ro": "Romania",
    "rs": "Serbia", "ru": "Russia", "se": "Sweden", "si": "Slovenia",
    "sk": "Slovakia", "tr": "Turkey", "ua": "Ukraine", "us": "United States",
    "uz": "Uzbekistan", "vn": "Vietnam", "za": "South Africa",
}


def clean(value: str) -> str:
    """Strip refs, tags and quote markup from a wikitext fragment."""
    return _WIKI_MARKUP.sub("", _HTML_COMMENT.sub("", value)).strip()


def extract_template(wikitext: str, name: str) -> str | None:
    """Return the inner text of the first ``{{name ...}}`` (balanced braces)."""
    start = wikitext.find("{{" + name)
    if start < 0:
        return None
    depth, i = 0, start
    while i < len(wikitext) - 1:
        if wikitext[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif wikitext[i : i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start + 2 : i - 2]
        else:
            i += 1
    return None


def parse_template_params(block: str) -> dict[str, str]:
    """Split a template body into ``key -> value`` at top-level pipes."""
    params: dict[str, str] = {}
    depth, current = 0, []
    for ch in block:
        if ch in "{[":
            depth += 1
        elif ch in "}]" and depth:
            depth -= 1
        if ch == "|" and depth == 0:
            _flush_param(params, current)
            current = []
        else:
            current.append(ch)
    _flush_param(params, current)
    return params


def _flush_param(params: dict[str, str], chars: list[str]) -> None:
    text = "".join(chars).strip()
    if not text or "=" not in text:
        return
    key, _, value = text.partition("=")
    params[key.strip().lower()] = value.strip()


def parse_date(raw: str) -> DateValue | None:
    """Parse common Liquipedia date shapes, keeping original precision."""
    text = clean(raw)
    if not text or text.lower() in {"present", "??", "?"}:
        return None
    # Wildcard years: "201?" spans 2010-2019, "20??" spans the 2000s.
    match = re.fullmatch(r"(\d{0,3})\?+(?:-\?\?)*(?:-\?\?)*", text)
    if match:
        base = match.group(1)
        span = 10 ** (4 - len(base))
        year = int(base) * span if base else None
        return DateValue(
            raw=text, year=year,
            year_max=(year + span - 1) if year is not None else None,
        )
    # 1997-2-16 / 2012-??-?? (a '??' part lowers precision)
    match = re.fullmatch(r"(\d{4})(?:-(\d{1,2}|\?\?))?(?:-(\d{1,2}|\?\?))?", text)
    if match:
        parts = [None if p in (None, "??") else int(p) for p in match.groups()]
        if parts[1] and not (1 <= parts[1] <= 12):
            parts[1] = None
        if parts[2] and not (1 <= parts[2] <= 31):
            parts[2] = None
        if parts[2] and not parts[1]:
            parts[2] = None
        return DateValue(raw=text, year=parts[0], month=parts[1], day=parts[2])
    # "February 16, 1997" or "February 1997" or "1997"
    match = re.fullmatch(r"([A-Za-z]+)(?:\s+(\d{1,2}))?,\s*(\d{4})", text)
    if match and match.group(1).lower() in _MONTHS:
        return DateValue(
            raw=text, year=int(match.group(3)), month=_MONTHS[match.group(1).lower()],
            day=int(match.group(2)) if match.group(2) else None,
        )
    if re.fullmatch(r"\d{4}", text):
        return DateValue(raw=text, year=int(text))
    return DateValue(raw=text)


_MAX_ORDINAL = 3_652_592  # a far-future cap: ~year 9999


def date_bounds(value: DateValue | None, *, is_start: bool) -> tuple[int, int]:
    """Certainty window as day ordinals.

    A missing start is 'unknown past' -> [1, MAX]; a missing end is read as
    'ongoing' (Present) -> [MAX, MAX]. Coarser precision widens the window.
    """
    if value is None or value.year is None:
        return (1, _MAX_ORDINAL) if is_start else (_MAX_ORDINAL, _MAX_ORDINAL)
    year, month, day = value.year, value.month, value.day
    hi_year = value.year_max or year
    try:
        low = dt.date(year, month or 1, day or 1).toordinal()
        hi_month = month or 12
        hi_day = day or _last_day(hi_year, hi_month)
        high = dt.date(hi_year, hi_month, hi_day).toordinal()
        return low, high
    except ValueError:
        return (1, _MAX_ORDINAL) if is_start else (_MAX_ORDINAL, _MAX_ORDINAL)


def _last_day(year: int, month: int | None) -> int:
    if month is None:
        return 366 if _leap(year) else 365
    return (
        dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    ).day


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def compute_age(birth: DateValue | None, on: dt.date) -> int | None:
    """Exact age in years; needs a complete birth date (PRD section 3)."""
    if birth is None or not birth.complete:
        return None
    try:
        born = dt.date(birth.year, birth.month, birth.day)  # type: ignore[arg-type]
    except ValueError:
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def overlap_verdict(
    a: tuple[DateValue | None, DateValue | None],
    b: tuple[DateValue | None, DateValue | None],
) -> str:
    """Compare two stints: 'confirmed', 'unconfirmed' or 'none'.

    'confirmed' means the two certainly overlapped for every date the
    sources could mean; 'unconfirmed' means they might have.
    """
    a_start, a_end = date_bounds(a[0], is_start=True), date_bounds(a[1], is_start=False)
    b_start, b_end = date_bounds(b[0], is_start=True), date_bounds(b[1], is_start=False)
    # Tighten windows against each other: a join never happens after a leave.
    a_start = (a_start[0], min(a_start[1], a_end[1]))
    b_start = (b_start[0], min(b_start[1], b_end[1]))
    a_end = (max(a_end[0], a_start[0]), a_end[1])
    b_end = (max(b_end[0], b_start[0]), b_end[1])
    # Certainly disjoint?
    if a_start[0] > b_end[1] or b_start[0] > a_end[1]:
        return "none"
    # Certainly intersecting? (latest possible start <= earliest possible end)
    if a_start[1] <= b_end[0] and b_start[1] <= a_end[0]:
        return "confirmed"
    return "unconfirmed"


@dataclass
class SquadEntry:
    """One ``{{Person|...}}`` row from a team page."""

    nickname: str
    real_name: str | None = None
    nationality: str | None = None
    roles: list[str] = field(default_factory=list)
    staff: bool = False            # coach / manager / ... excluded as teammate
    substitute: bool = False
    join: DateValue | None = None
    leave: DateValue | None = None
    section: str = "active"        # which squad table the row belongs to


def _normalize_role(raw: str) -> str:
    key = raw.strip().lower()
    return _ROLE_ALIASES.get(key, key.title() if key else "")


def parse_player(
    wikitext: str, *, title: str, page_id: int | None, settings: Settings,
    revision_id: int | None = None, fetched_at=None, url: str | None = None,
) -> Player:
    """Build the PRD section 3 profile from a player page."""
    from app.liquipedia import page_url

    params = parse_template_params(extract_template(wikitext, "Infobox player") or "")
    source = Source(
        page=title, page_id=page_id, url=url or page_url(settings, title),
        revision_id=revision_id, fetched_at=fetched_at,
    )
    missing: list[str] = []

    nickname = clean(params.get("id", "")) or title
    real_name = clean(params.get("romanized_name", "")) or clean(params.get("name", "")) or None
    if real_name is None:
        missing.append("real name not stated on page")

    birth = parse_date(params.get("birth_date", ""))
    if birth is None:
        missing.append("birth date not stated on page")

    country = clean(params.get("country", "")) or None
    if country is None:
        missing.append("nationality not stated on page")

    roles = [
        _normalize_role(part)
        for part in re.split(r"[;,]", params.get("roles", "") or params.get("role", ""))
        if part.strip()
    ]
    for extra in ("role2", "role3"):
        if params.get(extra, "").strip():
            roles.append(_normalize_role(params[extra]))
    if not roles:
        missing.append("no role stated on page")

    team_name = clean(params.get("team", ""))
    status_raw = clean(params.get("status", "")).lower()
    if team_name:
        team_status = TeamStatus.ACTIVE
        if status_raw in {"retired"}:
            team_status = TeamStatus.RETIRED
    elif status_raw in {"retired"}:
        team_status = TeamStatus.RETIRED
    elif status_raw in {"active", "not active", "unknown"} or status_raw == "":
        # Active with no team listed: the page confirms the player has no team.
        team_status = TeamStatus.NONE if status_raw == "active" else TeamStatus.UNKNOWN
    else:
        team_status = TeamStatus.UNKNOWN

    return Player(
        page_id=page_id, nickname=nickname, real_name=real_name,
        nationality=country, birth_date=birth,
        age=compute_age(birth, dt.date.today()) if birth else None,
        current_team=TeamRef(name=team_name) if team_name else None,
        team_status=team_status, roles=roles, sources=[source], missing=missing,
    )


def parse_team_history(wikitext: str, settings: Settings) -> list[Tenure]:
    """All stints from the infobox ``team_history``, oldest first as on page."""
    block = extract_template(wikitext, "Infobox player") or ""
    params = parse_template_params(block)
    history_raw = params.get("team_history", "")
    tenures: list[Tenure] = []
    for span, team_raw in _TH_LINE.findall(_HTML_COMMENT.sub("", history_raw)):
        start_raw, _, end_raw = span.partition("—")
        end_text = clean(end_raw)
        still_there = end_text.lower() == "present"
        team_name = clean(team_raw)
        tenures.append(
            Tenure(
                team=TeamRef(name=team_name, url=(
                    None if not team_name else
                    f"{settings.api_url.rsplit('/api.php', 1)[0]}/{team_name.replace(' ', '_')}"
                )),
                start=parse_date(start_raw),
                end=None if still_there else parse_date(end_text),
            )
        )
    return tenures


def recent_previous_teams(tenures: list[Tenure], current_team: str | None, limit: int = 3):
    """Most recent 3 ended, distinct stints excluding the current team.

    Duplicate stints at the same team keep only the latest one. Ranking uses
    the pessimistic end bound (earliest date an end could be), so stints with
    wildcard dates never outrank firmly dated ones.
    """
    others = [t for t in tenures if not _same_team(t.team.name, current_team)]
    others = [t for t in others if t.end is not None]  # 'Present' = current stint

    def rank_key(tenure: Tenure) -> tuple[bool, int, int]:
        end = tenure.end
        end_known = end is not None and end.year is not None
        # Unknown-end stints sort last but stay eligible when fewer than 3.
        end_low = date_bounds(end, is_start=False)[0] if end_known else 0
        start_high = date_bounds(tenure.start, is_start=True)[1] if tenure.start else 0
        return (end_known, end_low, start_high)

    deduped: dict[str, Tenure] = {}
    for tenure in others:
        key = _team_key(tenure.team.name)
        deduped[key] = tenure  # later entries win: list is oldest -> newest
    ranked = sorted(deduped.values(), key=rank_key, reverse=True)
    return ranked[:limit]


def _team_key(name: str) -> str:
    return name.strip().lower().replace("_", " ")


def _same_team(a: str | None, b: str | None) -> bool:
    return bool(a and b and _team_key(a) == _team_key(b))


def first_active_squad(entries: list[SquadEntry]) -> list[SquadEntry]:
    """Rows of the first 'active' table only — the current-game roster."""
    collecting = False
    rows: list[SquadEntry] = []
    for entry in entries:
        if entry.section == "active":
            collecting = True
            rows.append(entry)
        elif collecting:
            break
    return rows


def parse_squads(wikitext: str) -> list[SquadEntry]:
    """Every ``{{Person}}`` row on a team page, tagged with its table.

    The first ``active`` squad belongs to the current (CS2) tab; older
    game tabs repeat the pattern further down the page.
    """
    entries: list[SquadEntry] = []
    section = "none"
    for line in wikitext.splitlines():
        squad = re.search(r"\{\{Squad\|status=(\w+)", line)
        if squad:
            section = squad.group(1)
            continue
        if "Stand-ins table" in line or "{{Stand-ins" in line:
            section = "standin"
            continue
        if line.strip() == "}}":
            section = "none"
            continue
        row = _PERSON_ROW.search(line)
        if not row or section == "none":
            continue
        params = parse_template_params(row.group(1))
        role_raw = clean(params.get("role", "")).lower()
        entries.append(
            SquadEntry(
                nickname=clean(params.get("id", "")),
                real_name=clean(params.get("name", "")) or None,
                nationality=_FLAG_NAMES.get(clean(params.get("flag", "")).lower()),
                roles=(["IGL"] if params.get("igl", "").strip().lower() == "y" else []),
                staff=_is_staff_role(role_raw),
                substitute=role_raw in _SUBSTITUTE_ROLES,
                join=parse_date(params.get("joindate", "")),
                leave=parse_date(params.get("leavedate", "")),
                section=section,
            )
        )
    return entries
