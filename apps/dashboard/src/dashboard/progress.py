"""GET /progress et GET /api/v1/progress : avancement du projet et travail des agents (#26).

GitHub : API REST publique **sans jeton** (décision D20, dépôt public), cache mémoire de 60 s,
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
CACHE_TTL_S = 60.0
HTTP_TIMEOUT_S = 5.0
MAX_BODY_BYTES = 4 * 1024 * 1024
EVENT_WINDOW = 500
STALE_AFTER = timedelta(minutes=30)
TIMELINE_MAX = 10
NO_EPIC_ID = 0
NO_EPIC_TITLE = "Sans épique"

IGNORED_LABELS = frozenset({"decision", "wontfix", "duplicate", "invalid", "question"})
IN_PROGRESS_LABELS = frozenset({"in-progress", "en-cours"})
READY_LABELS = frozenset({"P0", "P1"})
EPIC_RE = re.compile(r"[ÉE]pique\s*:\s*#(\d+)", re.IGNORECASE)
BRANCH_RE = re.compile(r"^[\w-]+/(\d+)-")
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


class HttpJson:
    """GET JSON sur api.github.com, sans jeton, avec ETag (304 = données déjà connues)."""

    def __init__(self) -> None:
        self._known: dict[str, tuple[str, Any]] = {}

    def __call__(self, path: str) -> Any:
        url = f"{API_ROOT}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "vva-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if url in self._known:
            headers["If-None-Match"] = self._known[url][0]
        try:
            # URL https construite sur une constante : pas de schéma file:/ possible.
            with urlopen(Request(url, headers=headers), timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read(MAX_BODY_BYTES + 1)
                etag = resp.headers.get("ETag")
        except HTTPError as exc:
            if exc.code == 304 and url in self._known:
                return self._known[url][1]
            raise
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("réponse GitHub trop volumineuse")
        data = json.loads(raw)
        if etag:
            self._known[url] = (etag, data)
        return data


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    closed: bool
    labels: frozenset[str]
    epic: int | None


@dataclass(frozen=True)
class Pull:
    number: int
    title: str
    open: bool
    merged_at: str | None
    sha: str | None
    issue: int | None


@dataclass(frozen=True)
class Tag:
    name: str
    sha: str


@dataclass(frozen=True)
class Snapshot:
    issues: tuple[Issue, ...] = ()
    pulls: tuple[Pull, ...] = ()
    tags: tuple[Tag, ...] = ()


def _items(raw: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("réponse GitHub inattendue")
    return (item for item in raw if isinstance(item, dict))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_issues(raw: Any) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    for item in _items(raw):
        number = item.get("number")
        if "pull_request" in item or not isinstance(number, int):
            continue
        labels = frozenset(
            name
            for label in item.get("labels") or []
            if isinstance(label, dict) and (name := _text(label.get("name")))
        )
        match = EPIC_RE.search(_text(item.get("body")) or "")
        epic = int(match.group(1)) if match else None
        issues.append(
            Issue(
                number, _text(item.get("title")) or "", item.get("state") == "closed", labels, epic
            )
        )
    return tuple(issues)


def parse_pulls(raw: Any) -> tuple[Pull, ...]:
    pulls: list[Pull] = []
    for item in _items(raw):
        number = item.get("number")
        if not isinstance(number, int):
            continue
        head = item.get("head")
        ref = _text(head.get("ref")) if isinstance(head, dict) else None
        match = BRANCH_RE.match(ref or "")
        pulls.append(
            Pull(
                number,
                _text(item.get("title")) or "",
                item.get("state") == "open",
                _text(item.get("merged_at")),
                _text(item.get("merge_commit_sha")),
                int(match.group(1)) if match else None,
            )
        )
    return tuple(pulls)


def parse_tags(raw: Any) -> tuple[Tag, ...]:
    tags: list[Tag] = []
    for item in _items(raw):
        commit = item.get("commit")
        name = _text(item.get("name"))
        sha = _text(commit.get("sha")) if isinstance(commit, dict) else None
        if name and sha:
            tags.append(Tag(name, sha))
    return tuple(tags)


class GithubCache:
    """Cache mémoire : une lecture GitHub par `ttl`, même après un échec (limite 60 req/h)."""

    def __init__(
        self,
        fetch: Callable[[str], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_TTL_S,
    ) -> None:
        self._fetch, self._clock, self._ttl = fetch, clock, ttl
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self._error_at: datetime | None = None
        self._checked_at: float | None = None

    def get(self) -> tuple[Snapshot, datetime | None]:
        with self._lock:
            now = self._clock()
            if self._checked_at is None or now - self._checked_at >= self._ttl:
                self._refresh()
                self._checked_at = now
            return self._snapshot, self._error_at

    def _refresh(self) -> None:
        base = f"/repos/{repo()}"
        try:
            self._snapshot = Snapshot(
                parse_issues(self._fetch(f"{base}/issues?state=all&per_page=100")),
                parse_pulls(self._fetch(f"{base}/pulls?state=all&per_page=100")),
                parse_tags(self._fetch(f"{base}/tags?per_page=100")),
            )
            self._error_at = None
        except (OSError, ValueError) as exc:  # URLError, HTTPError, délai, JSON invalide
            log.warning("lecture GitHub impossible : %s", type(exc).__name__)
            self._error_at = datetime.now(UTC)


CACHE = GithubCache(HttpJson())


# --- Calculs purs -----------------------------------------------------------------------------
def percent(counts: Counts) -> int:
    total = sum(counts.model_dump().values())
    return round(counts.delivered * 100 / total) if total else 0


def classify(issue: Issue, in_review: set[int]) -> str:
    if issue.closed:
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
    in_review = {p.issue for p in snapshot.pulls if p.open and p.issue is not None}
    epics = sorted((i for i in snapshot.issues if "epic" in i.labels), key=lambda i: i.number)
    counted = [
        i for i in snapshot.issues if "epic" not in i.labels and not i.labels & IGNORED_LABELS
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
    merged = {p.sha: p for p in snapshot.pulls if p.sha and p.merged_at}
    entries = []
    for tag in snapshot.tags:
        pr = merged.get(tag.sha)
        entries.append(
            TimelineEntry(
                version=tag.name,
                title=pr.title if pr else tag.name,
                pr=pr.number if pr else None,
                date=pr.merged_at if pr else None,
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
    agents.sort(key=lambda a: not a.active)  # stable : actifs d'abord, récents d'abord
    return agents


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
