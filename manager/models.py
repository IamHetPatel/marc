"""Pydantic models: the data the planner, executor, tools and /state all agree on.

Frame: metres in the drone's Lighthouse frame. Yaw in degrees at every boundary.
"""
import json
import math
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Robot = Literal["drone", "rover", "manager"]
StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
MissionStatus = Literal["idle", "planning", "awaiting_approval", "running", "replanning",
                        "done", "failed", "stopped"]


# ---------------------------------------------------------------- geometry
class Pose(BaseModel):
    x: float
    y: float
    yaw: Optional[float] = None      # degrees, counter-clockwise from +x
    z: Optional[float] = None


class Rect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    def normalized(self) -> "Rect":
        return Rect(x0=min(self.x0, self.x1), y0=min(self.y0, self.y1),
                    x1=max(self.x0, self.x1), y1=max(self.y0, self.y1))

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        r = self.normalized()
        return r.x0 - margin <= x <= r.x1 + margin and r.y0 - margin <= y <= r.y1 + margin

    @property
    def center(self) -> tuple:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


class Finding(BaseModel):
    id: str
    x: float
    y: float
    dz: Optional[float] = None       # how far the floor dipped under the drone (m)
    by: str = "drone"
    label: Optional[str] = None
    confidence: Optional[float] = None
    image_id: Optional[str] = None
    category: Optional[str] = None           # own_drone | hostile_drone | not_a_drone | unknown (set by rover_inspect)
    hazardous: Optional[bool] = None         # None until inspected
    authorized: bool = False                 # the operator said yes to handling it
    status: Literal["found", "inspected", "recovered", "skipped"] = "found"


# ---------------------------------------------------------------- arena.json
class Zone(BaseModel):
    rect: list


class Camera(BaseModel):
    width: int = 640
    height: int = 360
    fov_deg: float = 70.0
    grip_line_row: int = 300
    deadband_px: int = 40


class MockObject(BaseModel):
    label: str
    kind: str = "unknown"            # own_drone | hostile_drone | decoy | unknown
    x: float
    y: float
    height: float = 0.15
    grippable: bool = True
    color: str = "#888888"


class Arena(BaseModel):
    calibrated: bool = False
    bounds: Rect
    drone_cage: Rect
    drone_z_max: float = 1.2
    home: Pose
    plate_offset: Pose = Pose(x=0, y=0, yaw=0)
    dock_z: float = 0.27
    zones: dict = Field(default_factory=dict)       # name -> Rect
    camera: Camera = Camera()
    mock_objects: list = Field(default_factory=list)

    def zone(self, name: str) -> Optional[Rect]:
        return self.zones.get(name)


def load_arena(path: Optional[str] = None) -> Arena:
    raw = json.loads(Path(path or Path(__file__).with_name("arena.json")).read_text())
    cage = raw["drone_cage"]
    plate = raw.get("plate_offset", {})
    return Arena(
        calibrated=raw.get("calibrated", False),
        bounds=Rect(**raw["bounds"]),
        drone_cage=Rect(**{k: cage[k] for k in ("x0", "y0", "x1", "y1")}),
        drone_z_max=cage.get("z_max", 1.2),
        home=Pose(**raw["home"]),
        plate_offset=Pose(x=plate.get("x", 0), y=plate.get("y", 0), yaw=plate.get("yaw", 0)),
        dock_z=plate.get("dock_z", 0.27),
        zones={n: Rect(x0=z["rect"][0], y0=z["rect"][1], x1=z["rect"][2], y1=z["rect"][3])
               for n, z in raw.get("zones", {}).items()},
        camera=Camera(**raw.get("camera", {})),
        mock_objects=[MockObject(**o) for o in raw.get("mock_objects", [])],
    )


# ---------------------------------------------------------------- plans
class StepDraft(BaseModel):
    """What the LLM writes for one step. Flat on purpose: Gemini's response schema has no free-form dicts.
    Fill only the fields the chosen action documents; leave the rest null."""
    robot: str
    action: str
    why: str = ""
    target: Optional[str] = None       # "$finding.f1", "$zone.exit", "$home", or an object description
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    x0: Optional[float] = None
    y0: Optional[float] = None
    x1: Optional[float] = None
    y1: Optional[float] = None
    question: Optional[str] = None
    mode: Optional[str] = None         # open|close for rover_grip, pick|carry|home for rover_arm
    text: Optional[str] = None
    finding_id: Optional[str] = None

    def args(self) -> dict:
        skip = {"robot", "action", "why"}
        return {k: v for k, v in self.model_dump().items() if k not in skip and v is not None}


class PlanDraft(BaseModel):
    summary: str = ""
    steps: list[StepDraft]


class Step(BaseModel):
    id: str
    robot: str
    action: str
    args: dict = Field(default_factory=dict)
    why: str = ""
    status: StepStatus = "pending"
    result: Optional[dict] = None


class SupervisorDecision(BaseModel):
    decision: Literal["continue", "replan", "done", "abort", "ask_human"]
    reason: str = ""
    remaining_steps: list[StepDraft] = Field(default_factory=list)


# ---------------------------------------------------------------- /state
class MissionInfo(BaseModel):
    id: Optional[str] = None
    text: str = ""
    status: MissionStatus = "idle"
    plan_version: int = 0
    fallback: bool = False
    source: Literal["ai", "fallback", "scenario", "manual"] = "ai"   # where the plan came from (additive)
    reason: str = ""                        # the AI's latest one-line explanation (additive)
    pending_question: Optional[str] = None  # set while an ask_human step waits for /change (additive)
    error: Optional[str] = None             # why the mission failed (additive)
    started_at: Optional[float] = None      # epoch seconds (additive)
    finished_at: Optional[float] = None


class Message(BaseModel):
    t: float
    role: Literal["ai", "human", "system"]
    text: str


def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


JsonDict = dict[str, Any]
