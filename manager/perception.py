"""Camera-only perception (plan §6): identify, point at a target, check the gripper.

VLM distance estimates are never used for control. `point` returns where the target is in the image
(pixels); tools compare that to the calibrated grip line.
"""
import math
from typing import Literal, Optional, Protocol

from pydantic import BaseModel

from .adapters.mock import HORIZON
from .llm import LLMError
from .scenarios import DEFAULT_LABEL


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


# ---------------------------------------------------------------- Map: positions instead of pixels (no AI)
class MapPerception:
    """No camera understanding at all: the camera steps answer from POSITIONS. The rover's pose (riding Crazyflie,
    Lighthouse) and the wreck's known spot (the scenario's fixed target, else the marked finding) are projected
    through the same camera model the mock uses, so search, approach and the grip check still steer the real rover,
    on coordinates. For a floor demo without GEMINI_API_KEY; every answer says it came from the map, not the image."""

    metric = True                # rover_approach lines the gripper up on positions instead of pixels

    def __init__(self, arena):
        self.arena, self.camera = arena, arena.camera
        self.loc = self.detector = self.store = None      # bound by runtime.build_ctx

    def bind(self, ctx):
        self.loc, self.detector, self.store = ctx.loc, ctx.detector, ctx.store

    def _objects(self) -> list:
        """[{x, y, kind}] where things are believed to be: scenario targets first, else the findings."""
        out = []
        if self.detector is not None and hasattr(self.detector, "truth"):
            kinds = {t.id: t.kind for t in getattr(self.detector, "targets", [])}
            out = [{"x": t["x"], "y": t["y"], "kind": kinds.get(t["id"], "unknown")} for t in self.detector.truth()]
        if not out and self.store is not None:
            out = [{"x": f.x, "y": f.y, "kind": "unknown"} for f in self.store.findings if f.status != "recovered"]
        return out

    def nearest(self, pose) -> Optional[dict]:
        """The believed object nearest to the rover's gripper: {x, y, kind}, or None."""
        reach = self.arena.rover.grip_reach_m
        gx = pose.x + reach * math.cos(math.radians(pose.yaw))
        gy = pose.y + reach * math.sin(math.radians(pose.yaw))
        return min(self._objects(), key=lambda o: math.hypot(o["x"] - gx, o["y"] - gy), default=None)

    def _ahead(self):
        """[(obj, forward_m, left_m)] in the rover frame, nearest first."""
        pose = self.loc.rover_pose()
        c, s = math.cos(math.radians(pose.yaw)), math.sin(math.radians(pose.yaw))
        rows = []
        for o in self._objects():
            dx, dy = o["x"] - pose.x, o["y"] - pose.y
            rows.append((o, c * dx + s * dy, -s * dx + c * dy))
        return sorted(rows, key=lambda r: math.hypot(r[1], r[2]))

    def point(self, jpeg, target):
        cam = self.camera
        half = math.radians(cam.fov_deg / 2)
        for o, f, l in self._ahead():
            if f < 0.05 or abs(math.atan2(l, f)) > half:
                continue
            horizon = cam.height * HORIZON
            row = min(cam.height, horizon + (cam.grip_line_row - horizon) * self.arena.rover.grip_reach_m / f)
            col = cam.width / 2 - (l / f) / math.tan(half) * (cam.width / 2)
            return {"col": col, "row": row, "label": f"{DEFAULT_LABEL.get(o['kind'], 'object')} (map)"}
        return None

    def inspect(self, jpeg, question):
        near = [(o, f, l) for o, f, l in self._ahead() if math.hypot(f, l) <= 1.0]
        if not near:
            return {"answer": "Map mode: nothing is marked within 1 m of the rover.", "label": "nothing",
                    "confidence": 1.0, "grippable": False, "category": "unknown", "hazardous": False,
                    "reason": "no marked object nearby (camera not analysed)"}
        o, f, l = near[0]
        kind = o["kind"] if o["kind"] in KIND_TRIAGE else "unknown"
        category, hazardous, reason = KIND_TRIAGE[kind]
        label = DEFAULT_LABEL.get(kind, "object")
        return {"answer": f"Map mode: the marked {label} is {math.hypot(f, l):.2f} m from the rover "
                          f"(category from the scenario; the photo is kept but not analysed).",
                "label": label, "confidence": 1.0, "grippable": True, "category": category,
                "hazardous": hazardous, "reason": f"{reason} (from the scenario, not the camera)"}

    def holding(self, jpeg):
        reach = self.arena.rover.grip_reach_m
        near = [(f, l) for _, f, l in self._ahead() if abs(f - reach) <= 0.12 and abs(l) <= 0.1]
        return {"holding": bool(near),
                "reason": ("map mode: the object was in gripper reach when it closed" if near
                           else "map mode: no marked object within gripper reach") + " (camera not analysed)"}


def make_perception(cfg, arena, llm, world=None):
    mode = cfg.perception
    if mode == "auto":
        if world is None and llm is None:
            return MapPerception(arena)      # real rover, no key: steer on positions so the demo still runs
        mode = "mock" if world is not None else "gemini"
    if mode == "map":
        return MapPerception(arena)
    if mode == "none":
        return NoPerception("PERCEPTION=none: the camera steps are switched off")
    if mode == "mock":
        if world is None:
            raise LLMError("PERCEPTION=mock needs ROVER=mock")
        return MockPerception(world)
    if llm is None:
        raise LLMError("PERCEPTION=gemini needs GEMINI_API_KEY (or use ROVER=mock for mock perception)")
    return GeminiPerception(llm, arena.camera)
