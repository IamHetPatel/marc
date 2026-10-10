"""Checkpoint / failure / human-change decisions (plan §2, §4, §11).

The LLM comes back only when there is something to decide. `RuleSupervisor` is the no-LLM backstop and
the fallback when a Gemini call fails: it keeps the mission moving with conservative, explainable rules.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from .llm import LLMError
from .models import Step, StepDraft, SupervisorDecision
from .planner import arena_text, rules_text, compact_plan, step_to_draft, validate_drafts
from .tools import vocabulary_text

log = logging.getLogger("manager.supervisor")

PICK_ACTIONS = {"rover_approach", "rover_grip", "rover_check_grip", "rover_arm", "request_authorization"}
DELIVERY_TARGETS = ("$home", "$zone.disposal")


@dataclass
class Trigger:
    kind: str                           # checkpoint | step_failed | missing_reference | human_change
    step: Optional[Step] = None
    detail: str = ""                    # error text, the unresolved reference, or the human's words


@dataclass
class Situation:
    mission: str
    steps: list                         # the whole plan with statuses and results
    findings: list
    robots: dict
    drone_out: bool
    replans_left: int


class Supervisor(Protocol):
    def decide(self, trigger: Trigger, sit: Situation) -> SupervisorDecision: ...


def _remaining(sit: Situation) -> list:
    return [step_to_draft(s) for s in sit.steps if s.status == "pending"]


def _is_pick_chain(s: Step) -> bool:
    return s.action in PICK_ACTIONS or (s.action == "rover_goto" and s.args.get("target") in DELIVERY_TARGETS)


FIELD_LABELS = {"field_a": ("field A", "field B"), "field_b": ("field B", "field A")}    # (this, the other one)


def _zone_of(step: Step) -> str:
    return str(step.args.get("target", "")).removeprefix("$zone.")


class RuleSupervisor:
    def decide(self, trigger: Trigger, sit: Situation) -> SupervisorDecision:
        step, res = trigger.step, (trigger.step.result if trigger.step else None) or {}
        keep = _remaining(sit)

        if trigger.kind == "human_change":
            return SupervisorDecision(decision="continue", reason=f"Noted: \"{trigger.detail}\". Changing a running "
                                      "plan needs the AI planner, which is not available, so the plan is unchanged.")

        if trigger.kind == "missing_reference":
            if "finding" in trigger.detail and not sit.findings:
                return SupervisorDecision(decision="done", reason="The scout found nothing, so there is nothing "
                                          "for the rover to check.")
            return SupervisorDecision(decision="abort", reason=f"The plan refers to {trigger.detail}, "
                                      "which does not exist.")

        if trigger.kind == "checkpoint" and step is not None:
            if step.action == "drone_search":
                n = len(res.get("findings", []))
                if n:
                    return SupervisorDecision(decision="continue",
                                              reason=f"The scout found {n} object(s); marking the strongest.")
                this, other = FIELD_LABELS.get(_zone_of(step), (None, None))
                if other:
                    return SupervisorDecision(decision="ask_human", reason=f"Nothing found in {this}. Search {other}?")
                return SupervisorDecision(decision="done", reason="The scout found nothing in that area.")
            if step.action == "drone_scan":
                n = len(res.get("findings", []))
                if n == 0:
                    return SupervisorDecision(decision="done", reason="The drone scan found nothing in that area.")
                return SupervisorDecision(decision="continue",
                                          reason=f"The drone flagged {n} spot(s); the rover will check the strongest.")
            if step.action == "rover_inspect":
                if res.get("category") == "not_a_drone":
                    return SupervisorDecision(decision="done", reason="Not a drone, nothing to recover.")
                if res.get("category") == "own_drone":
                    swapped = [step_to_draft(s).model_copy(update={"target": "$home"})
                               if s.action == "rover_goto" and s.args.get("target") == "$zone.disposal"
                               else step_to_draft(s)
                               for s in sit.steps if s.status == "pending" and s.action != "request_authorization"]
                    if any(s.action == "rover_goto" for s in sit.steps if s.status == "pending") \
                            and (len(swapped) != len(keep) or swapped != keep):
                        return SupervisorDecision(decision="replan", remaining_steps=swapped,
                                                  reason="It is one of ours, so the rover brings it to the base.")
                if res.get("grippable") is False and any(_is_pick_chain(s) for s in sit.steps if s.status == "pending"):
                    rest = [step_to_draft(s) for s in sit.steps if s.status == "pending" and not _is_pick_chain(s)]
                    return SupervisorDecision(decision="replan", remaining_steps=rest,
                                              reason=f"The {res.get('label', 'object')} cannot be grabbed, so the "
                                              "rover will only report it.")
                return SupervisorDecision(decision="continue", reason="")      # the tool already told the human
            if step.action == "ask_human":
                asked = re.search(r"Search (field [AB])\?", str(step.args.get("question", "")))
                if asked:                                          # the question the empty search raised
                    if str(res.get("answer", "")).strip().lower().startswith("y"):
                        zone = "field_" + asked.group(1)[-1].lower()
                        search = StepDraft(robot="drone", action="drone_search", target=f"$zone.{zone}",
                                           why=f"Search {asked.group(1)} as the operator asked")
                        return SupervisorDecision(decision="replan", remaining_steps=[search, *keep],
                                                  reason=f"Searching {asked.group(1)}.")
                    return SupervisorDecision(decision="done", reason="Search stopped at the operator's request.")
                return SupervisorDecision(decision="continue", reason="Thanks, continuing.")
            return SupervisorDecision(decision="continue", reason="")

        if trigger.kind == "step_failed" and step is not None:
            err = str(res.get("error", ""))
            if res.get("blocked") == "mentor":
                return SupervisorDecision(decision="ask_human", reason="The drone is locked until a mentor is "
                                          "present. Switch the mentor toggle on, then reply to continue.")
            if res.get("offline"):
                return SupervisorDecision(decision="abort", reason=f"The {res['offline']} is unreachable: {err}")
            if step.action == "request_authorization":
                return SupervisorDecision(decision="done", reason="The operator did not authorize it, so the "
                                          "rover leaves it where it is.")
            if step.action == "drone_dock":
                land = StepDraft(robot="drone", action="drone_land", why="Docking failed, so land the drone in place")
                return SupervisorDecision(decision="replan", remaining_steps=[land, *keep],
                                          reason=f"Docking failed ({err}); landing the drone instead.")
            if sit.replans_left > 0:
                return SupervisorDecision(decision="replan", remaining_steps=[step_to_draft(step), *keep],
                                          reason=f"{step.action} failed ({err}); trying once more.")
            return SupervisorDecision(decision="abort", reason=f"{step.action} keeps failing: {err}")

        return SupervisorDecision(decision="continue", reason="")


class LLMSupervisor:
    def __init__(self, llm, arena, fallback: Optional[Supervisor] = None, dock_mode: bool = False):
        self.llm, self.arena, self.dock_mode = llm, arena, dock_mode
        self.rules = fallback or RuleSupervisor()

    def decide(self, trigger: Trigger, sit: Situation) -> SupervisorDecision:
        if self.llm is None:
            return self.rules.decide(trigger, sit)
        prompt = self._prompt(trigger, sit)
        problems: list = []
        for _ in range(2):
            try:
                d = self.llm.generate(prompt + self._note(problems), SupervisorDecision, tag="supervisor")
            except LLMError as e:
                log.warning("supervisor LLM failed (%s); using rules", e)
                break
            problems = validate_drafts(d.remaining_steps, drone_out=sit.drone_out, dock_mode=self.dock_mode) \
                if d.decision == "replan" else []
            if d.decision == "replan" and not d.remaining_steps:
                problems = ["replan needs remaining_steps"]
            if not problems:
                return d
        out = self.rules.decide(trigger, sit)
        return out.model_copy(update={"reason": out.reason + " (rule-based: the AI supervisor was unavailable)"})

    @staticmethod
    def _note(problems: list) -> str:
        return ("\n\nYour previous answer was rejected; fix these problems:\n- " + "\n- ".join(problems)
                if problems else "")

    def _prompt(self, trigger: Trigger, sit: Situation) -> str:
        done = [{"id": s.id, "action": s.action, "args": s.args, "status": s.status, "result": s.result}
                for s in sit.steps if s.status in ("done", "failed")]
        what = {"checkpoint": "A checkpoint step just finished. Decide whether the plan still makes sense.",
                "step_failed": "A step just FAILED. Decide how to recover.",
                "missing_reference": "The next step refers to something that does not exist yet.",
                "human_change": "The human typed a change while the mission is running. Apply it."}[trigger.kind]
        return "\n\n".join([
            rules_text(self.dock_mode), arena_text(self.arena),
            "ACTIONS\n" + vocabulary_text(),
            f"MISSION\n{sit.mission}",
            f"WHAT HAPPENED\n{what}\nTrigger: {trigger.kind}. Detail: {trigger.detail or '-'}"
            + (f"\nStep: {trigger.step.action} {json.dumps(trigger.step.args)} -> {json.dumps(trigger.step.result)}"
               if trigger.step else ""),
            f"EXECUTED SO FAR\n{json.dumps(done)}",
            f"REMAINING PLAN\n{compact_plan([s for s in sit.steps if s.status == 'pending'])}",
            f"FINDINGS\n{json.dumps([f.model_dump(exclude_none=True) for f in sit.findings])}",
            f"ROBOTS\n{json.dumps(sit.robots)}   drone_in_air={sit.drone_out}   replans_left={sit.replans_left}",
            'Reply with JSON {"decision": continue|replan|done|abort|ask_human, "reason": <one sentence for the '
            'human>, "remaining_steps": [...]}.\n'
            "- continue: keep the remaining plan as is (remaining_steps empty).\n"
            "- replan: remaining_steps is the COMPLETE list of steps still to run; it replaces the remaining plan.\n"
            "- done: the goal is met or impossible to pursue further. abort: unrecoverable failure.\n"
            "- ask_human: you need information only the human has; put the question in reason.\n"
            "After rover_inspect: not_a_drone -> done (report it, do not pick it up); own_drone -> deliver to $home "
            "instead of $zone.disposal and skip request_authorization; hostile or unknown -> keep "
            "request_authorization before the pick-up; a declined authorization -> done. "
            "A drone_search that finds nothing: ask the operator whether to search the other field.\n"
            "A failed step can be retried by including it again. Do not repeat a failing step more than twice."])
