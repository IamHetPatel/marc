"""Mission text -> validated Plan. One structured-output LLM call; invalid output is re-requested once,
then we fall back to the canned plan (plan §8, §11). Nothing here moves a robot."""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .fallback_plans import demo_plan
from .llm import LLMError
from .models import Arena, PlanDraft, Step, StepDraft
from .tools import REGISTRY, validate_step, vocabulary_text

log = logging.getLogger("manager.planner")

MAX_STEPS = 30
ROVER_MOTION = {n for n, s in REGISTRY.items() if s.moves_rover}
DRONE_LAUNCH = {"drone_scan", "drone_goto"}
DRONE_RECOVER = {"drone_dock", "drone_land"}

RULES = """\
You are the mission planner of FleetPilot, a human-supervised team of two robots.

ROBOTS
- drone: a small Crazyflie. Fast. Flies only inside the flight cage. It CANNOT identify objects: it only reports
  "something sticks up from the floor at (x, y)".
- rover: a RoboMaster EP ground robot. Slow. Has a front camera, an arm and a gripper. It identifies objects with its
  camera and can pick them up.

RULES
1. Drones find, the rover verifies and fetches. Never use the rover to search a whole area when the drone can scan it.
2. The drone rides on the rover. The rover must NOT move while the drone is in the air: after drone_scan or
   drone_goto always call drone_dock (or drone_land) before any rover_goto, rover_search or rover_approach.
3. Scan hits get ids f1, f2, ... in the order of strength. They do not exist yet when you plan: refer to them as
   "$finding.f1". Use "$zone.<name>" for named zones and "$home" for the start position.
4. Always verify with rover_inspect before concluding anything. End with a report step for the human.
5. Only pick things up if the mission asks to bring, fetch, remove or move something. The pick-up sequence is:
   rover_arm pick, rover_grip open, rover_approach, rover_grip close, rover_arm carry, rover_check_grip,
   then rover_goto "$home".
6. Use only the actions listed below, with the argument names shown. Leave unused fields null.
7. 3 to 15 steps. Each step's "why" is one short sentence a non-technical person understands.
"""


@dataclass
class PlanResult:
    drafts: list              # StepDrafts: the controller assigns step ids when it commits the plan
    fallback: bool
    summary: str = ""


def drafts_to_steps(store, drafts: list) -> list:
    return [Step(id=store.next_step_id(), robot=d.robot, action=d.action, args=d.args(), why=d.why)
            for d in drafts]


def step_to_draft(step: Step) -> StepDraft:
    return StepDraft(robot=step.robot, action=step.action, why=step.why, **step.args)


def validate_drafts(drafts: list, *, drone_out: bool = False) -> list:
    """Static checks on LLM output. Returns a list of human-readable problems (empty = fine)."""
    problems = []
    if not drafts:
        return ["the plan has no steps"]
    if len(drafts) > MAX_STEPS:
        problems.append(f"too many steps ({len(drafts)} > {MAX_STEPS})")
    for i, d in enumerate(drafts, 1):
        if (msg := validate_step(d.action, d.robot, d.args())):
            problems.append(f"step {i}: {msg}")
            continue
        if d.action in DRONE_LAUNCH:
            drone_out = True
        elif d.action in DRONE_RECOVER:
            drone_out = False
        elif d.action in ROVER_MOTION and drone_out:
            problems.append(f"step {i}: {d.action} while the drone is still in the air (dock it first)")
    return problems


def arena_text(arena: Arena) -> str:
    b, c = arena.bounds, arena.drone_cage
    zones = "\n".join(f"  - {n}: x {r.x0}..{r.x1}, y {r.y0}..{r.y1}" for n, r in arena.zones.items())
    return (f"ARENA (metres, Lighthouse frame): x {b.x0}..{b.x1}, y {b.y0}..{b.y1}; the drone may only fly in "
            f"x {c.x0}..{c.x1}, y {c.y0}..{c.y1}; home = ({arena.home.x}, {arena.home.y}).\nNamed zones:\n{zones}")


def compact_plan(steps: list) -> str:
    return json.dumps([{"action": s.action, "args": s.args, "status": s.status} for s in steps])


class Planner:
    def __init__(self, llm, arena: Arena):
        self.llm, self.arena = llm, arena

    def fallback(self, text: str) -> PlanResult:
        return PlanResult(demo_plan(text, self.arena), True,
                          "Using the built-in demo plan (the AI planner was not available).")

    def plan(self, text: str, *, previous: Optional[list] = None, change: Optional[str] = None,
             robots: Optional[dict] = None) -> PlanResult:
        if self.llm is None:
            return self.fallback(text)
        prompt = self._prompt(text, previous, change, robots)
        problems: list = []
        for attempt in range(2):
            try:
                draft = self.llm.generate(prompt + self._retry_note(problems), PlanDraft, tag="plan")
            except LLMError as e:
                log.warning("planner LLM failed: %s", e)
                break
            problems = validate_drafts(draft.steps)
            if not problems:
                return PlanResult(draft.steps, False, draft.summary)
            log.warning("plan rejected (attempt %d): %s", attempt + 1, problems)
        return self.fallback(text)

    def _prompt(self, text, previous, change, robots) -> str:
        parts = [RULES, arena_text(self.arena), "ROBOT STATUS\n" + json.dumps(robots or {}),
                 "ACTIONS\n" + vocabulary_text(),
                 'Reply with JSON {"summary": <one sentence>, "steps": [...]}. Each step: robot, action, why, '
                 "and only the argument fields that action documents.",
                 f"MISSION\n{text}"]
        if previous and change:
            parts.append(f"The human saw this plan:\n{compact_plan(previous)}\nand asked for this change: {change}\n"
                         "Write the complete revised plan.")
        return "\n\n".join(parts)

    @staticmethod
    def _retry_note(problems: list) -> str:
        if not problems:
            return ""
        return "\n\nYour previous answer was rejected for these reasons; fix them:\n- " + "\n- ".join(problems)
