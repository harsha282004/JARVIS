# Intelligence engine

`agent/intelligence/`. The facade is `IntelligenceService`; `IntelligenceRouter` recognizes what the user asked; the conversation engine calls
the router after the task executor's own confirmation and before the agent brain.

```
utterance -> ConfirmationEngine.respond (an answer to a pending confirmation?)
          -> preference statements -> "add it to my calendar" -> patterns (focus, importance on a day, plan, why, source, conflicts,
             prepare, project status, timeline, dependencies, briefings, "tell me more", references, acknowledge)
          -> otherwise None: normal path (agent brain + tools + LLM)
```
It is deterministic and **makes no LLM call** (`MeteredLLM` counts calls; a test uses an LLM that fails if touched). It therefore keeps working
when Ollama or the internet is down. Replies contain the user's own titles (some written by other people), so, like Gmail replies, they are kept out
of the conversation history (a placeholder is stored instead).

## What it answers

| You say | It does |
|---|---|
| "What's important tomorrow?" | calendar + due tasks + deadlines mentioned by email + findings (preparation, missing calendar entry, conflicts), facts first |
| "What should I focus on today?" | facts (schedule, due tasks with status), then "You may want to start with ..." and an offer of a work block |
| "Plan my day / tomorrow" | a proposed plan; changes nothing (see `PLANNING_ENGINE.md`) |
| "Add it to my calendar" | asks for confirmation naming every block; "yes" creates them and reads them back |
| "Why did you schedule that?" / "Why are you telling me this?" | the recorded evidence, with sources; never hidden reasoning |
| "Where did you get that?" | sources of the last answer |
| "Any conflicts?" | overlaps, source disagreements, several deadlines a day, reminder collisions (facts only, no ranking) |
| "Prepare me for tomorrow's project review" | read-only workflow: event, project, tasks, documents, emails, conflicts, checklist (a suggestion) |
| "What is pending for my JARVIS project?" | tasks, upcoming events, documents, related emails |
| "What happened with my project yesterday?" | only what the activity timeline recorded; says so if nothing |
| "X depends on Y" / "What is blocking X?" | stores/reads task dependencies |
| "Good morning", "evening review", "tell me more" | briefings (TODAY, PRIORITIES, DEADLINES, IMPORTANT, EVENTS, ATTENTION); real counts only |
| "When is it due?" after talking about something | resolves "it" from the conversation (see below) |
| "Don't notify me about newsletters", "show my preferences" | preference store |

## Statement kinds

Every sentence is a `Statement` with a kind: FACT (retrieved), EXTRACTED (parsed from text: "An email mentions ..."), INFERENCE ("appears
related"), SUGGESTION ("You may want to ..."), ACTION ("I created ..."; only after a verified result). Wording follows the kind.

## Conversation references

`ConversationContext` records which of the user's entities recent turns mentioned (found by matching names in what was said *and* answered,
including turns the LLM answered). "it/that/this project/the meeting/the email/that hackathon/the previous task" resolve to one entity; if two
different things were just discussed it asks ("Do you mean X or Y?"). Context expires after 30 minutes. With nothing to refer to, the question
falls through to the normal path.

## Memory relevance

`relevance.rank_memories` scores retrieved memories by semantic overlap, recency (90-day half-life), importance, source (explicit > inferred),
active context and graph relationships, and drops memories with no link to the request before they reach the LLM.

## Budget

Deterministic first, retrieve before generating, cache extraction, skip unchanged sources. Measured: 0 LLM calls; a cold answer re-reads sources in ~3 ms
(fake sources).
