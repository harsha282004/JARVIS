# Workflow templates (Phase 22)

A template is a fixed, reviewed graph of steps over the operator tools (`workflows/templates.py`). The planner picks one from the user's words and fills its parameters. Optional systems appear only when available; a missing *required* system refuses the plan.

Step notation: `tool(args)` ← `depends on`. `?` = optional (failure is a warning). References use `From(step, path)`. Risk is READ_ONLY unless stated. **Bold** steps write.

## Operator tools

| Tool | System | What it does |
|---|---|---|
| `gmail_search_important` / `gmail_search(query)` | Gmail | important/unread emails (importance classification and reasons); search by words |
| `gmail_extract_deadlines(emails, topic?)` | Gmail | dated statements → `Fact`s with provenance and status |
| `verify_deadline_fact(facts)` | local | the one VERIFIED/HIGH_CONFIDENCE deadline, or the reason there is none; several different dates → asks which |
| `calendar_compare_fact(fact)` | Calendar | a same-named event on another day → conflict question (never resolved silently) |
| `compute_reminder_time(fact, days_before, hour)` | local | fact date − N days at 09:00, must be in the future |
| **`task_create(fact, title?)`** / **`task_create_batch(facts)`** | Tasks | deduplicated, provenance in metadata, read back, entity link (LOW_RISK) |
| **`reminder_create(at, fact, message?)`** | Reminders | deduplicated, provenance in metadata, read back (LOW_RISK) |
| `calendar_events(day)` / `gmail_related_emails(events)` | Calendar / Gmail | the day's events; emails matching the events' titles |
| `tasks_overview(days)` / `deadlines_overview(days)` | Tasks (+Gmail) | open/overdue tasks; task due dates + actionable dates found in email |
| `github_identity` / `github_activity(login, days)` | GitHub | which account; open issues/PRs and commit count in *that* account's repositories |
| `documents_search` / `documents_deadlines(query)` / `documents_requirements(query)` | Documents | provenance = document id + page; dates; "required documents" lists |
| `memory_context(query)` | Memory | context only |
| `email_links(emails)` | Gmail | validated links, most application-like first |
| `briefing_compose(...)` | local | priority synthesis with reasons + the spoken briefing |
| `notify_user(text, priority, key)` | Notifications | deduplicated announcement (Do-Not-Disturb by the Phase 19 policy) |
| `compose_workflow_report(kind)` | local | final wording from what the steps returned |
| `open_url`, `read_page`, `find_element`, `click_element`, `upload_file`, `get_page_state` | Browser | through the Phase 21 `ToolRouter` (schemas, code-computed risk, PermissionManager) |

## Templates

| Template | Example | Steps |
|---|---|---|
| `important_email_review` | "Check my important emails and tell me what needs attention today" | `gmail_search_important` → `compose(important_emails)`. Scope: Gmail only. |
| `deadline_to_task` | "Find the internship email and create a task for the deadline" | `gmail_search(topic)` → `gmail_extract_deadlines` → `verify_deadline_fact` → `calendar_compare_fact`? → **`task_create`** |
| `deadline_to_reminder` | "Remind me two days before the deadline in the internship email" | …verify → `calendar_compare_fact`? → `compute_reminder_time(days_before)` → **`reminder_create`** |
| `deadline_task_and_reminder` | "Find the internship email, identify the deadline, create a task for it, and remind me two days before" | …verify → `calendar_compare_fact`? → **`task_create`**; `compute_reminder_time` → **`reminder_create`** (independent branches: a passed reminder time does not block the task, and the result says so) |
| `email_deadlines_to_tasks` | "Turn the deadlines in my important emails into tasks" | `gmail_search_important` → `gmail_extract_deadlines` → **`task_create_batch`** (non-actionable facts are listed as skipped, with why) |
| `meeting_preparation` | "Check tomorrow's calendar and related emails…", "Prepare for tomorrow's meetings and notify me" | `calendar_events(day)` → `gmail_related_emails`? → `compose(meeting_prep)` → `notify_user`? |
| `daily_briefing` | "Give me my morning briefing" | `calendar_events(today)`?, `gmail_search_important`?, `tasks_overview`?, `deadlines_overview`? → `briefing_compose` (optional references degrade to empty; missing sources are named) → `notify_user`? |
| `weekly_review` | "Weekly review" | `tasks_overview`?, `deadlines_overview`?, `gmail_search_important`? → `compose(weekly)` |
| `deadlines_review` | "Check my upcoming deadlines and tell me what I need to finish this week" | `deadlines_overview(days)`, `tasks_overview`? → `compose(deadlines)` |
| `github_activity_review` | "Find my GitHub activity from this week and add anything important to my task list" | `github_identity` → `github_activity(login, days)` → **`task_create_batch`**? (items become tasks with **no** due date: an issue is not a deadline) → `compose(github)` |
| `document_deadline_review` | "Find the scholarship deadline in my documents and create a task for it" | `documents_deadlines(topic)` → `verify_deadline_fact` → **`task_create`**? → `compose(documents)` |
| `email_to_browser` | "Open the link in the internship email" | `gmail_search` → `email_links` → `open_url(url)` → `read_page` |
| `application_submit` | "Apply for the internship from my email" | `gmail_search` → `email_links` → `open_url` → `read_page` → `documents_requirements`? → `memory_context`? → `click_element("Submit")` — **EXTERNAL_EFFECT/SENSITIVE: stops and asks with a grouped preview; nothing is submitted without the yes** |

Follow-ups from the previous fact (no new search): `fact_to_task`, `fact_to_reminder`, `fact_task_and_reminder` ("Turn that into a reminder two days before").

## Verification rules

`readback` — the tool re-read the created object and it matches (title, due date, status). `has:<key>` — the output contains the key. Every write step uses `readback`; "Done" is only said when all of them held.

## Adding a template

1. Add a builder in `templates.py` using only operator/browser tools and `From` references to declared dependencies; register a `TemplateDef` (required and optional systems, `writes`).
2. Add its grammar to `planner._match` (specific first; be careful not to steal ordinary requests — extend `test_ordinary_requests_are_left_to_the_other_routers`).
3. Add report wording in `compose` if it needs one, and a test in `tests/workflows/`.
