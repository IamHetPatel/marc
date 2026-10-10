"""Real Gemini calls. Skipped by default; run with:  GEMINI_API_KEY=... pytest -m live

First-hour checklist from the plan (§11): does the chosen model accept structured output + images in one call,
and how long does it take?"""
import os
import time

import pytest

from manager.config import Config
from manager.llm import GeminiLLM
from manager.models import load_arena
from manager.perception import GeminiPerception
from manager.planner import Planner, validate_drafts
from manager.runtime import build_ctx

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
                                 reason="needs GEMINI_API_KEY")]


def test_planner_returns_a_valid_plan_for_the_demo_mission():
    arena = load_arena()
    llm = GeminiLLM(Config.from_env())
    t0 = time.time()
    result = Planner(llm, arena).plan("Check the test zone for anything blocking the exit.")
    print(f"\nplan latency {time.time() - t0:.1f}s, fallback={result.fallback}, {len(result.drafts)} steps")
    assert not result.fallback, "the LLM plan was rejected; see runs/ for the raw response"
    assert validate_drafts(result.drafts) == []
    assert result.drafts[0].robot == "drone"


def test_structured_output_with_an_image_in_one_call():
    cfg = Config.from_env()
    ctx, world = build_ctx(Config(mock_time_scale=5000.0, perception="mock"))
    jpeg = ctx.rover.snapshot()
    perception = GeminiPerception(GeminiLLM(cfg), ctx.arena.camera)
    t0 = time.time()
    out = perception.inspect(jpeg, "What is in front of the robot?")
    print(f"\ninspect latency {time.time() - t0:.1f}s -> {out}")
    assert set(out) == {"answer", "label", "confidence", "grippable"}
