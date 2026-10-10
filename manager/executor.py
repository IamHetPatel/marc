"""Runs an approved plan one step at a time through the tools (plan §2).

Deterministic and stoppable between any two steps. The LLM supervisor is consulted only at
checkpoints, on failures, on unresolved references and when the human types a change.
Replan budget: 2 per failed step, 6 per mission (plan §4).
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from .llm import call_with_timeout
from .models import StepDraft, SupervisorDecision
from .planner import drafts_to_steps, step_to_draft
from .supervisor import RuleSupervisor, Situation, Supervisor, Trigger
from .tools import REGISTRY, Ctx, check_refs, run_action, succeeded

log = logging.getLogger("manager.executor")

SUPERVISOR_TIMEOUT_S = 25.0


@dataclass
class Budget:
    """Replans spent so far. Lives on the controller so it survives a pause for approval."""
    total: int = 0
    per_step: dict = field(default_factory=dict)       # root step id -> replans
    lineage: dict = field(default_factory=dict)        # retry step id -> root step id

    def root(self, step_id: str) -> str:
        return self.lineage.get(step_id, step_id)


class Executor:
    def __init__(self, ctx: Ctx, supervisor: Supervisor, budget: Budget,
                 set_status: Callable[..., None], take_changes: Callable[[], list]):
        self.ctx, self.store, self.cfg = ctx, ctx.store, ctx.cfg
        self.supervisor, self.budget = supervisor, budget
        self._status, self._take_changes = set_status, take_changes
        self.error: Optional[str] = None

    # ------------------------------------------------------------ main loop
    def run(self) -> str:
        """-> 'done' | 'failed' | 'stopped' | 'awaiting_approval'. `self.error` explains 'failed'."""
        outcome = self._loop()
        if outcome in ("done", "failed") and self.ctx.drone_out and not self.ctx.cancelled():
            self._land_drone()                  # never leave the drone hovering after the mission ends
        return outcome

    def _loop(self) -> str:
        ctx, store = self.ctx, self.store
        t0 = ctx.clock.now()
        while True:
            if ctx.cancelled():
                return "stopped"
            if ctx.clock.now() - t0 > self.cfg.mission_timeout_s:
                self.error = f"Mission timeout after {self.cfg.mission_timeout_s:.0f} s."
                return "failed"
            if (changes := self._take_changes()):
                if (out := self._consult(Trigger("human_change", None, " / ".join(changes)))):
                    return out
                continue
            step = store.next_pending()
            if step is None:
                return self._finish()
            if (ref := check_refs(ctx, step.action, step.args)) is not None:
                if (out := self._consult(Trigger("missing_reference", step, ref.ref))):
                    return out
                continue
            if (out := self._run_step(step)):
                return out

    def _run_step(self, step) -> Optional[str]:
        ctx, store = self.ctx, self.store
        store.update_step(step.id, status="running")
        result = run_action(ctx, step.action, step.args)
        ok = succeeded(step.action, result)
        store.update_step(step.id, status="done" if ok else "failed", result=result)
        self._track(step, result)
        if ctx.cancelled():
            return "stopped"
        step = next(s for s in store.plan if s.id == step.id)
        if not ok:
            return self._consult(Trigger("step_failed", step, str(result.get("error", ""))))
        if REGISTRY[step.action].checkpoint:
            return self._consult(Trigger("checkpoint", step))
        return None

    def _track(self, step, result: dict):
        if step.action == "rover_check_grip":
            self.store.merge_robot("rover", holding=bool(result.get("holding")))
        elif step.action == "rover_grip" and step.args.get("mode") == "open":
            self.store.merge_robot("rover", holding=False)

    def _finish(self) -> str:
        ran = [s for s in self.store.plan if s.status in ("done", "failed")]
        if ran and ran[-1].status == "failed":
            self.error = f"The last step ({ran[-1].action}) failed: {(ran[-1].result or {}).get('error', '')}"
            return "failed"
        return "done"

    def _land_drone(self):
        try:
            self.ctx.drone.land()
            self.ctx.drone_out = False
            self.store.say("system", "Mission over with the drone still in the air; it was landed.")
        except Exception as e:                              # noqa: BLE001  best effort on the way out
            log.warning("could not land drone at end of mission: %s", e)

    # ------------------------------------------------------------ supervisor
    def _situation(self, trigger: Trigger) -> Situation:
        store, b = self.store, self.budget
        step_left = self.cfg.replans_per_step - b.per_step.get(b.root(trigger.step.id), 0) if trigger.step else 99
        return Situation(mission=store.mission.text, steps=list(store.plan), findings=list(store.findings),
                         robots=dict(store.robots), drone_out=self.ctx.drone_out,
                         replans_left=max(0, min(self.cfg.replans_per_mission - b.total, step_left)))

    def _decide(self, trigger: Trigger, sit: Situation) -> SupervisorDecision:
        try:
            return call_with_timeout(lambda: self.supervisor.decide(trigger, sit), SUPERVISOR_TIMEOUT_S)
        except Exception as e:                              # noqa: BLE001  never let the supervisor kill a mission
            log.warning("supervisor failed (%s); using rules", e)
            d = RuleSupervisor().decide(trigger, sit)
            return d.model_copy(update={"reason": d.reason + " (rule-based: the AI supervisor failed)"})

    def _consult(self, trigger: Trigger) -> Optional[str]:
        """Ask the supervisor and apply its decision. Returns an outcome if the run must end, else None."""
        self._status("replanning")
        d = self._decide(trigger, self._situation(trigger))
        if self.ctx.cancelled():
            return "stopped"
        self.store.update_mission(reason=d.reason)
        if d.reason and d.decision != "ask_human":          # the ask_human step posts its own question
            self.store.say("ai", d.reason)
        self.store.event("decision", trigger=trigger.kind, decision=d.decision, reason=d.reason)
        out = self._apply(trigger, d)
        if out is None:
            self._status("running")
        return out

    def _apply(self, trigger: Trigger, d: SupervisorDecision) -> Optional[str]:
        store, b = self.store, self.budget
        failure = trigger.kind in ("step_failed", "missing_reference")
        pending = [step_to_draft(s) for s in store.plan if s.status == "pending"]

        if d.decision == "continue":
            if trigger.kind == "missing_reference" and trigger.step:       # else the same step would loop forever
                store.update_step(trigger.step.id, status="failed",
                                  result={"ok": False, "error": f"cannot resolve {trigger.detail}"})
            return None
        if d.decision == "done":
            store.skip_pending()
            return "done"
        if d.decision == "abort":
            store.skip_pending()
            self.error = d.reason or "The AI aborted the mission."
            return "failed"

        if d.decision == "ask_human":
            ask = StepDraft(robot="manager", action="ask_human", question=d.reason or "How should I continue?",
                            why="Ask the human before going on")
            retry = [step_to_draft(trigger.step)] if trigger.kind == "step_failed" and trigger.step else []
            if trigger.kind == "missing_reference":
                pending = pending[1:] if pending else pending            # the unresolvable step is dropped
            store.replace_pending(drafts_to_steps(store, [ask, *retry, *pending]))
            return None

        # replan
        if trigger.kind != "human_change":
            root = b.root(trigger.step.id) if trigger.step else "-"
            if b.total >= self.cfg.replans_per_mission or b.per_step.get(root, 0) >= self.cfg.replans_per_step:
                if failure:
                    store.skip_pending()
                    self.error = f"Replan budget used up. {d.reason}"
                    return "failed"
                return None                                               # checkpoint: keep the plan we have
            b.total += 1
            b.per_step[root] = b.per_step.get(root, 0) + 1
        new_steps = drafts_to_steps(store, d.remaining_steps)
        if trigger.kind == "step_failed" and trigger.step:
            retry = next((s for s in new_steps if s.action == trigger.step.action and s.args == trigger.step.args), None)
            if retry:
                b.lineage[retry.id] = b.root(trigger.step.id)
        store.replace_pending(new_steps)
        if trigger.kind == "human_change" and self.cfg.change_needs_approval:
            return "awaiting_approval"
        return None
