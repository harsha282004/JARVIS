"""ConversationEngine + intelligence layer: routing order, LLM independence, history hygiene, references across turns, memory relevance."""

from datetime import datetime, timedelta, timezone

from backend.core.conversation.engine import ConversationEngine
from backend.core.events import EventBus, SystemEvent
from backend.core.llm.base import LLMProvider, LLMProviderError
from tests.intelligence_helpers import NoLLM, scenario_harness


class CountingLLM(LLMProvider):
    def __init__(self, fail=False):
        self.requests, self.fail = [], fail

    def chat(self, messages, json_mode=False):
        self.requests.append(list(messages))
        if self.fail:
            raise LLMProviderError("Ollama unreachable")
        return "LLM reply"


def make(h, llm, **kw):
    clock = {"now": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    return ConversationEngine(llm, 20, 120.0, clock=lambda: clock["now"], intelligence=h.router, bus=h.bus, **kw), clock


def test_intelligence_answers_without_calling_the_llm_even_when_it_is_down(tmp_path):
    h = scenario_harness(tmp_path)
    llm = CountingLLM(fail=True)
    engine, _ = make(h, llm)
    reply = engine.respond("Plan my day")
    assert "proposed plan" in reply and llm.requests == []  # works with the LLM (and internet) unavailable


def test_unmatched_utterances_still_reach_the_llm(tmp_path):
    h = scenario_harness(tmp_path)
    llm = CountingLLM()
    engine, _ = make(h, llm)
    assert engine.respond("Tell me a joke") == "LLM reply" and len(llm.requests) == 1


def test_personal_data_replies_stay_out_of_the_llm_history(tmp_path):
    h = scenario_harness(tmp_path)
    llm = CountingLLM()
    engine, _ = make(h, llm)
    engine.respond("What's important tomorrow?")
    engine.respond("Tell me a joke")
    sent = " ".join(m.content for m in llm.requests[0])
    assert "JARVIS Project Review" not in sent and "Finish JARVIS documentation" not in sent  # titles (some written by others) never re-enter the model


def test_reference_resolves_after_a_turn_the_llm_answered(tmp_path):
    h = scenario_harness(tmp_path)
    h.service.bundle()  # graph available
    llm = CountingLLM()
    llm.chat = lambda messages, json_mode=False: "The JARVIS Project Review is a meeting you have."  # the LLM mentions the event by name
    engine, _ = make(h, llm)
    engine.respond("Tell me about it")
    assert "tomorrow at 11 AM" in engine.respond("When is it?")


def test_confirmation_flow_through_the_engine_and_session_end_cancels_it(tmp_path):
    h = scenario_harness(tmp_path)
    engine, clock = make(h, CountingLLM())
    engine.respond("Plan my day")
    assert "Shall I go ahead?" in engine.respond("add it to my calendar")
    clock["now"] += timedelta(seconds=500)  # the conversation times out
    assert engine.session is None
    assert h.calendar_client.mutations() == []
    engine.respond("Tell me a joke")
    assert engine.respond("yes") == "LLM reply" and h.calendar_client.mutations() == []  # the old confirmation did not survive


def test_confirmed_change_announces_itself_on_the_event_bus(tmp_path):
    h = scenario_harness(tmp_path)
    engine, _ = make(h, CountingLLM())
    seen = []
    h.bus.subscribe(SystemEvent.AGENT_RESPONSE, lambda e: seen.append(e.payload.get("executed")))
    engine.respond("Plan my day")
    engine.respond("add it to my calendar")
    engine.respond("yes")
    assert True in seen  # the runner can react to the change instead of polling


def test_broken_intelligence_layer_falls_back_to_normal_path(tmp_path):
    h = scenario_harness(tmp_path)

    class Broken:
        service = None

        def handle(self, *a, **k):
            raise RuntimeError("boom")

    llm = CountingLLM()
    engine = ConversationEngine(llm, 20, 120.0, intelligence=Broken())
    assert engine.respond("Plan my day") == "LLM reply"


def test_memory_reranking_drops_irrelevant_memories(tmp_path):
    from agent.memory.models import MemoryType

    h = scenario_harness(tmp_path)
    h.memory.store  # noqa: B018
    from agent.memory.models import Confidence, MemoryBasis, MemoryCandidate, MemorySource

    for text in ("User likes green tea in the morning.", "User is preparing the JARVIS demo for the review."):
        h.memory.store(MemoryCandidate(type=MemoryType.FACT, content=text, source=MemorySource.EXPLICIT_USER_STATEMENT, basis=MemoryBasis.EXPLICIT, confidence=Confidence.HIGH))
    found = h.memory.search(None)
    kept = h.service.rerank_memories("what should I do for the JARVIS review", found)
    contents = [m.content for m in kept]
    assert any("JARVIS demo" in c for c in contents) and not any("green tea" in c for c in contents)
    _ = NoLLM
