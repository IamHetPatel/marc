"""The action vocabulary (plan §5). Each tool validates its args, checks safety, calls an adapter,
writes to the state log and RETURNS A DICT. It never raises into the planner.

The planner prompt is generated from the docstrings here (`vocabulary_text`), so the prompt cannot drift
from the code. Write docstrings for the model: units, frame, what it returns.

Cross-cutting rules are enforced once, in `run_action`, not in each tool:
  * args are validated against the spec
  * mentor gate for drone tools
  * no rover motion while the drone is out (plan §7)
  * fault injection (MOCK_FAIL)
"""
import inspect
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .adapters.base import RobotError
from .detector import search_waypoints
from .models import Rect, distance

log = logging.getLogger("manager.tools")

REGISTRY: dict = {}

APPROACH_STEP_M = 0.12          # forward step per iteration of the visual-servo loop (plan §6: 10-15 cm)
APPROACH_MAX_ITERS = 15
APPROACH_MAX_S = 60.0
SEARCH_TURNS = 8                # 8 x 45 deg = one full circle
GOTO_TIMEOUT_S = 60.0
GOTO_GAIN = 0.8
GOTO_MIN_SPEED = 0.08           # m/s: slower commands do not turn the EP's wheels, so it would stall short of the goal
GOTO_STALL_S = 4.0              # no progress (1 cm) for this long: settle if close enough, else report it as stuck
INSPECT_FINDING_RADIUS_M = 1.5
UNFLAGGED_AHEAD_M = 0.6         # rover_inspect registers an unflagged object this far in front of the rover
GRIP_GATE_RADIUS_M = 1.0        # a hazardous, unauthorized finding this close blocks closing the gripper
RECOVERED_RADIUS_M = 1.5


LOCATION_ACTIONS = {"rover_goto", "drone_scan", "drone_mark", "drone_search"}     # args["target"] is a place


class UnresolvedRef(Exception):
    def __init__(self, kind: str, ref: str):
        super().__init__(f"cannot resolve {ref!r} ({kind})")
        self.kind, self.ref = kind, ref


@dataclass
class ActionSpec:
    name: str
    robot: str
    fn: Callable
    needs: tuple = ()                 # alternatives; one tuple of arg names must be fully present
    choices: dict = field(default_factory=dict)
    ok_key: Optional[str] = None      # result key that must be truthy for the step to count as succeeded
    checkpoint: bool = False          # supervisor looks at the result before the next step
    moves_rover: bool = False
    mentor: bool = False
    fault: Optional[str] = None       # MOCK_FAIL key

    @property
    def doc(self) -> str:
        return inspect.getdoc(self.fn) or ""


def action(robot: str, **kw):
    def deco(fn):
        REGISTRY[fn.__name__] = ActionSpec(name=fn.__name__, robot=robot, fn=fn, **kw)
        return fn
    return deco


@dataclass
class Ctx:
    """Everything a tool may touch. Built once per run by runtime.py."""
    cfg: object
    arena: object
    store: object
    rover: object
    drone: object
    loc: object
    perception: object
    safety: object
    clock: object
    faults: object = None
    cancel: threading.Event = field(default_factory=threading.Event)
    ask: Optional[Callable[[str], Optional[str]]] = None    # controller's ask_human hook
    drone_out: bool = False
    detector: object = None                                 # SimDetector, or OverheadDetector with the webcam
    overhead: object = None                                 # OverheadCamera (manager/overhead.py), if installed

    def cancelled(self) -> bool:
        return self.cancel.is_set()


def _err(msg: str, **extra) -> dict:
    return {"ok": False, "error": msg, **extra}


def _norm(res: dict) -> dict:
    """Robot servers may return arbitrary JSON; make sure a boolean `ok` is there."""
    res = dict(res)
    res["ok"] = res.get("ok", True) is not False and "error" not in res
    return res


# ---------------------------------------------------------------- references and validation
def resolve_location(ctx: Ctx, value) -> dict:
    """'$finding.f1' | '$zone.exit' | '$home' | 'x,y' | {x, y} -> {x, y, rect, kind}. Raises UnresolvedRef."""
    if isinstance(value, dict):
        return {"x": value["x"], "y": value["y"], "rect": None, "kind": "point"}
    ref = str(value).strip()
    name = ref[1:] if ref.startswith("$") else ref
    if name == "home":
        h = ctx.arena.home
        return {"x": h.x, "y": h.y, "rect": None, "kind": "home"}
    fid = name.split(".", 1)[1] if name.startswith("finding.") else (name if re.fullmatch(r"f\d+", name) else None)
    if fid:
        f = ctx.store.get_finding(fid)
        if f is None:
            raise UnresolvedRef("finding", ref)
        return {"x": f.x, "y": f.y, "rect": None, "kind": "finding"}
    zone = ctx.arena.zone(name[5:] if name.startswith("zone.") else name)
    if zone is not None:
        cx, cy = zone.center
        return {"x": cx, "y": cy, "rect": zone.normalized(), "kind": "zone"}
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", name)
    if m:
        return {"x": float(m.group(1)), "y": float(m.group(2)), "rect": None, "kind": "point"}
    raise UnresolvedRef("unknown", ref)


def _location(ctx: Ctx, args: dict) -> dict:
    if args.get("target") is not None:
        return resolve_location(ctx, args["target"])
    return {"x": args["x"], "y": args["y"], "rect": None, "kind": "point"}


def check_refs(ctx: Ctx, name: str, args: dict) -> Optional[UnresolvedRef]:
    """Used by the executor before running a step: a reference that does not resolve yet needs a supervisor
    checkpoint, not a guess."""
    if name in LOCATION_ACTIONS and args.get("target") is not None:
        try:
            resolve_location(ctx, args["target"])
        except UnresolvedRef as e:
            return e
    return None


def validate_args(spec: ActionSpec, args: dict) -> Optional[str]:
    if spec.needs and not any(all(args.get(k) is not None for k in group) for group in spec.needs):
        want = " or ".join("+".join(g) for g in spec.needs)
        return f"{spec.name} needs {want}"
    for key, allowed in spec.choices.items():
        if key in args and args[key] not in allowed:
            return f"{spec.name}: {key} must be one of {', '.join(allowed)}"
    return None


def validate_step(action_name: str, robot: str, args: dict) -> Optional[str]:
    """Plan-time check used by the planner/supervisor on LLM output."""
    spec = REGISTRY.get(action_name)
    if spec is None:
        return f"unknown action {action_name!r}"
    if robot != spec.robot:
        return f"{action_name} belongs to robot {spec.robot!r}, not {robot!r}"
    return validate_args(spec, args)


def vocabulary_text() -> str:
    return "\n\n".join(f"{s.name}  (robot: {s.robot})\n{s.doc}" for s in REGISTRY.values())


def succeeded(name: str, result: dict) -> bool:
    spec = REGISTRY[name]
    return result.get("ok", True) is not False and (spec.ok_key is None or bool(result.get(spec.ok_key)))


# ---------------------------------------------------------------- the dispatcher
def _authorization_gate(ctx: Ctx) -> Optional[str]:
    """Closing the gripper on something hazardous needs the operator's yes. Enforced here, in code, so it holds for
    scripted plans, AI plans and manual control alike (a prompt can be talked out of it, this cannot)."""
    pose = ctx.loc.rover_pose()
    f = ctx.store.nearest_finding(pose.x, pose.y, GRIP_GATE_RADIUS_M)
    if f is not None and f.hazardous and not f.authorized:
        return f"not authorized: {f.id} ({f.category or 'unknown'}) is hazardous; ask the operator first"
    return None


def _separation_violation(ctx: Ctx, name: str, args: dict) -> Optional[str]:
    """Without docking nothing rides on the rover, so the rule is geometric: a drone flying lower than the mark
    height must stay `separation_m` (x/y) away from wherever the rover is going. Fails open if a robot does not
    answer; the watchdog owns that case."""
    try:
        d = ctx.drone.pose()
        if d.get("state") != "flying" or d.get("z", 99.0) >= ctx.cfg.mark_z - 0.1:
            return None
        if name == "rover_goto":
            loc = _location(ctx, args)
            dest = (loc["x"], loc["y"])
        else:                                       # turning or creeping in place: it stays where it is
            p = ctx.loc.rover_pose()
            dest = (p.x, p.y)
    except (RobotError, UnresolvedRef):
        return None
    gap = distance(dest[0], dest[1], d["x"], d["y"])
    if gap < ctx.cfg.separation_m:
        return (f"the scout is flying low (z={d['z']:.2f} m) only {gap:.2f} m from where the rover is going; "
                f"raise it to {ctx.cfg.mark_z} m (drone_mark) or land it first")
    return None


def run_action(ctx: Ctx, name: str, args: dict) -> dict:
    spec = REGISTRY.get(name)
    if spec is None:
        return _err(f"unknown action {name!r}")
    if (msg := validate_args(spec, args)):
        return _err(msg)
    if ctx.cancelled():
        return _err("cancelled")
    if spec.mentor and (msg := ctx.safety.mentor_gate()):
        return _err(msg, blocked="mentor")
    if spec.moves_rover:
        if ctx.cfg.dock_mode:
            if ctx.drone_out:
                return _err("the drone is out; dock or land it before moving the rover", blocked="drone_out")
        elif (msg := _separation_violation(ctx, name, args)):
            return _err(msg, blocked="separation")
    if spec.fault and ctx.faults is not None and ctx.faults.hit(spec.fault):
        return _err(f"injected failure ({spec.fault})", injected=True)
    try:
        if name == "rover_grip" and args.get("mode") == "close" and (msg := _authorization_gate(ctx)):
            return _err(msg, blocked="authorization")
        return spec.fn(ctx, args)
    except UnresolvedRef as e:
        return _err(str(e), unresolved=e.kind)
    except RobotError as e:
        return _err(f"{spec.robot} unreachable: {e}", offline=spec.robot)
    except Exception as e:                                       # a tool bug must not kill the mission thread
        log.exception("tool %s crashed", name)
        return _err(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- drone tools
def _flight_area(ctx: Ctx, args: dict):
    """target (a zone, clipped to the cage) or x0..y1 (must be inside the cage) -> (Rect, clipped) or an error dict."""
    if args.get("target") is not None:
        loc = _location(ctx, args)
        rect = loc["rect"] or Rect(x0=loc["x"] - 0.6, y0=loc["y"] - 0.6, x1=loc["x"] + 0.6, y1=loc["y"] + 0.6)
        cage = ctx.arena.drone_cage
        clip = Rect(x0=max(rect.x0, cage.x0), y0=max(rect.y0, cage.y0), x1=min(rect.x1, cage.x1),
                    y1=min(rect.y1, cage.y1))
        if clip.x0 >= clip.x1 or clip.y0 >= clip.y1:
            return _err("that area is outside the drone cage")
        return clip, clip != rect
    rect = Rect(x0=args["x0"], y0=args["y0"], x1=args["x1"], y1=args["y1"]).normalized()
    if (msg := ctx.safety.drone_rect(rect)):
        return _err(msg)
    return rect, False


@action("drone", needs=(("target",), ("x0", "y0", "x1", "y1")), checkpoint=True, mentor=True, fault="scan")
def drone_scan(ctx: Ctx, args: dict) -> dict:
    """Take off from where the drone is (normally the rover's plate), sweep a rectangle and report spots where
    something sticks up from the floor. The drone cannot identify objects, only say "something is here".
    The drone stays in the air afterwards: call drone_dock (or drone_land) before moving the rover.
    Args: target = "$zone.<name>" (clipped to the flight cage), or x0, y0, x1, y1 in metres.
    Returns: {findings: [{id, x, y, dz, by}]}; f1 is the strongest hit."""
    area = _flight_area(ctx, args)
    if isinstance(area, dict):
        return area
    rect, clipped = area
    if (msg := ctx.safety.drone_battery(ctx.drone.pose().get("battery_v"))):
        return _err(msg)
    ctx.drone_out = True                                         # from takeoff until dock/land, even if the scan fails
    res = ctx.drone.scan(rect.x0, rect.y0, rect.x1, rect.y1)
    if res.get("ok") is False or "error" in res:
        return _err(res.get("error", "scan failed"))
    found = ctx.store.add_findings(res.get("findings", []))
    ctx.store.say("ai", f"Drone scan found {len(found)} spot(s) of interest." if found
                  else "Drone scan found nothing in that area.")
    return {"ok": True, "clipped": clipped, "area": [rect.x0, rect.y0, rect.x1, rect.y1],
            "findings": [f.model_dump(include={"id", "x", "y", "dz", "by"}) for f in found]}


def _brief_pose(ctx: Ctx) -> dict:
    p = ctx.drone.pose()
    return {k: p.get(k) for k in ("x", "y", "z", "state")}


@action("drone", mentor=True)
def drone_takeoff(ctx: Ctx, args: dict) -> dict:
    """Take the scout drone off and hover at search height. Use it before manual flying; drone_search and
    drone_mark take off by themselves. Land it again with drone_land. Returns: {pose}."""
    if (msg := ctx.safety.drone_battery(ctx.drone.pose().get("battery_v"))):
        return _err(msg)
    res = _norm(ctx.drone.takeoff(ctx.cfg.search_z))
    if res["ok"]:
        ctx.drone_out = True
        res["pose"] = _brief_pose(ctx)
    return res


@action("drone", needs=(("target",), ("x", "y")), mentor=True)
def drone_mark(ctx: Ctx, args: dict) -> dict:
    """Fly the scout drone over a spot and hover there, so everyone (and the rover) can see where it is.
    The spot is also registered as a finding, so later steps can refer to it as "$finding.<id>".
    The drone stays in the air; land it with drone_land when the rover has arrived.
    Args: target = "$finding.<id>" | "$zone.<name>" | "x,y", or x, y in metres.
    Returns: {pose, finding_id}."""
    loc = _location(ctx, args)
    if (msg := ctx.safety.drone_target(loc["x"], loc["y"], ctx.cfg.mark_z)):
        return _err(msg)
    if (msg := ctx.safety.drone_battery(ctx.drone.pose().get("battery_v"))):
        return _err(msg)
    ctx.drone_out = True
    if ctx.drone.pose().get("state") != "flying":
        up = _norm(ctx.drone.takeoff(ctx.cfg.search_z))
        if not up["ok"]:
            return up
    res = _norm(ctx.drone.goto(loc["x"], loc["y"], ctx.cfg.mark_z))
    if not res["ok"]:
        return res
    (finding,) = ctx.store.add_findings([{"x": loc["x"], "y": loc["y"], "by": "scout"}])
    ctx.store.say("ai", f"Scout is hovering over {finding.id} at ({loc['x']:.1f}, {loc['y']:.1f}).")
    return {"ok": True, "pose": _brief_pose(ctx), "finding_id": finding.id}


@action("drone", needs=(("target",), ("x0", "y0", "x1", "y1")), choices={"mode": ("first", "all")},
        checkpoint=True, mentor=True, fault="scan")
def drone_search(ctx: Ctx, args: dict) -> dict:
    """Fly a lawnmower search pattern over an area at search height and watch with the scout's downward detector
    for the crashed drone. Stops at the first detection unless mode is "all". Every detection is registered as a
    finding (f1 = the first). Finding nothing is a normal result, not an error. The scout stays in the air.
    Args: target = "$zone.<name>" (clipped to the flight cage) or x0, y0, x1, y1 in metres;
    mode = "first" (default) | "all".
    Returns: {findings: [{id, x, y, by}], waypoints_flown, area, search_path}."""
    area = _flight_area(ctx, args)
    if isinstance(area, dict):
        return area
    rect, clipped = area
    if ctx.detector is None:
        return _err("no detector is configured")
    z = ctx.cfg.search_z
    path = search_waypoints(rect, ctx.detector.footprint_radius(z))
    base = {"area": [rect.x0, rect.y0, rect.x1, rect.y1], "clipped": clipped, "search_path": [[x, y] for x, y in path]}
    pose = ctx.drone.pose()
    if (msg := ctx.safety.drone_battery(pose.get("battery_v"))):
        return _err(msg, **base)
    ctx.drone_out = True
    if pose.get("state") != "flying":
        up = _norm(ctx.drone.takeoff(z))
        if not up["ok"]:
            return {**up, **base}
    found: list = []
    flown = 0
    for x, y in path:
        partial = {**base, "waypoints_flown": flown, "findings": _brief_findings(found)}
        if ctx.cancelled():
            return _err("cancelled", **partial)
        if (msg := ctx.safety.drone_battery(ctx.drone.pose().get("battery_v"))):
            return _err(f"{msg}; search aborted, land the drone", blocked="battery", **partial)
        res = _norm(ctx.drone.goto(x, y, z))
        if not res["ok"]:
            return {**res, **partial}
        flown += 1
        hits = ctx.detector.observe(ctx.drone.pose())
        for h in hits:
            (f,) = ctx.store.add_findings([{"x": h["x"], "y": h["y"], "by": "scout"}])
            if f not in found:
                found.append(f)
            if h.get("error_m") is not None:                    # unknown for a real camera
                ctx.store.set_metric("detection_error_m", h["error_m"])
        if hits and args.get("mode") != "all":
            break
    ctx.store.say("ai", f"Scout search flew {flown} waypoints and found {len(found)} object(s)." if found
                  else f"Scout search flew {flown} waypoints and found nothing.")
    return {"ok": True, **base, "waypoints_flown": flown, "findings": _brief_findings(found)}


def _brief_findings(found: list) -> list:
    return [f.model_dump(include={"id", "x", "y", "by"}) for f in found]


@action("drone", needs=(("x", "y", "z"),), mentor=True)
def drone_goto(ctx: Ctx, args: dict) -> dict:
    """Fly to a point inside the cage. Args: x, y, z in metres (z is height above the floor).
    The drone stays in the air; dock or land it afterwards."""
    if (msg := ctx.safety.drone_target(args["x"], args["y"], args["z"])):
        return _err(msg)
    if (msg := ctx.safety.drone_battery(ctx.drone.pose().get("battery_v"))):
        return _err(msg)
    ctx.drone_out = True
    if ctx.drone.pose().get("state") != "flying":
        ctx.drone.takeoff()
    return _norm(ctx.drone.goto(args["x"], args["y"], args["z"]))


@action("drone", ok_key="docked")
def drone_dock(ctx: Ctx, args: dict) -> dict:
    """Land the drone on the rover's plate. The rover must not have moved since the drone took off.
    While docked, the drone's position is also the rover's position. Returns: {docked}."""
    res = _norm(ctx.drone.dock())
    if res["ok"] and res.get("docked", True):
        ctx.drone_out = False
        res["docked"] = True
    return res


@action("drone")
def drone_land(ctx: Ctx, args: dict) -> dict:
    """Land the drone where it is (fallback when docking is not possible)."""
    res = _norm(ctx.drone.land())
    if res["ok"]:
        ctx.drone_out = False
    return res


@action("drone", ok_key="docked")
def drone_home(ctx: Ctx, args: dict) -> dict:
    """Fly the scout back to its launch pad (where it first took off) at a safe height and land it there.
    Use this to get the scout out of the rover's way after marking a spot, and at the end of a mission,
    instead of drone_land, which would land it on the spot it is hovering over. Returns: {docked, pose}."""
    if ctx.drone.pose().get("state") != "flying" and (msg := ctx.safety.mentor_gate()):
        return _err(msg, blocked="mentor")                      # landed elsewhere: getting home means taking off
    res = _norm(ctx.drone.dock())
    if res["ok"] and res.get("docked", True):
        ctx.drone_out = False
        res["docked"] = True
    return res


@action("manager", needs=(("target",),), choices={"mode": ("first", "all")}, checkpoint=True, fault="locate")
def camera_locate(ctx: Ctx, args: dict) -> dict:
    """Find an object on the floor with the fixed overhead camera (a webcam on the ceiling that sees the whole
    field; the backup when the scout cannot fly). Nothing moves. Every hit is registered as a finding (f1 = the best
    match) with the overhead photo as evidence, ready for rover_goto "$finding.f1". Finding nothing is a normal
    result. Only available when ROBOT STATUS shows an "overhead" camera that is calibrated.
    Args: target = what to look for, in words, e.g. "crashed drone"; mode = "first" (default) | "all".
    Returns: {findings: [{id, x, y, by, label}], image_id}."""
    if ctx.overhead is None:
        return _err("no overhead camera is installed (set OVERHEAD_CAM)")
    from .overhead import OverheadError
    try:
        hits = ctx.overhead.locate(args["target"])
    except OverheadError as e:
        return _err(str(e))
    if args.get("mode") != "all":
        hits = hits[:1]
    jpeg = ctx.overhead.preview_jpeg()
    image_id = ctx.store.add_image(jpeg) if jpeg else None
    found = ctx.store.add_findings([{"x": h["x"], "y": h["y"], "label": h["label"], "by": "overhead"} for h in hits])
    for f in found:
        if f.image_id is None:
            ctx.store.update_finding(f.id, image_id=image_id)
    ctx.store.say("ai", f"Overhead camera found {len(found)} object(s): " +
                  ", ".join(f"{f.id} at ({f.x:.2f}, {f.y:.2f})" for f in found) if found
                  else f"Overhead camera sees no {args['target']}.")
    return {"ok": True, "image_id": image_id,
            "findings": [f.model_dump(include={"id", "x", "y", "by", "label"}) for f in found]}


# ---------------------------------------------------------------- rover tools
def _bearing_deg(cam, col: float) -> float:
    """Pixel column -> degrees to turn left (+) or right (-) to centre it."""
    half = math.tan(math.radians(cam.fov_deg / 2))
    return math.degrees(math.atan((cam.width / 2 - col) / (cam.width / 2) * half))


@action("rover")
def rover_snapshot(ctx: Ctx, args: dict) -> dict:
    """Take one photo with the rover camera and keep it for the dashboard. Returns: {image_id}."""
    image_id = ctx.store.add_image(ctx.rover.snapshot())
    return {"ok": True, "image_id": image_id}


@action("rover", needs=(("x", "y", "z"),), moves_rover=True)
def rover_drive(ctx: Ctx, args: dict) -> dict:
    """Move the rover relative to where it is: x metres forward, y metres left, z degrees turn-left
    (clamped to 1 m and 90 degrees per call; never leaves the arena). For manual control: plans should use
    rover_goto instead. Returns: {pose, pose_source}."""
    x, y, z = ctx.safety.clamp_move(args["x"], args["y"], args["z"])
    pose = ctx.loc.rover_pose()
    c, s = math.cos(math.radians(pose.yaw)), math.sin(math.radians(pose.yaw))
    if (msg := ctx.safety.rover_target(pose.x + c * x - s * y, pose.y + s * x + c * y)):
        return _err(msg)
    res = _norm(ctx.rover.drive(x, y, z))
    after = ctx.loc.rover_pose()
    return {**res, "pose": after.as_dict(), "pose_source": after.source}


@action("rover", needs=(("target",), ("x", "y")), ok_key="arrived", moves_rover=True, fault="goto")
def rover_goto(ctx: Ctx, args: dict) -> dict:
    """Drive the rover to a place using metric navigation (within arena.goto_tolerance_m). For a drone finding it
    stops 0.6 m short so the camera can see the object, unless standoff says otherwise (rover centre to object;
    0 = drive right onto the spot), and then turns to face it. Does not identify anything.
    Args: target = "$finding.<id>" | "$zone.<name>" | "$home", or x, y in metres (Lighthouse frame);
    standoff = rover centre to the finding in metres (optional), or gap = gripper fingertips to the finding in
    metres (optional; e.g. 0.18 parks ready for rover_pick_up).
    Returns: {arrived, pose, pose_source, error_m}."""
    loc = _location(ctx, args)
    if (msg := ctx.safety.rover_target(loc["x"], loc["y"])):
        return _err(msg, arrived=False)
    pose = ctx.loc.rover_pose()
    gx, gy = loc["x"], loc["y"]
    if loc["kind"] == "finding":
        d = distance(pose.x, pose.y, gx, gy)
        if args.get("gap") is not None:
            standoff = ctx.arena.grip_reach_m + max(0.0, float(args["gap"]))
        elif args.get("standoff") is not None:
            standoff = max(0.0, float(args["standoff"]))
        else:
            standoff = ctx.arena.finding_standoff_m
        if d > standoff:
            k = (d - standoff) / d
            gx, gy = pose.x + (gx - pose.x) * k, pose.y + (gy - pose.y) * k
        else:
            gx, gy = pose.x, pose.y
    tol = ctx.arena.goto_tolerance_m
    t_end = ctx.clock.now() + GOTO_TIMEOUT_S
    best, t_best = float("inf"), ctx.clock.now()
    while True:
        if ctx.cancelled():
            ctx.rover.stop()
            return _err("cancelled", arrived=False)
        pose = ctx.loc.rover_pose()
        err = distance(pose.x, pose.y, gx, gy)
        info = {"pose": pose.as_dict(), "pose_source": pose.source, "pose_age_s": round(pose.age_s, 1),
                "error_m": round(err, 2)}
        if err < best - 0.01:
            best, t_best = err, ctx.clock.now()
        settled = ctx.clock.now() - t_best > GOTO_STALL_S
        if err <= tol or (settled and err <= 2 * tol):
            ctx.rover.stop()
            if loc["kind"] == "finding" and distance(pose.x, pose.y, loc["x"], loc["y"]) > 0.05:
                info["turned_deg"] = _face(ctx, loc["x"], loc["y"])
            return {"ok": True, "arrived": True, **info}
        if settled:
            ctx.rover.stop()
            return _err(f"stuck {err:.2f} m from the goal for {GOTO_STALL_S:g} s: something blocks the rover, or "
                        "its position is wrong", arrived=False, **info)
        if not ctx.arena.view_rect().contains(pose.x, pose.y, margin=0.1):
            ctx.rover.stop()                     # a wrong anchor makes the rover chase a goal it can never reach
            return _err("the rover's position left the mapped area; its position is probably wrong. Re-anchor it "
                        "(Manual control: anchor from a Crazyflie, or the start cross) and try again",
                        arrived=False, **info)
        if ctx.clock.now() > t_end:
            ctx.rover.stop()
            return _err("timed out before arriving", arrived=False, **info)
        c, s = math.cos(math.radians(pose.yaw)), math.sin(math.radians(pose.yaw))
        dx, dy = gx - pose.x, gy - pose.y
        vx, vy = GOTO_GAIN * (c * dx + s * dy), GOTO_GAIN * (-s * dx + c * dy)
        if 0 < math.hypot(vx, vy) < GOTO_MIN_SPEED:
            k = GOTO_MIN_SPEED / math.hypot(vx, vy)
            vx, vy = vx * k, vy * k
        vx, vy, _ = ctx.safety.clamp_speed(vx, vy)
        ctx.rover.speed(vx, vy, 0.0, 0.3)
        ctx.clock.sleep(0.1)


def _face(ctx: Ctx, x: float, y: float) -> float:
    """Turn in place until the rover's front (the gripper) points at (x, y). Returns the total turn in degrees."""
    total = 0.0
    for _ in range(3):                                   # clamp_move turns at most 90 degrees per call
        pose = ctx.loc.rover_pose()
        bearing = (math.degrees(math.atan2(y - pose.y, x - pose.x)) - pose.yaw + 180.0) % 360.0 - 180.0
        if abs(bearing) <= 5.0 or ctx.cancelled():
            break
        turn = ctx.safety.clamp_move(0.0, 0.0, bearing)[2]
        ctx.rover.drive(0.0, 0.0, turn)
        total += turn
    return round(total, 1)


@action("rover", needs=(("target",),), ok_key="found", moves_rover=True, fault="search")
def rover_search(ctx: Ctx, args: dict) -> dict:
    """Rotate in 45 degree steps (one full turn at most) until the camera sees the described object, then face it.
    Args: target = what to look for, in words, e.g. "cardboard box".
    Returns: {found, bearing_deg, image_id}."""
    target, cam = args["target"], ctx.arena.camera
    image_id = None
    for i in range(SEARCH_TURNS + 1):
        if ctx.cancelled():
            return _err("cancelled", found=False)
        jpeg = ctx.rover.snapshot()
        image_id = ctx.store.add_image(jpeg)
        p = ctx.perception.point(jpeg, target)
        if p:
            bearing = _bearing_deg(cam, p["col"])
            if abs(bearing) > 5:
                ctx.rover.drive(*ctx.safety.clamp_move(0.0, 0.0, bearing))
            return {"ok": True, "found": True, "bearing_deg": round(bearing, 1), "image_id": image_id,
                    "label": p.get("label")}
        if i < SEARCH_TURNS:
            ctx.rover.drive(0.0, 0.0, 45.0)
    return _err(f"{target!r} not seen in a full turn", found=False, image_id=image_id)


@action("rover", needs=(("target",),), ok_key="in_reach", moves_rover=True, fault="approach")
def rover_approach(ctx: Ctx, args: dict) -> dict:
    """Close the last distance to an object using only the camera: look, turn to centre it, step forward 12 cm,
    repeat until it sits on the calibrated grip line (inside gripper reach). Open the gripper first.
    Args: target = what to approach, in words, e.g. "cardboard box".
    Returns: {in_reach, steps, image_id}."""
    target, cam = args["target"], ctx.arena.camera
    steps = lost = 0
    image_id = None
    t_end = ctx.clock.now() + APPROACH_MAX_S
    for _ in range(APPROACH_MAX_ITERS):
        if ctx.cancelled():
            return _err("cancelled", in_reach=False, steps=steps)
        if ctx.clock.now() > t_end:
            break
        jpeg = ctx.rover.snapshot()
        image_id = ctx.store.add_image(jpeg)
        p = ctx.perception.point(jpeg, target)
        if p is None:
            lost += 1
            if lost >= 2:
                return _err("target lost; search for it again", in_reach=False, steps=steps, image_id=image_id)
            continue
        lost = 0
        if abs(p["col"] - cam.width / 2) > cam.deadband_px:
            ctx.rover.drive(*ctx.safety.clamp_move(0.0, 0.0, _bearing_deg(cam, p["col"])))
            steps += 1
        elif p["row"] >= cam.grip_line_row:
            return {"ok": True, "in_reach": True, "steps": steps, "image_id": image_id}
        else:
            ctx.rover.drive(*ctx.safety.clamp_move(APPROACH_STEP_M, 0.0, 0.0))
            steps += 1
    return _err("did not reach the object in time", in_reach=False, steps=steps, image_id=image_id)


@action("rover", needs=(("question",),), checkpoint=True)
def rover_inspect(ctx: Ctx, args: dict) -> dict:
    """Take a photo with the rover camera and answer a question about what is in front of the robot.
    The photo is kept as evidence on the dashboard.
    Args: question, e.g. "What object is this and is it blocking the exit?".
    Returns: {answer, label, confidence, grippable, image_id}."""
    jpeg = ctx.rover.snapshot()
    image_id = ctx.store.add_image(jpeg)
    r = ctx.perception.inspect(jpeg, args["question"])
    pose = ctx.loc.rover_pose()
    finding = ctx.store.nearest_finding(pose.x, pose.y, INSPECT_FINDING_RADIUS_M)
    if finding is None and r.get("label") != "nothing":
        # nobody flagged this (e.g. a manual drive): register it just ahead of the rover so it can be tracked
        c, s = math.cos(math.radians(pose.yaw)), math.sin(math.radians(pose.yaw))
        (finding,) = ctx.store.add_findings([{"x": pose.x + UNFLAGGED_AHEAD_M * c, "y": pose.y + UNFLAGGED_AHEAD_M * s,
                                              "by": "rover"}])
    if finding:
        fields = {"label": r.get("label"), "confidence": r.get("confidence"), "image_id": image_id,
                  "category": r.get("category"), "hazardous": r.get("hazardous")}
        if finding.status == "found":
            fields["status"] = "inspected"
        ctx.store.update_finding(finding.id, **fields)
    ctx.store.say("ai", f"Rover camera: {r.get('answer')}")
    return {"ok": True, **r, "image_id": image_id, "finding_id": finding.id if finding else None}


@action("rover", needs=(("mode",),), choices={"mode": ("open", "close")}, fault="grip")
def rover_grip(ctx: Ctx, args: dict) -> dict:
    """Open or close the gripper. Args: mode = "open" | "close"."""
    return _norm(ctx.rover.grip(args["mode"]))


@action("rover", ok_key="holding")
def rover_check_grip(ctx: Ctx, args: dict) -> dict:
    """Look at the gripper with the camera and say whether it holds an object. Call after closing the gripper
    and lifting. Returns: {holding, reason, image_id}."""
    jpeg = ctx.rover.snapshot()
    image_id = ctx.store.add_image(jpeg)
    r = ctx.perception.holding(jpeg)
    if r.get("holding"):
        pose = ctx.loc.rover_pose()
        if (f := ctx.store.nearest_finding(pose.x, pose.y, RECOVERED_RADIUS_M)) is not None:
            ctx.store.update_finding(f.id, status="recovered")
    return {"ok": True, **r, "image_id": image_id}


@action("rover", ok_key="holding", moves_rover=True, fault="grip")
def rover_pick_up(ctx: Ctx, args: dict) -> dict:
    """Pick up the object in front of the rover in one go: check the camera sees it, identify it (rover_inspect),
    lower the arm, open the gripper, creep up on the camera until it is in reach, close, lift, and check the grip.
    For an operator who drove the rover up to the object by hand. A hazardous object stops before the arm moves
    until the operator confirms (manual control: confirm=true, the "Pick up (confirm)" button).
    Args: target = what to pick up, in words (optional, default "object in front of the robot").
    Returns: {holding, stage, label, category, reason, image_id}; stage says where it stopped if it did not finish."""
    target = args.get("target") or "object in front of the robot"
    jpeg = ctx.rover.snapshot()
    image_id = ctx.store.add_image(jpeg)
    if ctx.perception.point(jpeg, target) is None:
        return _err(f"no {target} seen in front of the camera", holding=False, stage="look", image_id=image_id)
    seen = rover_inspect(ctx, {"question": f"What is the {target}? Is it a drone, and is it ours or hostile?"})
    what = {k: seen.get(k) for k in ("label", "category", "hazardous", "finding_id")}
    if seen.get("label") == "nothing":
        return _err("the camera sees nothing to pick up", holding=False, stage="look", image_id=seen.get("image_id"))
    if args.get("confirm") is True and seen.get("finding_id"):
        ctx.store.update_finding(seen["finding_id"], authorized=True)     # the operator's explicit yes
        ctx.store.say("system", f"Operator confirmed the pick-up of {seen['finding_id']} ({seen.get('label')})")
    if (msg := _authorization_gate(ctx)):
        return _err(f"{msg}. Camera: {seen.get('answer')} Press 'Pick up (confirm)' to go ahead.", holding=False,
                    stage="authorize", blocked="authorization", image_id=seen.get("image_id"), **what)
    ctx.store.say("system", f"Rover: picking up the {target}: arm down, gripper open")
    for step in (lambda: ctx.rover.arm("pick"), lambda: ctx.rover.grip("open")):
        if ctx.cancelled():
            return _err("cancelled", holding=False, stage="prepare")
        r = _norm(step())
        if not r.get("ok"):
            return _err(f"could not prepare the arm/gripper: {r.get('error')}", holding=False, stage="prepare")
    r = rover_approach(ctx, {"target": target})
    if not r.get("in_reach"):
        return _err(f"approach: {r.get('error')}", holding=False, stage="approach", image_id=r.get("image_id"))
    if (msg := _authorization_gate(ctx)):
        return _err(msg, holding=False, stage="authorize", blocked="authorization")
    for stage, step in (("grip", lambda: ctx.rover.grip("close")), ("lift", lambda: ctx.rover.arm("carry"))):
        if ctx.cancelled():
            return _err("cancelled", holding=False, stage=stage)
        r = _norm(step())
        if not r.get("ok"):
            return _err(f"{stage} failed: {r.get('error')}", holding=False, stage=stage)
    r = rover_check_grip(ctx, {})
    if not r.get("holding"):
        return _err(f"lifted, but the camera sees nothing in the gripper ({r.get('reason')})", holding=False,
                    stage="check", image_id=r.get("image_id"))
    ctx.store.say("system", f"Rover: holding the {target}")
    return {"ok": True, "holding": True, "stage": "done", "reason": r.get("reason"), "image_id": r.get("image_id"),
            **what}


@action("rover", needs=(("mode",),), choices={"mode": ("pick", "carry", "home")})
def rover_arm(ctx: Ctx, args: dict) -> dict:
    """Move the arm to a preset. Args: mode = "pick" (lowered, ready to grab) | "carry" (lifted) | "home"."""
    return _norm(ctx.rover.arm(args["mode"]))


# ---------------------------------------------------------------- manager tools
@action("manager", needs=(("text",),))
def report(ctx: Ctx, args: dict) -> dict:
    """Post a message to the human on the dashboard, optionally pointing at a finding (its photo is shown).
    Use it for the final answer: what was found, where, and what the robots did.
    Args: text, finding_id (optional)."""
    ctx.store.say("ai", args["text"])
    fid = args.get("finding_id")
    finding = ctx.store.get_finding(fid) if fid else None
    return {"ok": True, "finding_id": fid, "image_id": finding.image_id if finding else None}


@action("manager", needs=(("finding_id",),))
def request_authorization(ctx: Ctx, args: dict) -> dict:
    """Ask the human operator whether the rover may pick up a finding, and wait for the answer. Required before
    handling anything hazardous: the gripper will not close on a hazardous finding without it. A "no" skips the
    item. Args: finding_id, e.g. "f1". Returns: {authorized}."""
    f = ctx.store.get_finding(args["finding_id"])
    if f is None:
        return _err(f"no finding {args['finding_id']!r}")
    question = (f"Finding {f.id} looks like a {(f.category or 'object').replace('_', ' ')}"
                f"{' and is hazardous' if f.hazardous else ''}. Authorize the rover to pick it up? (yes/no)")
    ctx.store.say("ai", question)
    answer = ctx.ask(question) if ctx.ask else None
    if answer is None:
        return _err("no answer received from the operator")
    if answer.strip().lower().startswith("y"):
        ctx.store.update_finding(f.id, authorized=True)
        return {"ok": True, "authorized": True, "finding_id": f.id}
    ctx.store.update_finding(f.id, status="skipped")
    return {"ok": False, "authorized": False, "finding_id": f.id, "error": "operator declined"}


@action("manager", needs=(("question",),), checkpoint=True)      # the supervisor reacts to the answer
def ask_human(ctx: Ctx, args: dict) -> dict:
    """Pause the mission and ask the human a question; the answer comes back as the result.
    Use only when the mission is ambiguous and a wrong guess would waste robot time. Args: question."""
    ctx.store.say("ai", args["question"])
    answer = ctx.ask(args["question"]) if ctx.ask else None
    if answer is None:
        return _err("no answer received")
    return {"ok": True, "answer": answer}
