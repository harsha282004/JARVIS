#!/usr/bin/env python
"""Measure the Integration Hub on synthetic data (no network, no real accounts): sync times, duplicate detection, store latency, context update, resource use.

    python scripts/benchmark_hub.py

The external services are in-memory fakes, so these numbers are the cost of JARVIS's own work (parsing, extraction, normalization, storage, diffing). Real API latency
(Gmail, Calendar, GitHub) depends on the network and is NOT included. The store is SQLite in memory; PostgreSQL timings were not measured.
"""

import json
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.sysmetrics import CpuMeter, working_set_mb  # noqa: E402
from tests.calendar_helpers import cal_event  # noqa: E402
from tests.hub_helpers import build_hub_harness  # noqa: E402
from tests.intelligence_helpers import NOW, email_raw, ist  # noqa: E402


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return round((time.perf_counter() - start) * 1000, 2), result


def main() -> int:
    work = Path(tempfile.mkdtemp())
    emails = [email_raw(f"m{i}", f"Project update {i}", f"Your review {i} is scheduled for October {1 + i % 25} at 11 AM. Please submit the report by Friday.", hours_ago=i % 48) for i in range(50)]
    events = [cal_event(f"e{i}", f"Meeting {i}", ist(25, 9 + i % 8, month=9 + i // 30, minute=i % 60 % 2 * 30) + timedelta(days=i % 30), ist(25, 10 + i % 8, month=9 + i // 30, minute=i % 60 % 2 * 30) + timedelta(days=i % 30)) for i in range(100)]
    h = build_hub_harness(work, emails=emails, calendar_events=events)
    docs = h.docs_dir
    for i in range(300):
        (docs / f"note{i}.txt").write_text(f"note {i}: deadline October {1 + i % 25}", encoding="utf-8")

    rss0, cpu = working_set_mb(), CpuMeter()
    cpu.start()
    out: dict = {}
    out["gmail_sync_50_emails_cold_ms"], r = timed(lambda: h.sync("gmail"))
    out["gmail_items_created"] = r.created
    out["gmail_sync_incremental_no_new_mail_ms"], r = timed(lambda: h.sync("gmail"))
    out["calendar_sync_100_events_cold_ms"], r = timed(lambda: h.sync("calendar"))
    out["calendar_sync_unchanged_ms"], r = timed(lambda: h.sync("calendar"))
    out["github_sync_cold_ms"], r = timed(lambda: h.sync("github"))
    out["github_sync_unchanged_ms"], r = timed(lambda: h.sync("github"))
    total_ms, passes = 0.0, 0
    while True:  # each sync indexes at most `limit` files (bounded work); keep going until the folder is fully indexed
        ms, r = timed(lambda: h.sync("documents"))
        total_ms, passes = total_ms + ms, passes + 1
        if r.created + r.updated == 0 or passes > 20:
            break
    out["documents_index_300_files_total_ms"], out["documents_index_passes"] = round(total_ms, 1), passes
    out["documents_scan_300_files_unchanged_ms"], r = timed(lambda: h.sync("documents"))
    for i in range(20):
        (docs / f"note{i}.txt").write_text(f"note {i}: deadline moved", encoding="utf-8")
    out["documents_scan_20_of_300_changed_ms"], r = timed(lambda: h.sync("documents"))

    items = h.hub.repo.search("", limit=100)
    out["store_upsert_new_item_avg_ms"] = round(sum(timed(lambda i=i: h.hub.repo.upsert(type(items[0])(items[0].kind, "bench", f"b{i}", items[0].timestamp, f"bench {i}", "", {}, "high", None, items[0].retrieved_at)))[0] for i in range(200)) / 200, 3)
    out["duplicate_detection_upsert_unchanged_avg_ms"] = round(sum(timed(lambda: h.hub.repo.upsert(items[0]))[0] for _ in range(200)) / 200, 3)
    out["context_update_with_hub_ms"], _ = timed(lambda: (setattr(h.base.service._collector, "_hub", h.hub), h.base.service._invalidate(), h.base.service.bundle()))
    out["tool_search_email_ms"], _ = timed(lambda: h.hub.tools.call("search_email", {"query": "review"}))
    out["tool_search_all_stored_ms"], _ = timed(lambda: h.hub.tools.call("search_all", {"query": "meeting"}))
    out["voice_question_github_ms"], _ = timed(lambda: h.say("Show my repositories"))
    # background cost: twenty forced sync cycles of everything
    cycle_ms, _ = timed(lambda: [h.hub.engine.sync_due() or [h.sync(n) for n in ("gmail", "calendar", "github")] for _ in range(20)])
    out["twenty_full_sync_cycles_ms"] = cycle_ms
    out["cpu_percent_of_one_core_during_benchmark"] = cpu.stop()
    out["memory_mb_before_after"] = [round(rss0 or 0), round(working_set_mb() or 0)]
    out["rows_in_store"] = h.hub.repo.count()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
