"""Canned plan for the recovery mission, used when the LLM is down, slow or returns garbage (ai-manager-plan §11).
The dashboard shows "fallback" as the plan source whenever this is in use."""
from .models import Arena, StepDraft

FETCH_WORDS = ("bring", "fetch", "grab", "pick", "remove", "clear", "move it", "retrieve", "carry", "take",
               "recover", "dispose", "disposal", "collect")
ZONE_HINTS = (("field b", "field_b"), ("field_b", "field_b"), ("field a", "field_a"), ("field_a", "field_a"),
              ("test", "test_zone"))
GENERIC_TARGET = "crashed drone"


def pick_zone(text: str, arena: Arena) -> str:
    t = text.lower()
    for word, zone in ZONE_HINTS:
        if word in t and zone in arena.zones:
            return zone
    return "field_a" if "field_a" in arena.zones else next(iter(arena.zones), "test_zone")


def wants_fetch(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in FETCH_WORDS)


def demo_plan(text: str, arena: Arena, dock_mode: bool = False) -> list:
    """Search, mark, drive there, triage; and when the mission asks for it, authorize, pick up, deliver.
    Delivery goes to the disposal zone; the rule supervisor redirects our own drone to $home after the inspection.
    With docking nothing may fly while the rover drives, so the scout lands first and the mark is skipped."""
    zone = pick_zone(text, arena)
    steps = [StepDraft(robot="drone", action="drone_search", target=f"$zone.{zone}",
                       why=f"The scout flies a search pattern over {zone.replace('_', ' ')} looking for the crashed drone")]
    if dock_mode:
        steps.append(StepDraft(robot="drone", action="drone_dock",
                               why="The scout lands back on the rover so the rover can drive"))
    else:
        steps.append(StepDraft(robot="drone", action="drone_mark", target="$finding.f1",
                               why="The scout hovers over what it found so everyone can see it"))
    steps.append(StepDraft(robot="rover", action="rover_goto", target="$finding.f1",
                           why="The rover drives to the spot (and stops 0.6 m short)"))
    if not dock_mode:
        steps.append(StepDraft(robot="drone", action="drone_home", why="The scout flies back to its launch pad and lands, out of the rover's way"))
    steps += [
        StepDraft(robot="rover", action="rover_search", target=GENERIC_TARGET,
                  why="The rover turns until its camera sees it"),
        StepDraft(robot="rover", action="rover_inspect",
                  question="What is in front of the robot? Is it a drone, and is it ours or hostile?",
                  why="The camera checks what it is"),
    ]
    if wants_fetch(text):
        steps += [
            StepDraft(robot="manager", action="request_authorization", finding_id="f1",
                      why="A human must approve before the rover touches anything hazardous"),
            StepDraft(robot="rover", action="rover_arm", mode="pick", why="Lower the arm"),
            StepDraft(robot="rover", action="rover_grip", mode="open", why="Open the gripper"),
            StepDraft(robot="rover", action="rover_approach", target=GENERIC_TARGET,
                      why="Creep forward on the camera until it is inside the gripper"),
            StepDraft(robot="rover", action="rover_grip", mode="close", why="Close the gripper on it"),
            StepDraft(robot="rover", action="rover_arm", mode="carry", why="Lift it"),
            StepDraft(robot="rover", action="rover_check_grip", why="The camera checks that it is held"),
            StepDraft(robot="rover", action="rover_goto", target="$zone.disposal", why="Carry it to the disposal zone"),
            StepDraft(robot="rover", action="rover_grip", mode="open", why="Release it"),
            StepDraft(robot="rover", action="rover_arm", mode="home", why="Stow the arm"),
        ]
    steps.append(StepDraft(robot="manager", action="report",
                           text="Mission finished. Findings and evidence photos are on the dashboard.",
                           why="Tell the human what happened"))
    return steps
