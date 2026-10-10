"""Canned plan for the demo mission, used when the LLM is down, slow or returns garbage (plan §11).
The dashboard shows "fallback plan" whenever this is in use."""
from .models import Arena, StepDraft

FETCH_WORDS = ("bring", "fetch", "grab", "pick", "remove", "clear", "move it", "retrieve", "carry", "take")
ZONE_HINTS = (("charging", "charging_station"), ("left", "left_half"), ("right", "right_half"),
              ("exit", "exit"), ("test", "test_zone"))
GENERIC_TARGET = "object on the floor"


def pick_zone(text: str, arena: Arena) -> str:
    t = text.lower()
    for word, zone in ZONE_HINTS:
        if word in t and zone in arena.zones:
            return zone
    return "exit" if "exit" in arena.zones else next(iter(arena.zones), "test_zone")


def wants_fetch(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in FETCH_WORDS)


def demo_plan(text: str, arena: Arena) -> list:
    zone = pick_zone(text, arena)
    steps = [
        StepDraft(robot="drone", action="drone_scan", target=f"$zone.{zone}",
                  why=f"The drone sweeps the {zone.replace('_', ' ')} from above to find anything on the floor"),
        StepDraft(robot="drone", action="drone_dock", why="The drone lands back on the rover so the rover can drive"),
        StepDraft(robot="rover", action="rover_goto", target="$finding.f1",
                  why="The rover drives to the spot the drone flagged"),
        StepDraft(robot="rover", action="rover_search", target=GENERIC_TARGET,
                  why="The rover turns until its camera sees the object"),
        StepDraft(robot="rover", action="rover_inspect",
                  question="What is this object, and is it blocking a doorway, exit or path?",
                  why="The camera checks what the drone found"),
    ]
    if wants_fetch(text):
        steps += [
            StepDraft(robot="rover", action="rover_arm", mode="pick", why="Lower the arm to pick position"),
            StepDraft(robot="rover", action="rover_grip", mode="open", why="Open the gripper"),
            StepDraft(robot="rover", action="rover_approach", target=GENERIC_TARGET,
                      why="Creep forward using the camera until the object is inside the gripper"),
            StepDraft(robot="rover", action="rover_grip", mode="close", why="Close the gripper on the object"),
            StepDraft(robot="rover", action="rover_arm", mode="carry", why="Lift the object"),
            StepDraft(robot="rover", action="rover_check_grip", why="The camera checks that the object is held"),
            StepDraft(robot="rover", action="rover_goto", target="$home", why="Bring the object back"),
        ]
    steps.append(StepDraft(robot="manager", action="report",
                           text="Mission finished. Findings and evidence photos are on the dashboard.",
                           why="Tell the human what happened"))
    return steps
