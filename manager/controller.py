"""Mission state machine (plan §4). Owns the planning and execution threads.

idle -> planning -> awaiting_approval -> running <-> replanning -> done | failed
and `stopped` from anywhere. Stop never goes through the LLM.

Every background job captures the cancel Event of the epoch it was started in. Anything that finishes
after its epoch was cancelled (stop, restarted planning, new mission) discards its result, so a slow LLM
call can never overwrite newer state.
"""
import dataclasses
import json
import logging
import threading
from pathlib import Path
from typing import Optional

import yaml

from .adapters.base import RobotError
from .detector import Target
from .executor import Budget, Executor
from .llm import call_with_timeout
from .planner import Planner, drafts_to_steps, validate_drafts
from .scenarios import load_scenario
from .supervisor import Supervisor
from .tools import run_action, succeeded

log = logging.getLogger("manager.controller")

IDLE_STATES = ("idle", "done", "failed", "stopped")
MONITOR_PERIOD_S = 0.5
HEALTH_EVERY = 6                 # robot-server /health every 6 polls (3 s): battery, camera, Lighthouse


class Conflict(Exception):
    """The request does not fit the current state (HTTP 409)."""


class StaleApproval(Conflict):
    """/approve named a plan version that is no longer the current one (HTTP 409)."""


class InvalidPlan(ValueError):
    """A submitted plan failed validation (HTTP 422). `problems` says what is wrong, in plain words."""

    def __init__(self, problems: list):
        super().__init__("; ".join(problems))
        self.problems = problems


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

    def submit_plan(self, title: str, drafts: list, source: str = "manual") -> str:
        """Same as start_mission, minus the planner: the steps come from the caller (a scenario file or the
        dashboard). Validated first, so a bad plan never becomes a mission. Approve works as usual."""
        problems = validate_drafts(drafts, dock_mode=self.cfg.dock_mode)
        if problems:
            raise InvalidPlan(problems)
        with self._lock:
            if self.store.status not in IDLE_STATES:
                raise Conflict(f"a mission is already {self.store.status}; stop or reset it first")
            self._new_epoch()
            self.budget = Budget()
            mission_id = self.store.new_mission(title)
            self.store.restart_step_ids()
            self.store.set_plan(drafts_to_steps(self.store, drafts))
            self.store.set_status("awaiting_approval", source=source, fallback=False, pending_question=None)
            version = self.store.mission.plan_version
        if self.auto_approve:
            try:
                self.approve(version)
            except Conflict:
                pass
        return mission_id

    def load_scenario(self, name: str) -> str:
        """Register the scenario's targets (detector, and the mock world if there is one), then submit its scripted
        plan, or start an AI mission from its sentence. Raises scenarios.UnknownScenario."""
        sc = load_scenario(name)
        with self._lock:
            if self.store.status not in IDLE_STATES:
                raise Conflict(f"a mission is already {self.store.status}; stop or reset it first")
            self.base_ctx.detector.set_targets([Target(id=t.id, kind=t.kind, source=t.source, x=t.x, y=t.y)
                                                for t in sc.targets])
            if self.world is not None and sc.targets:
                with self.world.lock:
                    self.world.held = None
                    self.world.objects[:] = [t.mock_object() for t in sc.targets]
        if sc.plan:
            return self.submit_plan(sc.title, sc.plan, source="scenario")
        return self.start_mission(sc.mission)

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
            self._write_report()
        return result

    def reset(self):
        with self._lock:
            if self.store.status in ("running", "replanning"):
                raise Conflict("stop the mission before resetting")
            self._new_epoch()
            self.store.reset()

    def build_report(self) -> dict:
        """The handover report for the current mission: what was asked, what was found and decided, what ran."""
        st = self.store.to_json()
        m = st["mission"]
        return {"mission": {k: m[k] for k in ("id", "text", "status", "source", "plan_version", "fallback",
                                              "reason", "error")},
                "started": m["started_at"], "finished": m["finished_at"],
                "findings": st["findings"],
                "steps": [{k: s[k] for k in ("id", "robot", "action", "args", "why", "status", "result")}
                          for s in st["plan"]],
                "messages": st["messages"], "metrics": st["metrics"]}

    def _write_report(self):
        """runs/<mission_id>/report.yaml. An older report with the same id (a previous server run) is kept."""
        if self.store.mission.id is None:
            return
        try:
            report = json.loads(json.dumps(self.build_report(), default=str))
            folder = Path(self.cfg.runs_dir) / self.store.mission.id
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "report.yaml"
            if path.exists():
                path.rename(folder / f"report.{int(path.stat().st_mtime)}.yaml")
            tmp = folder / "report.yaml.tmp"                # readers never see a half-written report
            tmp.write_text(yaml.safe_dump(report, sort_keys=False, allow_unicode=True))
            tmp.replace(path)
            self.store.event("report", path=str(path))
        except (OSError, yaml.YAMLError) as e:
            log.warning("could not write the mission report: %s", e)

    def rover_at_home(self):
        """The rover was placed on the start cross: re-anchor its position there (wheel odometry drifts)."""
        with self._lock:
            if self.store.status in ("running", "replanning"):
                raise Conflict("stop the mission before moving the rover's position")
            self.base_ctx.loc.reanchor_home()
            self.store.say("system", "Rover position reset to the start cross.")

    def rover_anchor(self, cf: str) -> dict:
        """A Crazyflie ('target' or 'scout') sits on the rover's top plate, centred, nose to the rover's front:
        its Lighthouse pose becomes the rover's pose. This puts rover and drone in the same frame."""
        if cf not in ("target", "scout"):
            raise ValueError("cf must be 'target' or 'scout'")
        with self._lock:
            if self.store.status in ("running", "replanning"):
                raise Conflict("stop the mission before moving the rover's position")
        try:
            p = self.base_ctx.drone.pose(cf=cf)
        except RobotError as e:
            raise Conflict(f"cannot read the {cf} Crazyflie: {e}") from e
        if not p.get("ok", True) or not isinstance(p.get("x"), (int, float)):
            raise Conflict(f"the {cf} Crazyflie has no position: {p.get('error', 'no pose')}")
        if p.get("state") == "flying":
            raise Conflict(f"the {cf} Crazyflie is flying; put it on the rover's top plate first")
        yaw = float(p.get("yaw") or 0.0)
        self.base_ctx.loc.anchor(float(p["x"]), float(p["y"]), yaw)
        msg = f"Rover position set from the {cf} Crazyflie: ({p['x']:.2f}, {p['y']:.2f}), heading {yaw:.0f}°."
        self.store.say("system", msg)
        return {"x": p["x"], "y": p["y"], "yaw": yaw}

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
        if self.base_ctx.overhead is not None:
            self.base_ctx.overhead.halt()

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
                                  source="fallback" if result.fallback else "ai", pending_question=None)
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
                self._write_report()
            elif outcome == "awaiting_approval":
                self.store.set_status("awaiting_approval")
                self.store.say("ai", "The plan was changed. Review the new plan and approve it to continue.")
            else:
                self.store.set_status("failed", error=executor.error, pending_question=None)
                self.store.say("ai", f"Mission failed: {executor.error}")
                self._write_report()

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
        self._polls = 0

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
            if d.get("ok", True) is False:          # the server answers, but its pose is not trustworthy (tracking lost)
                store.merge_robot("drone", online=True, pose_ok=False, pose_error=d.get("error"),
                                  state=d.get("state"), battery_v=d.get("battery_v"))
            else:
                store.merge_robot("drone", online=True, pose_ok=True, pose_error=None,
                                  pose={k: d.get(k) for k in ("x", "y", "z")},
                                  state=d.get("state"), battery_v=d.get("battery_v"), home=d.get("home"))
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
        if ctx.overhead is not None:                # status only; a dead webcam is not a reason to stop a mission
            store.merge_robot("overhead", **ctx.overhead.status())
        for name in ("rover", "drone"):
            rtt = getattr(getattr(ctx, name), "rtt_ms", None)
            if rtt is not None:
                store.set_metric(f"{name}_rtt_ms", rtt)
        if self._polls % HEALTH_EVERY == 0:
            self.poll_health()
        self._polls += 1

    def poll_health(self):
        """Robot-server status for the dashboard only: a failing /health never trips the watchdog (pose does that)."""
        ctx, store = self.ctrl.base_ctx, self.ctrl.store
        for name in ("rover", "drone"):
            health = getattr(getattr(ctx, name), "health", None)
            if health is None:
                continue
            try:
                h = health()
            except RobotError:
                continue
            store.merge_robot(name, health=_brief_health(name, h))

    def _fail(self, name: str):
        self.ctrl.store.merge_robot(name, online=False)
        self.fails[name] += 1
        if self.fails[name] >= self.ctrl.cfg.watchdog_fails and self.ctrl.store.status in ("running", "replanning"):
            self.fails[name] = 0
            self.ctrl.stop(reason=f"Watchdog: the {name} stopped answering")


def _brief_health(name: str, h: dict) -> dict:
    """The few /health fields the dashboard shows, so /state stays small."""
    if name == "rover":
        return {"battery_pct": h.get("battery"), "camera": h.get("camera"), "last": h.get("last"),
                "mock": bool(h.get("mock")), "video": h.get("camera") == "live",
                "error": h.get("error") if h.get("ok") is False else None}
    lh = h.get("scout_lighthouse") or {}
    return {"link": h.get("scout"), "target": h.get("target"), "mock": bool(h.get("mock")),
            "lighthouse": lh.get("status"), "base_stations": lh.get("used"), "hint": lh.get("hint")}
