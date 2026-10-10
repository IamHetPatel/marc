"""Camera-only perception (plan §6): identify, point at a target, check the gripper.

VLM distance estimates are never used for control. `point` returns where the target is in the image
(pixels); tools compare that to the calibrated grip line.
"""
from typing import Literal, Optional, Protocol

from pydantic import BaseModel

from .llm import LLMError


class Perception(Protocol):
    def point(self, jpeg: bytes, target: str) -> Optional[dict]:
        """-> {col, row, label} in image pixels, or None if the target is not visible."""

    def inspect(self, jpeg: bytes, question: str) -> dict:
        """-> {answer, label, confidence, grippable, category, hazardous, reason}"""

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
    category: Literal["own_drone", "hostile_drone", "not_a_drone", "unknown"]
    hazardous: bool
    reason: str


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
        prompt = (f"You are the camera of a small ground robot that recovers crashed drones from a field. {question} "
                  "Answer as JSON: answer (one sentence), label (the main object, 1-3 words, or 'nothing'), "
                  "confidence (0-1), grippable (can a small two-finger gripper pick it up), "
                  "category (own_drone | hostile_drone | not_a_drone | unknown), hazardous (bool), "
                  "reason (one short sentence). "
                  "Visual cues our team uses: a drone with RED tape is hostile and may carry a payload; a drone "
                  "with BLUE tape is ours; a bottle, box or anything else that is not a drone is not_a_drone. "
                  "If you cannot tell what it is, category is unknown. Treat every drone that is not clearly "
                  "ours, and every unknown object, as hazardous.")
        r = self.llm.generate(prompt, _Inspection, images=[jpeg], tag="inspect")
        return {"answer": r.answer, "label": r.label, "confidence": r.confidence, "grippable": r.grippable,
                "category": r.category, "hazardous": r.hazardous, "reason": r.reason}

    def holding(self, jpeg):
        prompt = ("Look at the robot gripper at the bottom of this camera image. Is it holding an object? "
                  "Answer as JSON: holding (bool), reason (one short sentence).")
        r = self.llm.generate(prompt, _Holding, images=[jpeg], tag="holding")
        return {"holding": r.holding, "reason": r.reason}


# ---------------------------------------------------------------- Mock: ground truth from the mock world
KIND_TRIAGE = {                       # mock object kind -> (category, hazardous, reason)
    "hostile_drone": ("hostile_drone", True, "red tape: possible payload"),
    "own_drone": ("own_drone", False, "blue tape: one of ours"),
    "decoy": ("not_a_drone", False, "it is not a drone"),
    "unknown": ("unknown", True, "cannot tell what it is, so treat it as hazardous"),
}
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
            return {"answer": "The floor in front of the robot is empty.", "label": "nothing", "confidence": 0.9,
                    "grippable": False, "category": "unknown", "hazardous": False, "reason": "nothing is there"}
        o, p = min(vis, key=lambda op: op[1]["f"])
        category, hazardous, reason = KIND_TRIAGE.get(o.get("kind", "unknown"), KIND_TRIAGE["unknown"])
        return {"answer": f"A {o['label']} about {p['f']:.1f} m ahead.", "label": o["label"], "confidence": 0.9,
                "grippable": bool(o.get("grippable")), "category": category, "hazardous": hazardous,
                "reason": reason}

    def holding(self, jpeg):
        held = self.world.held
        return {"holding": held is not None,
                "reason": f"holding a {held['label']}" if held else "the gripper is empty"}


class NoPerception:
    """A real rover without GEMINI_API_KEY. Driving, the gripper, the arm and photos still work, so the floor can be
    brought up from the dashboard; only the steps that must understand the camera image fail, with this reason."""

    def __init__(self, why: str):
        self.why = why

    def point(self, jpeg, target):
        raise LLMError(self.why)

    def inspect(self, jpeg, question):
        raise LLMError(self.why)

    def holding(self, jpeg):
        raise LLMError(self.why)


def make_perception(cfg, arena, llm, world=None):
    mode = cfg.perception
    if mode == "auto":
        if world is None and llm is None:
            return NoPerception("understanding the rover camera needs GEMINI_API_KEY")
        mode = "mock" if world is not None else "gemini"
    if mode == "mock":
        if world is None:
            raise LLMError("PERCEPTION=mock needs ROVER=mock")
        return MockPerception(world)
    if llm is None:
        raise LLMError("PERCEPTION=gemini needs GEMINI_API_KEY (or use ROVER=mock for mock perception)")
    return GeminiPerception(llm, arena.camera)
