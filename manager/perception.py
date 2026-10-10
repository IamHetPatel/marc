"""Camera-only perception (plan §6): identify, point at a target, check the gripper.

VLM distance estimates are never used for control. `point` returns where the target is in the image
(pixels); tools compare that to the calibrated grip line.
"""
from typing import Optional, Protocol

from pydantic import BaseModel

from .llm import LLMError


class Perception(Protocol):
    def point(self, jpeg: bytes, target: str) -> Optional[dict]:
        """-> {col, row, label} in image pixels, or None if the target is not visible."""

    def inspect(self, jpeg: bytes, question: str) -> dict:
        """-> {answer, label, confidence, grippable}"""

    def holding(self, jpeg: bytes) -> dict:
        """-> {holding, reason}"""


# ---------------------------------------------------------------- Gemini
class _PointItem(BaseModel):
    point: list[int]            # [y, x], normalised 0..1000 (Gemini Robotics-ER convention)
    label: str = ""


class _Points(BaseModel):
    points: list[_PointItem]


class _Inspection(BaseModel):
    answer: str
    label: str
    confidence: float
    grippable: bool


class _Holding(BaseModel):
    holding: bool
    reason: str = ""


class GeminiPerception:
    def __init__(self, llm, camera):
        self.llm, self.camera = llm, camera

    def point(self, jpeg, target):
        prompt = (f"Point to the {target} on the floor in this robot camera image. "
                  "Return JSON {\"points\": [{\"point\": [y, x], \"label\": <name>}]} with coordinates "
                  "normalised to 0-1000. Point at the object's base where it touches the floor. "
                  "If the object is not visible, return {\"points\": []}.")
        r = self.llm.generate(prompt, _Points, images=[jpeg], tag="point")
        for p in r.points:
            if len(p.point) == 2:
                y, x = p.point
                return {"col": x / 1000 * self.camera.width, "row": y / 1000 * self.camera.height,
                        "label": p.label}
        return None

    def inspect(self, jpeg, question):
        prompt = (f"You are the camera of a small ground robot. {question} "
                  "Answer as JSON: answer (one sentence), label (the main object, 1-3 words, or 'nothing'), "
                  "confidence (0-1), grippable (can a small two-finger gripper pick it up).")
        r = self.llm.generate(prompt, _Inspection, images=[jpeg], tag="inspect")
        return {"answer": r.answer, "label": r.label, "confidence": r.confidence, "grippable": r.grippable}

    def holding(self, jpeg):
        prompt = ("Look at the robot gripper at the bottom of this camera image. Is it holding an object? "
                  "Answer as JSON: holding (bool), reason (one short sentence).")
        r = self.llm.generate(prompt, _Holding, images=[jpeg], tag="holding")
        return {"holding": r.holding, "reason": r.reason}


# ---------------------------------------------------------------- Mock: ground truth from the mock world
GENERIC_WORDS = {"object", "thing", "item", "obstacle", "anything", "unknown"}


class MockPerception:
    """Reads the mock world instead of a VLM so mission tests need no network. Uses the same projection
    that rendered the image, so the grip-line servoing loop is exercised for real."""

    def __init__(self, world):
        self.world = world

    def _best(self, target: str):
        words = [w for w in target.lower().split() if w not in ("the", "a", "an", "on", "floor")]
        vis = self.world.visible()
        generic = any(w in GENERIC_WORDS for w in words)
        match = vis if generic else [(o, p) for o, p in vis if any(w in o["label"].lower() for w in words)]
        return min(match or [], key=lambda op: op[1]["f"], default=None)

    def point(self, jpeg, target):
        hit = self._best(target)
        return None if hit is None else {"col": hit[1]["col"], "row": hit[1]["row"], "label": hit[0]["label"]}

    def inspect(self, jpeg, question):
        vis = self.world.visible()
        if not vis:
            return {"answer": "The floor in front of the robot is empty.", "label": "nothing",
                    "confidence": 0.9, "grippable": False}
        o, p = min(vis, key=lambda op: op[1]["f"])
        return {"answer": f"A {o['label']} about {p['f']:.1f} m ahead.", "label": o["label"],
                "confidence": 0.9, "grippable": bool(o.get("grippable"))}

    def holding(self, jpeg):
        held = self.world.held
        return {"holding": held is not None,
                "reason": f"holding a {held['label']}" if held else "the gripper is empty"}


def make_perception(cfg, arena, llm, world=None):
    mode = cfg.perception
    if mode == "auto":
        mode = "mock" if world is not None else "gemini"
    if mode == "mock":
        if world is None:
            raise LLMError("PERCEPTION=mock needs ROVER=mock")
        return MockPerception(world)
    if llm is None:
        raise LLMError("PERCEPTION=gemini needs GEMINI_API_KEY (or use ROVER=mock for mock perception)")
    return GeminiPerception(llm, arena.camera)
