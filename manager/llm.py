"""The one place that talks to Gemini. Structured output only, no automatic function calling (plan §11).

* model fallback chain: MODEL, then the ER 1.5 preview, then a Flash model; the first that answers sticks
* every call is logged to runs/<timestamp>/NNN_<tag>.json (+ images) for the technical-depth story and metrics
* ScriptedLLM replays canned responses so mission tests are deterministic and free
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """No model in the chain produced a valid answer."""


class LLM(Protocol):
    def generate(self, prompt: str, schema: type, images: list = (), tag: str = "llm",
                 temperature: float = 0.2) -> BaseModel: ...


def call_with_timeout(fn: Callable, timeout: float):
    """Run fn() in a daemon thread; raise TimeoutError if it takes longer. The straggler is abandoned, so callers
    must treat its eventual result as stale."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:           # re-raised in the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout:.0f} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def have_api_key() -> bool:
    """AI=off hides the key: built-in plans, rule supervisor, and the rover camera answers from the map."""
    if os.getenv("AI", "on").lower() in ("off", "0", "false", "no"):
        return False
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


class GeminiLLM:
    def __init__(self, cfg, on_latency: Optional[Callable[[int], None]] = None):
        from google import genai            # imported here so mock-only runs never need the SDK configured
        self.genai = genai
        self.client = genai.Client()
        self.models = [cfg.model, *cfg.model_fallbacks]
        self.on_latency = on_latency
        self._first = 0                      # index of the first model worth trying (sticky)
        self._n = 0
        self._lock = threading.Lock()
        self._run_dir = Path(cfg.runs_dir) / time.strftime("%Y%m%d-%H%M%S")

    def generate(self, prompt, schema, images=(), tag="llm", temperature=0.2):
        from google.genai import types
        parts = [types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images] + [prompt]
        config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema,
                                             temperature=temperature)
        errors = []
        for i in range(self._first, len(self.models)):
            model = self.models[i]
            t0 = time.monotonic()
            try:
                resp = self.client.models.generate_content(model=model, contents=parts, config=config)
                out = schema.model_validate_json(resp.text)
            except Exception as e:           # network, quota, unknown model, or JSON that fails validation
                errors.append(f"{model}: {e}")
                self._log(tag, model, prompt, images, None, (time.monotonic() - t0) * 1000, str(e))
                continue
            ms = int((time.monotonic() - t0) * 1000)
            self._first = i
            self._log(tag, model, prompt, images, resp.text, ms, None)
            if self.on_latency:
                self.on_latency(ms)
            return out
        raise LLMError("; ".join(errors) or "no models configured")

    def _log(self, tag, model, prompt, images, response, ms, error):
        try:
            with self._lock:
                self._n += 1
                n = self._n
            self._run_dir.mkdir(parents=True, exist_ok=True)
            names = []
            for j, img in enumerate(images):
                names.append(f"{n:03d}_{tag}_{j}.jpg")
                (self._run_dir / names[-1]).write_bytes(img)
            (self._run_dir / f"{n:03d}_{tag}.json").write_text(json.dumps(
                {"model": model, "latency_ms": round(ms), "prompt": prompt, "response": response,
                 "error": error, "images": names}, indent=2))
        except OSError:
            pass                              # logging must never break a mission


class ScriptedLLM:
    """Returns queued responses in order. An entry may be a model instance, an Exception to raise,
    or a callable(prompt, schema) -> instance. `calls` records (tag, prompt) for assertions."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, schema, images=(), tag="llm", temperature=0.2):
        self.calls.append((tag, prompt))
        if not self.responses:
            raise LLMError("ScriptedLLM exhausted")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            r = r(prompt, schema)
        if not isinstance(r, schema):
            raise LLMError(f"ScriptedLLM expected {schema.__name__}, got {type(r).__name__}")
        return r


def make_llm(cfg, on_latency=None) -> Optional[LLM]:
    """GeminiLLM when a key is configured, else None (planner then uses the canned plan)."""
    return GeminiLLM(cfg, on_latency) if have_api_key() else None
