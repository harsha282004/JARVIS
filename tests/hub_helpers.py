"""Test harness for the Integration Hub: REAL hub, registry, sync engine, store (SQLite), adapters and routers over in-memory fakes of every external
service (Gmail/Calendar clients, an httpx.MockTransport GitHub, a fake RAG index, a fake Telegram provider). Synthetic data only; no credentials."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.intelligence.hub_router import HubRouter
from agent.intelligence.router import IntelligenceRouter
from agent.rag.models import Document, DocumentChunk, IngestOutcome, IngestResult, RetrievalResult, SourceType
from backend.models.base import Base
from integrations.calendar.service import CalendarService
from integrations.documents.adapter import DocumentsAdapter
from integrations.github.adapter import GitHubAdapter, ProjectRepos
from integrations.github.auth import GitHubTokenStore
from integrations.github.client import GitHubClient
from integrations.gmail.adapter import GmailAdapter
from integrations.gmail.service import GmailService
from integrations.hub.hub import IntegrationHub
from integrations.hub.models import Permission
from integrations.calendar.adapter import CalendarAdapter
from integrations.messaging.adapter import MessagingAdapter
from integrations.messaging.base import ProviderRegistry
from integrations.messaging.service import MessagingService
from tests.calendar_helpers import cal_event
from tests.intelligence_helpers import IST, NOW, Harness, NoLLM, build_harness, email_raw, ist
from tests.messaging_helpers import FakeProvider, msg

import backend.models.hub  # noqa: F401

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # syntactically plausible, obviously fake


class FakeAuth:
    """The Google authenticator's surface the adapters use."""

    def __init__(self, ready=True):
        self.ready, self.authorized, self.forgotten, self.revoked, self.fail = ready, 0, 0, 0, None

    def is_ready(self):
        return self.ready

    def authorize(self):
        self.authorized += 1
        if self.fail:
            raise self.fail
        self.ready = True

    def forget(self):
        self.forgotten += 1
        was, self.ready = self.ready, False
        return was

    def revoke_remote(self):
        self.revoked += 1
        return True


class FakeGitHub:
    """A scriptable api.github.com behind httpx.MockTransport."""

    def __init__(self):
        self.calls: list[str] = []
        self.status_override: int | None = None
        self.headers_override: dict[str, str] = {}
        self.repos = [{"full_name": "harsh/jarvis", "description": "Personal AI assistant", "private": False, "default_branch": "main", "language": "Python",
                       "pushed_at": "2026-09-24T07:00:00Z", "updated_at": "2026-09-24T07:00:00Z", "open_issues_count": 2, "archived": False}]
        self.commits = [{"sha": "a" * 40, "commit": {"message": "Add integration hub\n\nlong body", "author": {"name": "Harsh", "date": "2026-09-24T06:30:00Z"}}},
                        {"sha": "b" * 40, "commit": {"message": "Fix calendar sync", "author": {"name": "Harsh", "date": "2026-09-23T10:00:00Z"}}}]
        self.issues = [{"number": 7, "title": "Dashboard cards", "state": "open", "user": {"login": "harsh"}, "created_at": "2026-09-20T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z", "labels": [{"name": "ui"}]},
                       {"number": 8, "title": "A pull request", "state": "open", "pull_request": {}, "user": {"login": "x"}, "created_at": "2026-09-20T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z"}]
        self.pulls = [{"number": 8, "title": "Add GitHub adapter", "state": "open", "draft": False, "user": {"login": "harsh"}, "created_at": "2026-09-22T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z", "merged_at": None}]
        self.etags = True
        self.readme = "# Demo\n\nA demo project.\n\n## Requirements\n\n- Python 3.11\n- PostgreSQL 15\n\n## Installation\n\npip install -r requirements.txt\n"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.status_override:
            return httpx.Response(self.status_override, json={"message": "scripted"}, headers=self.headers_override)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        body: object
        if path == "/user":
            body = {"login": "harsh"}
        elif path == "/user/repos":
            body = self.repos
        elif path.endswith("/commits"):
            body = self.commits
        elif path.endswith("/issues"):
            body = self.issues
        elif path.endswith("/pulls"):
            body = self.pulls
        elif path.endswith("/readme"):
            import base64

            body = {"name": "README.md", "encoding": "base64", "content": base64.b64encode(self.readme.encode()).decode()}
        elif path.endswith("/branches"):
            body = [{"name": "main", "protected": True}]
        elif path.startswith("/repos/"):
            name = path[len("/repos/"):]
            hit = next((r for r in self.repos if r["full_name"] == name), None)
            if hit is None:
                return httpx.Response(404, json={"message": "Not Found"})
            body = hit
        else:
            return httpx.Response(404, json={})
        etag = f'"{hash(json.dumps(body, sort_keys=True)) & 0xffffff:x}"'
        if self.etags and request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, json=body, headers={"ETag": etag, "X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "9999999999"})


class FakeRag:
    """Just enough of RagService: ingest by content hash, list, search, chunks."""

    def __init__(self):
        self.docs: dict[str, Document] = {}
        self.hashes: dict[str, str] = {}
        self.ingested: list[str] = []
        self.fail: set[str] = set()

    def ingest_file(self, path, force=False):
        import hashlib

        path = str(path)
        self.ingested.append(path)
        if path in self.fail:
            return IngestResult(outcome=IngestOutcome.FAILED, error="parser failure")
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        existing = self.docs.get(path)
        if existing and self.hashes.get(path) == digest and not force:
            return IngestResult(outcome=IngestOutcome.UNCHANGED, document=existing)
        doc = Document(filename=Path(path).name, source_type=SourceType.TXT, source_location=path, content_hash=digest, chunk_count=1, indexed_at=datetime.now(timezone.utc),
                       status="indexed") if False else Document(filename=Path(path).name, source_type=SourceType.TXT, source_location=path, content_hash=digest, chunk_count=1)
        self.docs[path], self.hashes[path] = doc, digest
        return IngestResult(outcome=IngestOutcome.REINDEXED if existing else IngestOutcome.INDEXED, document=doc)

    def list_documents(self, statuses=None):
        return list(self.docs.values())

    def get_document(self, document_id):
        return next((d for d in self.docs.values() if d.document_id == document_id), None)

    def get_chunks(self, document_id):
        doc = self.get_document(document_id)
        text = Path(doc.source_location).read_text(encoding="utf-8") if doc else ""
        return [DocumentChunk(document_id=document_id, chunk_index=0, text=text or "x")]

    def delete_document(self, document_id):
        return True

    def search(self, query):
        hits = []
        for path, doc in self.docs.items():
            text = Path(path).read_text(encoding="utf-8")
            if all(w in text.lower() for w in query.lower().split()):
                hits.append(RetrievalResult(chunk_id="c1", document_id=doc.document_id, chunk_index=0, text=text[:200], score=0.8, filename=doc.filename, title=None,
                                            source_type=SourceType.TXT, page=1))
        return hits


@dataclass
class HubHarness:
    base: Harness
    hub: IntegrationHub
    router: IntelligenceRouter
    gmail_auth: FakeAuth
    calendar_auth: FakeAuth
    github: FakeGitHub
    github_store: GitHubTokenStore
    projects: ProjectRepos
    rag: FakeRag
    provider: FakeProvider
    docs_dir: Path
    memories: list = field(default_factory=list)
    delivered: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def say(self, text: str, session: str = "s1"):
        reply = self.router.handle(text, session)
        return None if reply is None else reply.text

    @property
    def calendar_client(self):
        return self.base.calendar_client

    @property
    def gmail_client(self):
        return self.base.gmail_client

    def sync(self, name: str, force: bool = True):
        return self.hub.engine.sync(name, force=force)


def build_hub_harness(tmp_path: Path, *, emails=(), calendar_events=(), tasks=(), grant_create=True, github_token=True, messages=(), **kw) -> HubHarness:
    base = build_harness(tmp_path, emails=emails, calendar_events=calendar_events, tasks=tasks, **kw)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    state = tmp_path / "state"
    hub = IntegrationHub(factory, state / "integrations.json", base.bus, IST, base.clock, center=base.center)

    gmail_auth, calendar_auth = FakeAuth(), FakeAuth()
    gmail_service = GmailService(base.gmail_client, NoLLM(), is_ready=gmail_auth.is_ready)
    calendar_service = CalendarService(base.calendar_client, zone=IST, clock=base.clock, is_ready=calendar_auth.is_ready)
    base.service.executor and None
    hub.register(GmailAdapter(gmail_service, gmail_auth, IST, base.clock, attachments_dir=tmp_path / "attachments"))
    hub.register(CalendarAdapter(calendar_service, calendar_auth, IST, base.clock))

    github = FakeGitHub()
    store = GitHubTokenStore(state / "github.token", encrypt=False)
    if github_token:
        store.save(TOKEN)
    projects = ProjectRepos(state / "github_projects.json")
    gh_client = GitHubClient(store.token, http=httpx.Client(transport=httpx.MockTransport(github.handler)), sleep=lambda s: None, clock=lambda: base.clock().timestamp())
    hub.register(GitHubAdapter(gh_client, store, projects, base.clock))

    provider = FakeProvider(list(messages))
    registry = ProviderRegistry()
    registry.register(provider)
    hub.register(MessagingAdapter(MessagingService(registry, NoLLM()), IST, base.clock))

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    rag = FakeRag()
    hub.register(DocumentsAdapter(rag, [docs_dir], state / "docwatch.json", base.clock))

    if grant_create:
        hub.registry.grant("calendar", Permission.CREATE_EVENT)
    memories: list[str] = []
    hub_router = HubRouter(hub, base.service, remember=memories.append)
    router = IntelligenceRouter(base.service, hub_router)
    h = HubHarness(base, hub, router, gmail_auth, calendar_auth, github, store, projects, rag, provider, docs_dir, memories)
    hub.bus.subscribe(None, lambda e: h.events.append(e.type.value))
    return h
