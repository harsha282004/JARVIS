"""Durable, minimal workflow state: checkpoints, the side-effect ledger (idempotency), entity links, history and the audit log.

What is persisted is deliberately small: ids, dates, statuses, counts, and facts WITHOUT their evidence sentences. Email/document/page text, tokens and any typed
payload never reach disk here. The ledger is the crash-recovery backbone: an effect (task, reminder) is recorded as `begun` BEFORE it is attempted and `done` with its
object id right after, so a restart can tell "never attempted", "attempted, outcome unknown" (verify externally, adopt what exists) and "done" (never repeat).
"""

import threading
import time
from pathlib import Path
from typing import Any

from backend.core.redaction import redact
from backend.core.state_store import JsonFile, JsonLines
from workflows.models import Fact, SStatus, WStatus, Workflow

PERSIST_KEYS = frozenset({"fact", "facts", "task_id", "reminder_id", "remind_at", "message_ids", "count", "url", "day", "repo", "login", "reused", "due", "title", "conflict", "event_ids",
                          "links", "email_ids"})
HISTORY_LIMIT = 60
CHECKPOINT_KEEP_FINISHED = 20


def persistable(output: dict[str, Any]) -> dict[str, Any]:
    """The part of a step output that may be written to disk (whitelisted keys; facts without evidence text; strings bounded)."""
    out: dict[str, Any] = {}
    for key, value in (output or {}).items():
        if key not in PERSIST_KEYS:
            continue
        if key == "fact" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "original_text"}
        elif key == "facts" and isinstance(value, list):
            value = [{k: v for k, v in f.items() if k != "original_text"} for f in value if isinstance(f, dict)][:20]
        elif isinstance(value, str):
            value = value[:200]
        elif isinstance(value, list):
            value = [v for v in value if isinstance(v, (str, int, float))][:50]
        elif not isinstance(value, (int, float, bool, dict, type(None))):
            continue
        out[key] = value
    return out


class WorkflowStore:
    def __init__(self, directory: Path | None):
        self._dir = directory
        self._lock = threading.RLock()
        self._checkpoints = JsonFile(directory / "workflow_checkpoints.json", {}) if directory else None
        self._effects = JsonFile(directory / "workflow_effects.json", {}) if directory else None
        self._links = JsonFile(directory / "workflow_links.json", []) if directory else None
        self._history = JsonFile(directory / "workflow_history.json", []) if directory else None
        self._audit = JsonLines(directory / "workflow_audit.jsonl") if directory else None
        self._mem_effects: dict[str, dict[str, Any]] = {}
        self._mem_links: list[dict[str, Any]] = []

    # ---- checkpoints ---------------------------------------------------------------------------------------------------------

    def save_checkpoint(self, wf: Workflow) -> None:
        if self._checkpoints is None:
            return
        cp = {"workflow_id": wf.workflow_id, "goal": redact(wf.goal)[:200], "template": wf.template, "status": wf.status.value, "session_id": wf.session_id, "requested_by": wf.requested_by,
              "risk": wf.risk_level.name, "scope": sorted(wf.scope), "params": {k: v for k, v in wf.params.items() if isinstance(v, (str, int, float, bool)) and k != "text"},
              "pending_step": wf.pending_step, "updated_at": time.time(), "created_at": wf.created_at,
              "completed_steps": [s.step_id for s in wf.steps if s.status in (SStatus.DONE, SStatus.SKIPPED)],
              "current_step": (wf.current.step_id if wf.current else None),
              "steps": [{"id": s.step_id, "status": s.status.value, "output": persistable(s.output), "note": s.note[:120], "idempotency_key": s.idempotency_key} for s in wf.steps],
              "facts": [f.to_dict(with_text=False) for f in wf.facts][:20]}
        with self._lock:
            def update(data):
                data = dict(data or {})
                data[wf.workflow_id] = cp
                finished = sorted((k for k, v in data.items() if v["status"] in ("COMPLETED", "FAILED", "CANCELLED")), key=lambda k: data[k]["updated_at"])
                for k in finished[:-CHECKPOINT_KEEP_FINISHED]:
                    data.pop(k, None)
                return data

            self._checkpoints.update(update)

    def load_incomplete(self) -> list[dict[str, Any]]:
        if self._checkpoints is None:
            return []
        return [cp for cp in (self._checkpoints.read() or {}).values() if cp["status"] not in ("COMPLETED", "FAILED", "CANCELLED")]

    def all_checkpoints(self) -> dict[str, Any]:
        return dict(self._checkpoints.read() or {}) if self._checkpoints is not None else {}

    def mark(self, workflow_id: str, status: WStatus, note: str = "") -> None:
        if self._checkpoints is None:
            return

        def update(data):
            data = dict(data or {})
            if workflow_id in data:
                data[workflow_id]["status"] = status.value
                data[workflow_id]["note"] = note[:160]
                data[workflow_id]["updated_at"] = time.time()
            return data

        with self._lock:
            self._checkpoints.update(update)

    # ---- side-effect ledger ----------------------------------------------------------------------------------------------------

    def effect_lookup(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._effects.read() if self._effects is not None else self._mem_effects
            return dict(data.get(key)) if key in data else None

    def effect_begin(self, key: str, kind: str, workflow_id: str) -> None:
        self._effect_write(key, {"state": "begun", "kind": kind, "object_id": None, "workflow_id": workflow_id, "at": time.time()})

    def effect_done(self, key: str, kind: str, object_id: str, workflow_id: str) -> None:
        self._effect_write(key, {"state": "done", "kind": kind, "object_id": object_id, "workflow_id": workflow_id, "at": time.time()})

    def effect_forget(self, key: str) -> None:
        with self._lock:
            if self._effects is not None:
                self._effects.update(lambda d: {k: v for k, v in (d or {}).items() if k != key})
            self._mem_effects.pop(key, None)

    def _effect_write(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            if self._effects is not None:
                self._effects.update(lambda d: {**(d or {}), key: entry})
            else:
                self._mem_effects[key] = entry

    # ---- entity links (email/event/task/reminder/document/repository/deadline/meeting) -------------------------------------------------

    def link(self, a: tuple[str, str], b: tuple[str, str], reason: str, confidence: str, source: str) -> bool:
        """Record that two entities are related, with why and how sure. Only called with an explicit basis (a shared message id, a created-from relation),
        never from vague similarity. Returns False if the same link already exists."""
        entry = {"a": list(a), "b": list(b), "reason": reason[:120], "confidence": confidence, "source": source, "at": time.time()}
        with self._lock:
            links = list(self._links.read() if self._links is not None else self._mem_links)
            if any({tuple(x["a"]), tuple(x["b"])} == {a, b} for x in links):
                return False
            links.append(entry)
            if self._links is not None:
                self._links.write(links[-500:])
            else:
                self._mem_links = links[-500:]
        return True

    def links_of(self, entity: tuple[str, str]) -> list[dict[str, Any]]:
        with self._lock:
            links = self._links.read() if self._links is not None else self._mem_links
        return [x for x in links if entity in (tuple(x["a"]), tuple(x["b"]))]

    # ---- history and audit --------------------------------------------------------------------------------------------------------

    def record_history(self, wf: Workflow) -> None:
        entry = wf.summary()
        entry = {k: entry[k] for k in ("workflow_id", "goal", "template", "status", "progress", "risk", "sources", "failure", "failure_kind", "result", "duration_s", "warnings", "requested_by")}
        entry.update({"goal": redact(entry["goal"]), "result": redact(entry["result"]), "failure": redact(entry["failure"]), "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if self._history is not None:
            with self._lock:
                self._history.update(lambda d: (list(d or []) + [entry])[-HISTORY_LIMIT:])

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        return list(reversed((self._history.read() if self._history is not None else [])[-limit:]))

    def audit(self, workflow_id: str, event: str, **fields: Any) -> None:
        """One redacted line per step/permission/confirmation event. Never a payload, token or body text."""
        if self._audit is None:
            return
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "workflow": workflow_id, "event": event}
        record.update({k: (redact(v)[:200] if isinstance(v, str) else v) for k, v in fields.items() if v not in (None, "")})
        try:
            self._audit.append(record)
        except OSError:
            pass

    def audit_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._audit.read(limit) if self._audit is not None else []


def fact_from_step_output(output: dict[str, Any]) -> Fact | None:
    d = output.get("fact")
    return Fact.from_dict(d) if isinstance(d, dict) and {"name", "value", "status", "source", "source_id"} <= set(d) else None
