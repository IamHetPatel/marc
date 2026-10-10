"""GeminiLLM's model fallback chain and run logging, with a fake SDK client (no network)."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from manager.config import Config
from manager.llm import GeminiLLM, LLMError, ScriptedLLM, call_with_timeout
from manager.models import SupervisorDecision


class FakeModels:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        out = self.behaviour(model)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out)


def make(tmp_path, behaviour, latencies=None):
    g = object.__new__(GeminiLLM)                          # skip __init__: no SDK client, no API key
    g.client = SimpleNamespace(models=FakeModels(behaviour))
    g.models = ["er-2", "er-1.5", "flash"]
    g.on_latency = latencies.append if latencies is not None else None
    g._first, g._n, g._lock = 0, 0, threading.Lock()
    g._run_dir = tmp_path / "run"
    return g


OK = json.dumps({"decision": "continue", "reason": "fine", "remaining_steps": []})


def test_falls_through_the_chain_and_sticks_to_the_working_model(tmp_path):
    lat = []
    g = make(tmp_path, lambda m: RuntimeError("404 model not found") if m == "er-2" else OK, lat)
    assert g.generate("p", SupervisorDecision).decision == "continue"
    assert g.client.models.calls == ["er-2", "er-1.5"] and len(lat) == 1
    g.generate("p", SupervisorDecision)
    assert g.client.models.calls[2:] == ["er-1.5"]         # no more wasted calls to the dead model


def test_invalid_json_counts_as_a_failure_of_that_model(tmp_path):
    g = make(tmp_path, lambda m: "not json" if m == "er-2" else OK)
    assert g.generate("p", SupervisorDecision).reason == "fine"
    assert g.client.models.calls == ["er-2", "er-1.5"]


def test_all_models_failing_raises_one_clear_error(tmp_path):
    g = make(tmp_path, lambda m: RuntimeError(f"{m} down"))
    with pytest.raises(LLMError) as e:
        g.generate("p", SupervisorDecision)
    assert all(m in str(e.value) for m in ("er-2", "er-1.5", "flash"))


def test_every_call_is_logged_with_prompt_response_and_images(tmp_path):
    g = make(tmp_path, lambda m: OK)
    g.generate("the prompt", SupervisorDecision, images=[b"\xff\xd8img"], tag="supervisor")
    files = sorted(p.name for p in (tmp_path / "run").iterdir())
    assert files == ["001_supervisor.json", "001_supervisor_0.jpg"]
    rec = json.loads((tmp_path / "run" / "001_supervisor.json").read_text())
    assert rec["prompt"] == "the prompt" and rec["model"] == "er-2" and rec["latency_ms"] >= 0 and rec["error"] is None


def test_scripted_llm_rejects_the_wrong_shape():
    with pytest.raises(LLMError):
        ScriptedLLM(["just a string"]).generate("p", SupervisorDecision)


def test_call_with_timeout():
    assert call_with_timeout(lambda: 7, 1.0) == 7
    with pytest.raises(TimeoutError):
        call_with_timeout(lambda: time.sleep(1.0), 0.05)
    with pytest.raises(ZeroDivisionError):
        call_with_timeout(lambda: 1 / 0, 1.0)
