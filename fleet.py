"""
fleet.py - ROS-free heterogeneous team: Crazyflie scouts + RoboMaster EP fetcher, one shared Lighthouse frame,
orchestrated by an LLM with tools. Runs on a laptop with a Crazyradio (Python 3.10+).

Trick 1 (shared frame, no SLAM): tape a spare Crazyflie with a Lighthouse deck on top of the EP, nose facing
         the EP's front, motors never armed. We only READ its Lighthouse pose -> the EP gets absolute x, y, yaw
         in the same frame the drones fly in.
Trick 2 (sensing without a drone camera): scouts fly a lawnmower pattern at fixed height. The flow deck's
         downward range sensor (range.zrange) suddenly gets shorter when the drone passes over an object.
         That dip = "something is here" at (x, y). The EP then drives there and its camera + VLM identify it.

  pip install cflib google-genai requests        (mac: brew install libusb; windows: Zadig driver for Crazyradio)
  export GEMINI_API_KEY=...  ROBOT=http://<windows-ip>:8000
  export SCOUTS="radio://0/80/2M/E7E7E7E7E1"  RIDER="radio://0/80/2M/E7E7E7E7E9"
  MOCK=1 python fleet.py "Search the left half of the arena, then bring me whatever you find"

VERIFY ON THE FLOOR (do not trust blindly): Lighthouse origin/axes from the mentors, EP strafe sign (SIGN_Y),
arming requirement on the brushless Crazyflie, zrange baseline at your flight height.
"""
import math, os, sys, threading, time
import requests

MOCK = os.getenv("MOCK") == "1"
ROBOT = os.getenv("ROBOT", "http://localhost:8000")
SCOUTS = [u for u in os.getenv("SCOUTS", "").split(",") if u]
RIDER = os.getenv("RIDER", "")
FLY_Z = 0.6          # metres
DIP_M = 0.12         # zrange shorter than baseline by this much = object below
SIGN_Y = -1.0        # RoboMaster SDK: +y = right. Flip to +1.0 if the EP strafes the wrong way.
MODEL = os.getenv("MODEL", "gemini-robotics-er-2-preview")

if not MOCK:
    import cflib.crtp
    from cflib.crazyflie import Crazyflie
    from cflib.crazyflie.log import LogConfig
    from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
    from cflib.positioning.position_hl_commander import PositionHlCommander
    from cflib.utils.reset_estimator import reset_estimator
    cflib.crtp.init_drivers()


# ---------------------------------------------------------------- rider: absolute pose for the EP
class Rider:
    def __init__(self, uri):
        self.pose = (0.0, 0.0, 0.0)  # x, y, yaw(deg)
        if MOCK or not uri:
            return
        self.scf = SyncCrazyflie(uri, cf=Crazyflie(rw_cache="./cache"))
        self.scf.open_link()
        lg = LogConfig(name="rider", period_in_ms=50)
        for v in ("stateEstimate.x", "stateEstimate.y", "stateEstimate.yaw"):
            lg.add_variable(v, "float")
        lg.data_received_cb.add_callback(
            lambda ts, d, c: setattr(self, "pose", (d["stateEstimate.x"], d["stateEstimate.y"], d["stateEstimate.yaw"])))
        self.scf.cf.log.add_config(lg)
        lg.start()


rider = Rider(RIDER)


# ---------------------------------------------------------------- scouts
def _lawnmower(x0, y0, x1, y1, lane=0.4):
    pts, y, flip = [], min(y0, y1), False
    while y <= max(y0, y1) + 1e-6:
        a, b = (min(x0, x1), max(x0, x1)) if not flip else (max(x0, x1), min(x0, x1))
        pts += [(a, y), (b, y)]
        y, flip = y + lane, not flip
    return pts


def _fly_scout(uri, waypoints, hits):
    if MOCK:
        hits.append((round(sum(p[0] for p in waypoints) / len(waypoints), 2), 1.0, uri or "mock"))
        return
    samples = []
    with SyncCrazyflie(uri, cf=Crazyflie(rw_cache="./cache")) as scf:
        reset_estimator(scf)
        try:
            scf.cf.platform.send_arming_request(True)  # brushless Crazyflie needs arming; harmless otherwise
            time.sleep(1.0)
        except AttributeError:
            pass
        lg = LogConfig(name="scan", period_in_ms=50)
        lg.add_variable("stateEstimate.x", "float")
        lg.add_variable("stateEstimate.y", "float")
        lg.add_variable("range.zrange", "uint16_t")  # mm
        lg.data_received_cb.add_callback(lambda ts, d, c: samples.append(
            (d["stateEstimate.x"], d["stateEstimate.y"], d["range.zrange"] / 1000.0)))
        scf.cf.log.add_config(lg)
        lg.start()
        with PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3) as pc:
            for x, y in waypoints:
                pc.go_to(x, y)
        lg.stop()
    if not samples:
        return
    base = sorted(s[2] for s in samples)[len(samples) // 2]  # median = floor distance
    blob = [s for s in samples if base - s[2] > DIP_M]
    if blob:  # one object per scout run is enough for a demo
        hits.append((round(sum(s[0] for s in blob) / len(blob), 2),
                     round(sum(s[1] for s in blob) / len(blob), 2), uri))


# ---------------------------------------------------------------- LLM tools
def scout_area(x_min: float, y_min: float, x_max: float, y_max: float) -> dict:
    """Send all scout drones to sweep a rectangle (metres, Lighthouse frame) in parallel.
    Returns positions where something sticks up from the floor. Drones land afterwards."""
    pts = _lawnmower(x_min, y_min, x_max, y_max)
    uris = SCOUTS or ["mock"]
    chunks = [pts[i::len(uris)] for i in range(len(uris))]  # naive split; good enough for 2-3 drones
    hits, threads = [], []
    for uri, wp in zip(uris, chunks):
        if wp:
            t = threading.Thread(target=_fly_scout, args=(uri, wp, hits))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    return {"objects_found_at": [{"x": h[0], "y": h[1], "found_by": h[2]} for h in hits]}


def ground_go_to(x: float, y: float, tolerance_m: float = 0.15, timeout_s: float = 30.0) -> dict:
    """Drive the ground robot to (x, y) in the shared frame using the rider's absolute pose. Mecanum: no turning needed."""
    if MOCK:
        return {"arrived": True, "pose": [x, y, 0.0]}
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        px, py, yaw = rider.pose
        dx, dy = x - px, y - py
        if math.hypot(dx, dy) < tolerance_m:
            requests.get(ROBOT + "/stop", timeout=2)
            return {"arrived": True, "pose": [px, py, yaw]}
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        fwd, left = c * dx + s * dy, -s * dx + c * dy  # world error -> robot frame
        k = 0.8
        requests.get(ROBOT + "/speed", params={"x": max(-0.3, min(0.3, k * fwd)),
                                               "y": SIGN_Y * max(-0.3, min(0.3, k * left)),
                                               "z": 0, "t": 0.3}, timeout=2)
        time.sleep(0.1)
    requests.get(ROBOT + "/stop", timeout=2)
    return {"arrived": False, "pose": list(rider.pose)}


def ground_gripper(action: str) -> dict:
    """Open or close the ground robot's gripper. action: 'open' or 'close'."""
    return requests.get(ROBOT + "/grip", params={"action": action}, timeout=10).json() if not MOCK else {"ok": True}


def ground_look() -> str:
    """Describe what the ground robot's camera sees right now (object identity, is it grippable)."""
    if MOCK:
        return "mock: a red cup about 20 cm ahead"
    from google import genai
    from google.genai import types
    img = requests.get(ROBOT + "/snapshot", timeout=10).content
    r = genai.Client().models.generate_content(
        model=MODEL, contents=[types.Part.from_bytes(data=img, mime_type="image/jpeg"),
                               "What object is directly in front of the robot, and how far roughly?"])
    return r.text


def run(task):
    from google import genai
    from google.genai import types
    chat = genai.Client().chats.create(model=MODEL, config=types.GenerateContentConfig(
        system_instruction=("You coordinate drones (fast, can only detect that something is on the floor) and a ground "
                            "robot (slow, has camera and gripper). Use drones to find, ground robot to verify and fetch. "
                            "Arena is roughly x 0..4, y 0..3 metres. Explain each decision in one line."),
        tools=[scout_area, ground_go_to, ground_gripper, ground_look], temperature=0.2))
    print(chat.send_message(task).text)


if __name__ == "__main__":
    run(" ".join(sys.argv[1:]) or "Find anything on the floor and bring it to (0, 0).")
