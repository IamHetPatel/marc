"""Thread-safe mission store. The controller/executor write, the HTTP layer reads `to_json()`.

`to_json()` is the /state contract from docs/ai-manager-plan.md §9. Fields may be added, never renamed.
"""
import threading
import time
from collections import OrderedDict
from typing import Optional

from .models import Finding, Message, MissionInfo, Step, distance

MAX_IMAGES = 80
MAX_LOG = 300
DEDUPE_M = 0.25            # a second scan hit this close to a known finding is the same object


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._mission_counter = 0
        self._step_counter = 0
        self._finding_counter = 0
        self._image_counter = 0
        self.reset()

    # ------------------------------------------------------------ lifecycle
    def reset(self):
        """Back to idle, keeping the mentor toggle and the image counter."""
        with self.lock:
            self.mission = MissionInfo()
            self.plan: list[Step] = []
            self.previous_plan: Optional[list[Step]] = None
            self.findings: list[Finding] = []
            self.messages: list[Message] = []
            self.log: list[dict] = []
            self.images: "OrderedDict[str, bytes]" = OrderedDict()
            self.robots: dict = {"rover": {"online": False}, "drone": {"online": False}}
            self.metrics: dict = {}
            self.current_step: Optional[str] = None
            self.last_image_id: Optional[str] = None
            self._finding_counter = 0
            self._step_counter = 0
            if not hasattr(self, "mentor_present"):
                self.mentor_present = False

    def new_mission(self, text: str) -> str:
        with self.lock:
            self.reset()
            self._mission_counter += 1
            self.mission = MissionInfo(id=f"m{self._mission_counter}", text=text, status="planning")
            self.say("human", text)
            return self.mission.id

    # ------------------------------------------------------------ mission fields
    def set_status(self, status: str, **fields):
        with self.lock:
            self.mission = self.mission.model_copy(update={"status": status, **fields})
            self.event("status", status=status, **fields)

    def update_mission(self, **fields):
        with self.lock:
            self.mission = self.mission.model_copy(update=fields)

    @property
    def status(self) -> str:
        with self.lock:
            return self.mission.status

    # ------------------------------------------------------------ plan
    def next_step_id(self) -> str:
        with self.lock:
            self._step_counter += 1
            return f"s{self._step_counter}"

    def set_plan(self, steps: list[Step], *, keep_previous: bool = False):
        """Install a new plan version. `keep_previous` stashes the old one for the dashboard's side-by-side."""
        with self.lock:
            self.previous_plan = [s.model_copy(deep=True) for s in self.plan] if keep_previous else None
            self.plan = steps
            self.mission = self.mission.model_copy(update={"plan_version": self.mission.plan_version + 1})
            self.event("plan", version=self.mission.plan_version, steps=len(steps))

    def replace_pending(self, new_steps: list[Step]):
        """Swap every not-yet-run step for `new_steps` (a re-plan of the remaining work)."""
        with self.lock:
            self.previous_plan = [s.model_copy(deep=True) for s in self.plan]
            kept = [s for s in self.plan if s.status in ("done", "failed", "running")]
            self.plan = kept + new_steps
            self.mission = self.mission.model_copy(update={"plan_version": self.mission.plan_version + 1})
            self.event("plan", version=self.mission.plan_version, steps=len(self.plan), replan=True)

    def update_step(self, step_id: str, **fields):
        with self.lock:
            for i, s in enumerate(self.plan):
                if s.id == step_id:
                    self.plan[i] = s.model_copy(update=fields)
                    break
            if fields.get("status") == "running":
                self.current_step = step_id
            self.event("step", id=step_id, **{k: v for k, v in fields.items() if k != "result"})

    def next_pending(self) -> Optional[Step]:
        with self.lock:
            return next((s for s in self.plan if s.status == "pending"), None)

    def skip_pending(self):
        with self.lock:
            self.plan = [s.model_copy(update={"status": "skipped"}) if s.status == "pending" else s
                         for s in self.plan]

    # ------------------------------------------------------------ findings
    def add_findings(self, raw: list, by: str = "drone") -> list[Finding]:
        """Register scan hits. Strongest dip first so f1 is the best candidate. Returns the new Findings."""
        added = []
        with self.lock:
            for r in sorted(raw, key=lambda r: -(r.get("dz") or 0)):
                if any(distance(r["x"], r["y"], f.x, f.y) < DEDUPE_M for f in self.findings):
                    continue
                self._finding_counter += 1
                f = Finding(id=f"f{self._finding_counter}", x=r["x"], y=r["y"], dz=r.get("dz"),
                            by=r.get("by", by))
                self.findings.append(f)
                added.append(f)
        return added

    def get_finding(self, finding_id: str) -> Optional[Finding]:
        with self.lock:
            return next((f for f in self.findings if f.id == finding_id), None)

    def update_finding(self, finding_id: str, **fields):
        with self.lock:
            for i, f in enumerate(self.findings):
                if f.id == finding_id:
                    self.findings[i] = f.model_copy(update=fields)

    def nearest_finding(self, x: float, y: float, within: float) -> Optional[Finding]:
        with self.lock:
            best = min(self.findings, key=lambda f: distance(x, y, f.x, f.y), default=None)
            return best if best and distance(x, y, best.x, best.y) <= within else None

    # ------------------------------------------------------------ messages, log, images
    def say(self, role: str, text: str):
        with self.lock:
            self.messages.append(Message(t=time.time(), role=role, text=text))

    def event(self, kind: str, **data):
        with self.lock:
            self.log.append({"t": time.time(), "kind": kind, **data})
            del self.log[:-MAX_LOG]

    def add_image(self, jpeg: bytes) -> str:
        with self.lock:
            self._image_counter += 1
            image_id = f"img_{self._image_counter:04d}"
            self.images[image_id] = jpeg
            while len(self.images) > MAX_IMAGES:
                self.images.popitem(last=False)
            self.last_image_id = image_id
            return image_id

    def get_image(self, image_id: str) -> Optional[bytes]:
        with self.lock:
            return self.images.get(image_id)

    # ------------------------------------------------------------ robots + metrics
    def set_robot(self, name: str, info: dict):
        with self.lock:
            self.robots[name] = info

    def set_metric(self, key: str, value):
        with self.lock:
            self.metrics[key] = value

    # ------------------------------------------------------------ the /state contract
    def to_json(self) -> dict:
        with self.lock:
            return {
                "mission": self.mission.model_dump(),
                "plan": [s.model_dump() for s in self.plan],
                "previous_plan": [s.model_dump() for s in self.previous_plan] if self.previous_plan else None,
                "current_step": self.current_step,
                "robots": dict(self.robots),
                "findings": [f.model_dump() for f in self.findings],
                "messages": [m.model_dump() for m in self.messages],
                "log": list(self.log[-60:]),
                "last_image_id": self.last_image_id,
                "mentor_present": self.mentor_present,
                "metrics": dict(self.metrics),
            }
