"""Mission state machine (plan §4). Owns the planning and execution threads.

idle -> planning -> awaiting_approval -> running <-> replanning -> done | failed
and `stopped` from anywhere. Stop never goes through the LLM.

Every background job captures the cancel Event of the epoch it was started in. Anything that finishes
after its epoch was cancelled (stop, restarted planning, new mission) discards its result, so a slow LLM
call can never overwrite newer state.
"""
import dataclasses
import logging
import threading
from typing import Optional

from .adapters.base import RobotError
from .executor import Budget, Executor
from .llm import call_with_timeout
from .planner import Planner, drafts_to_steps
from .supervisor import Supervisor
from .tools import run_action, succeeded

log = logging.getLogger("manager.controller")

IDLE_STATES = ("idle", "done", "failed", "stopped")
MONITOR_PERIOD_S = 0.5


class Conflict(Exception):
    """The request does not fit the current state (HTTP 409)."""


class StaleApproval(Conflict):
    """/approve named a plan version that is no longer the current one (HTTP 409)."""


class Controller:
    def __init__(self, ctx, planner: Planner, supervisor: Supervisor, auto_approve: bool = False):
        self.base_ctx, self.store, self.cfg = ctx, ctx.store, ctx.cfg
        self.planner, self.supervisor, self.auto_approve = planner, supervisor, auto_approve
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._changes: list = []
        self._answer: Optional[str] = None
        self._answer_event = threading.Event()
        self.budget = Budget()
        self._manual_busy = False
        self.monitor = Monitor(self)
        self.world = None                       # set by runtime.build_controller when anything is mocked

    # ------------------------------------------------------------ public API (what server.py calls)
    def start_mission(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("mission text is empty")
        with self._lock:
            if self.store.status not in IDLE_STATES:
                raise Conflict(f"a mission is already {self.store.status}; stop or reset it first")
            self._new_epoch()
            self.budget = Budget()
            mission_id = self.store.new_mission(text)
            self._spawn_plan(text, None, None)
            return mission_id

    def approve(self, plan_version: int):
        with self._lock:
            if self.store.status != "awaiting_approval":
                raise Conflict(f"nothing to approve (state is {self.store.status})")
            if plan_version != self.store.mission.plan_version:
                raise StaleApproval(f"plan changed: you approved v{plan_version}, "
                                    f"the current plan is v{self.store.mission.plan_version}")
            self._start_run()

    def change(self, text: str):
        text = text.strip()
        if not text:
            raise ValueError("change text is empty")
        with self._lock:
            st = self.store.status
            if st == "awaiting_approval":
                previous = list(self.store.plan)
                self._new_epoch()
                self.store.say("human", text)
                self.store.set_status("planning")
                self._spawn_plan(self.store.mission.text, previous, text)
            elif st == "planning":
                self._new_epoch()                                  # restart planning with the extra instruction
                merged = f"{self.store.mission.text}. {text}"
                self.store.say("human", text)
                self.store.update_mission(text=merged)
                self._spawn_plan(merged, None, None)
            elif st in ("running", "replanning"):
                self.store.say("human", text)
                if self.store.mission.pending_question is not None:
                    self._answer = text
                    self._answer_event.set()                       # it answers an ask_human step
                else:
                    self._changes.append(text)                     # applied after the current atomic step
            else:
                raise Conflict(f"no active mission to change (state is {st})")

    def manual(self, action: str, args: dict):
        """Run one tool right now, through the same validation and safety as a plan step (POST /manual).
        Only between missions, one at a time. `stop()` cancels it."""
        with self._lock:
            if self.store.status not in IDLE_STATES:
                raise Conflict(f"manual control is for between missions (state is {self.store.status})")
            if self._manual_busy:
                raise Conflict("a manual action is still running")
            if self._cancel.is_set():
                self._new_epoch()                  # a Stop spends the epoch; manual control needs a fresh one
            ctx = dataclasses.replace(self.base_ctx, cancel=self._cancel, ask=None, drone_out=self._drone_in_air())
            record = self.store.start_manual(action, args)
            self._manual_busy = True
        threading.Thread(target=self._manual_job, args=(ctx, action, args, record), daemon=True).start()

    def _manual_job(self, ctx, action: str, args: dict, record: dict):
        result = run_action(ctx, action, args)
        with self._lock:
            self._manual_busy = False
            self.store.finish_manual(record, "done" if succeeded(action, result) else "failed", result)

    def stop(self, reason: str = "Emergency stop") -> dict:
        """Works from any state. Safety first: cancel, halt the robots, then update the state."""
        with self._lock:
            self._cancel.set()
            self._answer_event.set()
            self._changes.clear()
        result = self.base_ctx.safety.stop_all()
        with self._lock:
            self.store.set_status("stopped", pending_question=None)
            self.store.say("system", f"{reason}. Rover halted, drone landing.")
        return result

    def reset(self):
        with self._lock:
            if self.store.status in ("running", "replanning"):
                raise Conflict("stop the mission before resetting")
            self._new_epoch()
            self.store.reset()

    def set_mentor(self, present: bool):
        self.store.mentor_present = bool(present)
        self.store.event("mentor", present=bool(present))
        self.store.say("system", "Mentor present: drone unlocked." if present else "Mentor left: drone locked.")

    def start(self):
        """Start the telemetry monitor. Safe to call more than once."""
        if self.monitor.ident is None:
            self.monitor.start()

    def shutdown(self):
        self.monitor.halt()
        self._cancel.set()

    # ------------------------------------------------------------ planning
    def _new_epoch(self):
        self._cancel.set()                    # whatever was running for the previous epoch must wind down
        self._cancel = threading.Event()
        self._answer_event.clear()

    def _spawn_plan(self, text: str, previous: Optional[list], change: Optional[str]):
        threading.Thread(target=self._plan_job, args=(self._cancel, text, previous, change), daemon=True).start()

    def _plan_job(self, cancel: threading.Event, text: str, previous: Optional[list], change: Optional[str]):
        robots = dict(self.store.robots)
        try:
            result = call_with_timeout(
                lambda: self.planner.plan(text, previous=previous, change=change, robots=robots),
                self.cfg.plan_timeout_s)
        except TimeoutError:
            self.store.say("system", f"The planner took longer than {self.cfg.plan_timeout_s:.0f} s; "
                                     "using the built-in plan.")
            result = self.planner.fallback(text)
        except Exception as e:                # noqa: BLE001  planning must always end in a plan or a clear failure
            log.exception("planning crashed")
            result = self.planner.fallback(text)
            self.store.event("planner_error", error=str(e))
        with self._lock:
            if cancel.is_set():
                return
            self.store.restart_step_ids()
            self.store.set_plan(drafts_to_steps(self.store, result.drafts), keep_previous=previous is not None)
            self.store.set_status("awaiting_approval", fallback=result.fallback, reason=result.summary,
                                  pending_question=None)
            if result.summary:
                self.store.say("ai", result.summary)
            version = self.store.mission.plan_version
        if self.auto_approve:
            try:
                self.approve(version)
            except Conflict:
                pass

    # ------------------------------------------------------------ running
    def _start_run(self):
        """Caller holds the lock and has checked the state."""
        if self._manual_busy:
            raise Conflict("a manual action is still running; wait for it or press Stop")
        cancel = self._cancel
        run_ctx = dataclasses.replace(self.base_ctx, cancel=cancel, ask=self._ask,
                                      drone_out=self._drone_in_air())
        self.store.set_status("running", pending_question=None)
        self.store.say("system", "Plan approved. Running.")
        executor = Executor(run_ctx, self.supervisor, self.budget,
                            set_status=lambda s, **kw: self._run_status(cancel, s, **kw),
                            take_changes=self._take_changes)
        threading.Thread(target=self._run_job, args=(cancel, executor), daemon=True).start()

    def _drone_in_air(self) -> bool:
        try:
            return self.base_ctx.drone.pose().get("state") == "flying"
        except RobotError:
            return False

    def _run_status(self, cancel: threading.Event, status: str, **fields):
        with self._lock:
            if not cancel.is_set():
                self.store.set_status(status, **fields)

    def _run_job(self, cancel: threading.Event, executor: Executor):
        try:
            outcome = executor.run()
        except Exception as e:                # noqa: BLE001  a bug must end the mission visibly, not hang it
            log.exception("executor crashed")
            outcome, executor.error = "failed", f"Internal error: {e}"
        with self._lock:
            if cancel.is_set():
                return                          # stop() or a restart owns the final state
            if outcome == "done":
                self.store.set_status("done", pending_question=None)
                self.store.say("ai", "Mission complete.")
            elif outcome == "awaiting_approval":
                self.store.set_status("awaiting_approval")
                self.store.say("ai", "The plan was changed. Review the new plan and approve it to continue.")
            else:
                self.store.set_status("failed", error=executor.error, pending_question=None)
                self.store.say("ai", f"Mission failed: {executor.error}")

    def _take_changes(self) -> list:
        with self._lock:
            out, self._changes = self._changes, []
            return out

    def _ask(self, question: str) -> Optional[str]:
        """Executor-thread side of ask_human: block until /change delivers the answer or the mission is cancelled."""
        cancel = self._cancel
        with self._lock:
            self._answer = None
            self._answer_event.clear()
            self.store.update_mission(pending_question=question)
        while not cancel.is_set() and not self._answer_event.wait(0.2):
            pass
        with self._lock:
            self.store.update_mission(pending_question=None)
            answer, self._answer = self._answer, None
        return None if cancel.is_set() else answer


class Monitor(threading.Thread):
    """Polls the robots for /state and runs the watchdog: a robot that stops answering while a mission is
    moving triggers stop_all (plan §8)."""

    def __init__(self, ctrl: Controller):
        super().__init__(daemon=True, name="monitor")
        self.ctrl, self._halt = ctrl, threading.Event()
        self.fails = {"rover": 0, "drone": 0}

    def halt(self):
        self._halt.set()

    def run(self):
        while not self._halt.wait(MONITOR_PERIOD_S):
            try:
                self.poll()
            except Exception:                    # noqa: BLE001  telemetry must never die
                log.exception("monitor poll failed")

    def poll(self):
        ctx, store = self.ctrl.base_ctx, self.ctrl.store
        try:
            p = ctx.loc.rover_pose()
            store.merge_robot("rover", online=True, pose=p.as_dict(), pose_source=p.source,
                              pose_age_s=round(p.age_s, 1))
            self.fails["rover"] = 0
        except RobotError:
            self._fail("rover")
        try:
            d = ctx.drone.pose()
            store.merge_robot("drone", online=True, pose={k: d.get(k) for k in ("x", "y", "z")},
                              state=d.get("state"), battery_v=d.get("battery_v"))
            self.fails["drone"] = 0
        except RobotError:
            self._fail("drone")
        try:                                        # ground truth for the dashboard; never feeds the watchdog
            t = ctx.drone.pose(cf="target")
            if t.get("ok", True) is False:
                store.merge_robot("target", online=False)
            else:
                store.merge_robot("target", online=True, pose={k: t.get(k) for k in ("x", "y", "z")})
        except RobotError:
            store.merge_robot("target", online=False)
        for name in ("rover", "drone"):
            rtt = getattr(getattr(ctx, name), "rtt_ms", None)
            if rtt is not None:
                store.set_metric(f"{name}_rtt_ms", rtt)

    def _fail(self, name: str):
        self.ctrl.store.merge_robot(name, online=False)
        self.fails[name] += 1
        if self.fails[name] >= self.ctrl.cfg.watchdog_fails and self.ctrl.store.status in ("running", "replanning"):
            self.fails[name] = 0
            self.ctrl.stop(reason=f"Watchdog: the {name} stopped answering")
