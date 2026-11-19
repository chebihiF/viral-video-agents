"""GET /progress et GET /api/v1/progress : avancement du projet et travail des agents (#26).

GitHub : API REST publique **sans jeton** (décision D20, dépôt public), cache mémoire de 3 min,
requêtes conditionnelles (ETag : un 304 ne compte pas dans la limite de 60 requêtes/heure),
dernières données connues en cas d'échec. Agents : table `events` (#3). Contrat :
contracts/progress.schema.json et contracts/dashboard-routes.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import APIRouter
from fastapi import Request as HttpRequest
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from dashboard import activity, auth, db

DEFAULT_REPO = "chebihiF/viral-video-agents"
API_ROOT = "https://api.github.com"
CACHE_TTL_S = 180.0
MAX_RATE_WAIT_S = 3600.0
MAX_PAGES = 5
HTTP_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 4 * 1024 * 1024
EVENT_WINDOW = 500
STALE_AFTER = timedelta(minutes=30)
TIMELINE_MAX = 10
MAX_INACTIVE = 5
NO_EPIC_ID = 0
NO_EPIC_TITLE = "Sans épique"

IGNORED_LABELS = frozenset({"decision", "wontfix", "duplicate", "invalid", "question"})
IN_PROGRESS_LABELS = frozenset({"in-progress", "en-cours"})
READY_LABELS = frozenset({"P0", "P1"})
EPIC_RE = re.compile(r"[ÉE]pique\s*:\s*#(\d+)", re.IGNORECASE)
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
TITLE_REF_RE = re.compile(r"\(#(\d+)\)")
PR_SUFFIX_RE = re.compile(r"\(#(\d+)\)\s*$")
NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

Step = Literal["dev", "review", "qa", "pr", "deploy"]
# Rôles d'agents (tools/agents) : le modèle et l'étape ne figurent pas dans les événements.
AGENT_MODELS = {
    "dev-backend": "claude-sonnet-5-5",
    "ai-engineer": "claude-sonnet-5-5",
    "comfyui-engineer": "claude-sonnet-5-5",
    "devops": "claude-sonnet-5-5",
    "qa": "claude-sonnet-5-5",
    "product-owner": "claude-sonnet-5-5",
    "ux-designer": "claude-sonnet-5-5",
    "reviewer": "claude-opus-5-5",
    "scout": "claude-haiku-5-5",
}
AGENT_STEPS: dict[str, Step] = {
    "dev-backend": "dev",
    "ai-engineer": "dev",
    "comfyui-engineer": "dev",
    "ux-designer": "dev",
    "reviewer": "review",
    "qa": "qa",
    "devops": "deploy",
}
STATUSES = (
    ("delivered", "Livré"),
    ("review", "Revue"),
    ("in_progress", "En cours"),
    ("ready", "Prêt"),
    ("backlog", "Backlog"),
)
STEPS: tuple[tuple[Step, str], ...] = (
    ("dev", "Dév"),
    ("review", "Revue"),
    ("qa", "QA"),
    ("pr", "PR"),
    ("deploy", "Déploiement"),
)

log = logging.getLogger(__name__)
router = APIRouter()


# --- Contrat (miroir de contracts/progress.schema.json, aligné par test) ---------------------
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Counts(_Model):
    delivered: int
    review: int
    in_progress: int
    ready: int
    backlog: int


class Totals(_Model):
    counts: Counts
    percent: int


class Epic(_Model):
    id: int
    title: str
    counts: Counts
    percent: int


class Sprint(_Model):
    available: bool
    title: str | None


class Agent(_Model):
    name: str
    model: str | None
    issue: int | None
    step: Step | None
    since: str
    active: bool
    elapsed_s: int


class TimelineEntry(_Model):
    version: str
    title: str
    pr: int | None
    date: str | None


class Progress(_Model):
    generated_at: str
    github_error_at: str | None
    totals: Totals
    epics: list[Epic]
    sprint: Sprint
    agents: list[Agent]
    timeline: list[TimelineEntry]


# --- Client GitHub ----------------------------------------------------------------------------
def repo() -> str:
    value = os.environ.get("VVA_GITHUB_REPO", "")
    return value if REPO_RE.fullmatch(value) and ".." not in value else DEFAULT_REPO


class RateLimited(Exception):
    """Limite GitHub atteinte : `until` = instant (epoch, s) de réinitialisation."""

    def __init__(self, until: float) -> None:
        super().__init__("limite de l'API GitHub atteinte")
        self.until = until


def _rate_limit(exc: HTTPError) -> RateLimited | None:
    headers = exc.headers
    if headers is None or exc.code not in (403, 429):
        return None
    reset, retry = headers.get("X-RateLimit-Reset"), headers.get("Retry-After")
    if exc.code == 403 and headers.get("X-RateLimit-Remaining") != "0" and retry is None:
        return None  # 403 ordinaire (accès refusé), pas une limite
    if reset and reset.isascii() and reset.isdigit():
        return RateLimited(float(reset))
    wait = float(retry) if retry and retry.isascii() and retry.isdigit() else 60.0
    return RateLimited(time.time() + wait)


def _next_url(link: str | None) -> str | None:
    match = NEXT_RE.search(link or "")
    url = match.group(1) if match else None
    return url if url and url.startswith(f"{API_ROOT}/") else None  # jamais un autre hôte


class HttpJson:
    """GET JSON sur api.github.com, sans jeton, avec ETag (304 = données déjà connues).

    Suit `Link: rel="next"` (au plus `MAX_PAGES` pages) ; lève `RateLimited` sur 403/429 de limite.
    """

    def __init__(self) -> None:
        self._known: dict[str, tuple[str, Any]] = {}
        self._links: dict[str, str | None] = {}

    def __call__(self, path: str) -> Any:
        url: str | None = f"{API_ROOT}{path}"
        pages: list[Any] = []
        while url and len(pages) < MAX_PAGES:
            data, link = self._get(url)
            if not isinstance(data, list):
                return data
            pages.extend(data)
            url = _next_url(link)
            if url is None:
                break
        return pages

    def _get(self, url: str) -> tuple[Any, str | None]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "vva-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if url in self._known:
            headers["If-None-Match"] = self._known[url][0]
        try:
            # URL https de api.github.com (constante ou lien validé) : pas de schéma file:/.
            with urlopen(Request(url, headers=headers), timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read(MAX_BODY_BYTES + 1)
                etag, link = resp.headers.get("ETag"), resp.headers.get("Link")
        except HTTPError as exc:
            if exc.code == 304 and url in self._known:
                return self._known[url][1], self._links.get(url)
            limited = _rate_limit(exc)
            if limited:
                raise limited from exc
            raise
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("réponse GitHub trop volumineuse")
        data = json.loads(raw)
        if etag:
            self._known[url] = (etag, data)
            self._links[url] = link
        return data, link


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    closed: bool
    completed: bool  # fermé « completed » (≠ « not planned »)
    labels: frozenset[str]
    epic: int | None


@dataclass(frozen=True)
class Pull:
    """PR ouverte (l'API issues les mêle aux tickets) et ticket qu'elle ferme."""

    number: int
    issue: int | None


@dataclass(frozen=True)
class Tag:
    name: str
    sha: str


@dataclass(frozen=True)
class Commit:
    sha: str
    title: str
    pr: int | None
    date: str | None


@dataclass(frozen=True)
class Snapshot:
    issues: tuple[Issue, ...] = ()
    pulls: tuple[Pull, ...] = ()
    tags: tuple[Tag, ...] = ()
    commits: tuple[Commit, ...] = ()


def _items(raw: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("réponse GitHub inattendue")
    return (item for item in raw if isinstance(item, dict))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _labels(item: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        name
        for label in item.get("labels") or []
        if isinstance(label, dict) and (name := _text(label.get("name")))
    )


def parse_issues(raw: Any) -> tuple[tuple[Issue, ...], tuple[Pull, ...]]:
    """Sépare tickets et PR ouvertes (`pull_request` présent) d'une même liste `issues`."""
    issues: list[Issue] = []
    pulls: list[Pull] = []
    for item in _items(raw):
        number = item.get("number")
        if not isinstance(number, int):
            continue
        body, title = _text(item.get("body")) or "", _text(item.get("title")) or ""
        if "pull_request" in item:
            if item.get("state") == "open":
                match = CLOSES_RE.search(body) or TITLE_REF_RE.search(title)
                pulls.append(Pull(number, int(match.group(1)) if match else None))
            continue
        epic = EPIC_RE.search(body)
        issues.append(
            Issue(
                number,
                title,
                item.get("state") == "closed",
                item.get("state_reason") == "completed",
                _labels(item),
                int(epic.group(1)) if epic else None,
            )
        )
    return tuple(issues), tuple(pulls)


def parse_tags(raw: Any) -> tuple[Tag, ...]:
    tags: list[Tag] = []
    for item in _items(raw):
        commit = item.get("commit")
        name = _text(item.get("name"))
        sha = _text(commit.get("sha")) if isinstance(commit, dict) else None
        if name and sha:
            tags.append(Tag(name, sha))
    return tuple(tags)


def parse_commits(raw: Any) -> tuple[Commit, ...]:
    commits: list[Commit] = []
    for item in _items(raw):
        sha, detail = _text(item.get("sha")), item.get("commit")
        if not sha or not isinstance(detail, dict):
            continue
        subject = (_text(detail.get("message")) or "").split("\n", 1)[0].strip()
        refs = PR_SUFFIX_RE.search(subject)
        committer = detail.get("committer")
        date = _text(committer.get("date")) if isinstance(committer, dict) else None
        title = subject[: refs.start()].rstrip() if refs else subject
        commits.append(Commit(sha, title, int(refs.group(1)) if refs else None, date))
    return tuple(commits)


class GithubCache:
    """Cache mémoire : une lecture GitHub par `ttl`, même après un échec (limite 60 req/h).

    Après une limite atteinte, aucune lecture avant la réinitialisation annoncée (plafonnée).
    """

    def __init__(
        self,
        fetch: Callable[[str], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        ttl: float = CACHE_TTL_S,
    ) -> None:
        self._fetch, self._clock, self._wall, self._ttl = fetch, clock, wall, ttl
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._error_at: datetime | None = None
        self._next_at: float | None = None
        self._wait = ttl

    def get(self) -> tuple[Snapshot, datetime | None]:
        with self._lock:
            now = self._clock()
            if self._next_at is None or now >= self._next_at:
                self._wait = self._ttl
                try:
                    self._refresh()
                finally:
                    self._next_at = now + self._wait
            return self._snapshot, self._error_at

    def _refresh(self) -> None:
        base = f"/repos/{repo()}"
        try:
            issues, pulls = parse_issues(self._fetch(f"{base}/issues?state=all&per_page=100"))
            tags = parse_tags(self._fetch(f"{base}/tags?per_page=100"))
            commits = parse_commits(self._fetch(f"{base}/commits?per_page=50"))
            self._snapshot = Snapshot(issues, pulls, tags, commits)
            self._error_at = None
        except RateLimited as exc:
            log.warning("limite GitHub atteinte")
            self._wait = max(self._ttl, min(exc.until - self._wall(), MAX_RATE_WAIT_S))
            self._error_at = datetime.now(UTC)
        except Exception as exc:  # IncompleteRead, HTTPException, JSON… : jamais de 500
            log.warning("lecture GitHub impossible : %s", type(exc).__name__)
            self._error_at = datetime.now(UTC)


CACHE = GithubCache(HttpJson())


# --- Calculs purs -----------------------------------------------------------------------------
def percent(counts: Counts) -> int:
    total = sum(counts.model_dump().values())
    return counts.delivered * 100 // total if total else 0


def classify(issue: Issue, in_review: set[int]) -> str:
    if issue.closed:  # (les fermés « not planned » sont exclus en amont)
        return "delivered"
    if issue.number in in_review:
        return "review"
    if issue.labels & IN_PROGRESS_LABELS:
        return "in_progress"
    if "blocked" not in issue.labels and issue.labels & READY_LABELS:
        return "ready"
    return "backlog"


def _counts(issues: Iterable[Issue], in_review: set[int]) -> Counts:
    values = {name: 0 for name, _ in STATUSES}
    for issue in issues:
        values[classify(issue, in_review)] += 1
    return Counts(**values)


def build_epics(snapshot: Snapshot) -> tuple[Totals, list[Epic]]:
    in_review = {p.issue for p in snapshot.pulls if p.issue is not None}
    epics = sorted((i for i in snapshot.issues if "epic" in i.labels), key=lambda i: i.number)
    counted = [
        i
        for i in snapshot.issues
        if "epic" not in i.labels
        and not i.labels & IGNORED_LABELS
        and (i.completed or not i.closed)
    ]
    known = {e.number for e in epics}
    groups = [(e.number, e.title, [i for i in counted if i.epic == e.number]) for e in epics]
    orphans = [i for i in counted if i.epic not in known]
    if orphans:
        groups.append((NO_EPIC_ID, NO_EPIC_TITLE, orphans))
    result = []
    for number, title, children in groups:
        counts = _counts(children, in_review)
        result.append(Epic(id=number, title=title, counts=counts, percent=percent(counts)))
    total = _counts(counted, in_review)
    return Totals(counts=total, percent=percent(total)), result


def build_timeline(snapshot: Snapshot) -> list[TimelineEntry]:
    commits = {c.sha: c for c in snapshot.commits}
    entries = []
    for tag in snapshot.tags:
        commit = commits.get(tag.sha)
        entries.append(
            TimelineEntry(
                version=tag.name,
                title=commit.title if commit else tag.name,
                pr=commit.pr if commit else None,
                date=commit.date if commit else None,
            )
        )
    entries.sort(key=lambda e: e.date or "", reverse=True)
    return entries[:TIMELINE_MAX]


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _step(agent_type: str, tool: str | None) -> Step | None:
    if tool and "pull_request" in tool.lower():
        return "pr"
    return AGENT_STEPS.get(agent_type)


def shown(agents: list[Agent]) -> list[Agent]:
    """Agents actifs, puis au plus `MAX_INACTIVE` inactifs (un par rôle, hors rôles actifs)."""
    active = [a for a in agents if a.active]
    seen = {a.name for a in active}
    idle: list[Agent] = []
    for agent in agents:  # du plus récent au plus ancien
        if not agent.active and agent.name not in seen and len(idle) < MAX_INACTIVE:
            seen.add(agent.name)
            idle.append(agent)
    return active + idle


def build_agents(events: list[activity.Event], now: datetime) -> list[Agent]:
    """`events` est trié du plus récent au plus ancien. Le ticket n'est pas dans les événements."""
    last: dict[tuple[str, str | None], tuple[activity.Event, datetime]] = {}
    started: dict[tuple[str, str | None], datetime] = {}
    oldest: dict[tuple[str, str | None], datetime] = {}
    for event in events:
        when = _parse_ts(event.ts)
        if event.agent_type is None or when is None:
            continue
        key = (event.agent_type, event.agent_id)
        last.setdefault(key, (event, when))
        oldest[key] = when
        if event.hook_event_name == "SubagentStart":
            started.setdefault(key, when)
    agents = []
    for key, (event, when) in last.items():
        active = event.hook_event_name != "SubagentStop" and now - when < STALE_AFTER
        since = started.get(key, oldest[key])
        elapsed = (now if active else when) - since
        agents.append(
            Agent(
                name=key[0],
                model=AGENT_MODELS.get(key[0]),
                issue=None,
                step=_step(key[0], event.tool_name),
                since=since.isoformat(),
                active=active,
                elapsed_s=max(0, int(elapsed.total_seconds())),
            )
        )
    return shown(agents)


def load_events() -> list[activity.Event]:
    try:
        with db.connect() as conn:
            return activity.latest_events(conn, EVENT_WINDOW)
    except sqlite3.Error as exc:
        log.warning("lecture des événements impossible : %s", type(exc).__name__)
        return []


def collect() -> Progress:
    snapshot, error_at = CACHE.get()
    now = datetime.now(UTC)
    totals, epics = build_epics(snapshot)
    return Progress(
        generated_at=now.isoformat(),
        github_error_at=error_at.isoformat() if error_at else None,
        totals=totals,
        epics=epics,
        sprint=Sprint(available=False, title=None),
        agents=build_agents(load_events(), now),
        timeline=build_timeline(snapshot),
    )


def duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


auth.TEMPLATES.env.filters["duration"] = duration


# --- Routes -----------------------------------------------------------------------------------
@router.get("/progress", response_model=None)
async def progress_page(request: HttpRequest) -> Response:
    if not auth.is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    data = await run_in_threadpool(collect)
    response = auth.TEMPLATES.TemplateResponse(
        request,
        "progress.html",
        {"p": data, "statuses": STATUSES, "steps": STEPS, "error_at": data.github_error_at},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/v1/progress", response_model=None)
async def progress_api(request: HttpRequest) -> Response:
    if not auth.is_authenticated(request):
        return JSONResponse({"detail": "authentification requise"}, status_code=401)
    data = await run_in_threadpool(collect)
    return JSONResponse(data.model_dump(), headers={"Cache-Control": "no-store"})
