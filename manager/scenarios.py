"""Scenario files (docs/history/mothership-plan.md WP-M5): a hand-written plan and/or a mission sentence, plus the ground
truth of what lies in the field. They are how the demo works without any LLM (plan source "scenario").

The file name (without .yaml) is the scenario's id, the thing `POST /scenarios/{name}/load` takes.
"""
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from .models import StepDraft

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
SLUG = re.compile(r"[A-Za-z0-9_-]+")

DEFAULT_LABEL = {"hostile_drone": "crashed drone", "own_drone": "own drone", "decoy": "plastic bottle"}
DEFAULT_COLOR = {"hostile_drone": "#c0392b", "own_drone": "#2471a3", "decoy": "#7f8c8d"}


class ScenarioTarget(BaseModel):
    id: str
    kind: str = "hostile_drone"            # own_drone | hostile_drone | decoy
    source: str = "fixed"                  # live: pose from the target Crazyflie; fixed: use x, y
    x: float
    y: float                               # for 'live' this is where the mock world puts the object
    label: Optional[str] = None
    color: Optional[str] = None
    height: float = 0.05
    grippable: bool = True

    def mock_object(self) -> dict:
        return {"label": self.label or DEFAULT_LABEL.get(self.kind, "object"), "kind": self.kind, "x": self.x,
                "y": self.y, "height": self.height, "grippable": self.grippable,
                "color": self.color or DEFAULT_COLOR.get(self.kind, "#888888")}


class Scenario(BaseModel):
    name: str
    title: str
    mission: str = ""                      # used for an AI mission when there is no scripted plan
    targets: list[ScenarioTarget] = Field(default_factory=list)
    plan: Optional[list[StepDraft]] = None
    arena: Optional[str] = None            # "mock" or "floor": the field its coordinates belong to (None: any)


class UnknownScenario(KeyError):
    pass


def _path(name: str) -> Path:
    if not SLUG.fullmatch(name):
        raise UnknownScenario(name)
    return SCENARIO_DIR / f"{name}.yaml"


def load_scenario(name: str) -> Scenario:
    path = _path(name)
    if not path.is_file():
        raise UnknownScenario(name)
    raw = yaml.safe_load(path.read_text()) or {}
    return Scenario(**{**raw, "name": name})


def list_scenarios(arena_kind: Optional[str] = None) -> list:
    """Every scenario, or only those that fit `arena_kind` (a scenario's coordinates belong to one field)."""
    out = []
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        sc = load_scenario(path.stem)
        if arena_kind and sc.arena and sc.arena != arena_kind:
            continue
        out.append({"name": sc.name, "title": sc.title, "has_plan": bool(sc.plan), "arena": sc.arena})
    return out
