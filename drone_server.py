"""Drone server (:8001): wraps cflib for the scout Crazyflie (flies) and an optional target Crazyflie
(sits on the floor and only reports its Lighthouse pose). Contract: docs/mothership-plan.md §4.2.

Every flight command goes through the high-level commander with a computed duration, and every wait is
sliced so /land (from another request thread) can cut in at any moment: it bumps an abort generation,
sends land, and returns at once. Motion calls that see a newer generation stop and send nothing more.

Env:
  SCOUT_URI   radio URI of the flying drone (default: the one confirmed by cfclient)
  TARGET_URI  radio URI of the floor drone; empty = /pose?cf=target answers {"ok": false}
  CAGE        "x0,y0,x1,y1,zmax" geofence in metres (default: DEFAULT_CAGE, measured on the floor)
  MOCK=1      no radio; flights take real time / MOCK_TIME_SCALE, the target sits at MOCK_TARGET="x,y" (default 1.0,0.3)
"""
import math
import os
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
MOCK = os.getenv("MOCK", "0") == "1"
MOCK_TIME_SCALE = float(os.getenv("MOCK_TIME_SCALE", "10"))  # mock flights take real duration / this

SCOUT_URI = os.getenv("SCOUT_URI", "radio://0/80/2M/E7E7E7E704")  # confirmed by cfclient (address ends in 04)
TARGET_URI = os.getenv("TARGET_URI", "")
FLY_Z = 0.6          # default flight height (m)
DOCK_Z = 0.27        # plate height, arena.json plate_offset.dock_z
DIP_M = 0.12         # zrange dip below the floor median that counts as "something here"
SPEED = 0.3          # m/s for go_to durations (spec: <= 0.4)
LANE_M = 0.4         # lawnmower lane spacing
MIN_BATTERY_V = 3.3  # refuse takeoff below this
POSE_STALE_S = 1.0   # no log packet for this long = pose not trustworthy
CONNECT_TRIES = 3
PORT = int(os.getenv("PORT", "8001"))


# Measured 2026-10-10 by walking the scout to the cage corners (Lighthouse frame, origin inside the cage):
# (2.06, -0.71), (1.96, 1.18), (-1.35, 1.48); the fourth corner read garbage (tracking lost), to re-measure.
# Default = the overlap of those corners, 0.3 m in from each wall.
DEFAULT_CAGE = "-1.05,-0.41,1.66,0.88,1.2"
SANE_MARGIN_M = 1.5  # a pose this far outside the cage means Lighthouse tracking is lost, not a real position
TAKEOFF_MARGIN_M = 0.3  # takeoff is a vertical lift, so it may start up to the measured walls (the cage is inset 0.3 m)


def _load_cage():
    x0, y0, x1, y1, zmax = (float(v) for v in os.getenv("CAGE", DEFAULT_CAGE).split(","))
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "z_max": zmax}


CAGE = _load_cage()


def _outside_cage(x, y, z=None, margin=0.0):
    """Error string if the point is outside the geofence (grown by margin), else None."""
    m = margin
    if not (CAGE["x0"] - m <= x <= CAGE["x1"] + m and CAGE["y0"] - m <= y <= CAGE["y1"] + m):
        return (f"({x:.2f}, {y:.2f}) is outside the cage x {CAGE['x0']}..{CAGE['x1']}, "
                f"y {CAGE['y0']}..{CAGE['y1']} (set CAGE env if the frame differs)")
    if z is not None and not 0.1 <= z <= CAGE["z_max"]:
        return f"z={z:.2f} is outside 0.1..{CAGE['z_max']}"
    return None


def _sane(p):
    m = SANE_MARGIN_M
    return (CAGE["x0"] - m <= p["x"] <= CAGE["x1"] + m and CAGE["y0"] - m <= p["y"] <= CAGE["y1"] + m
            and -0.5 <= p["z"] <= CAGE["z_max"] + m)


class Link:
    """One Crazyflie radio link with a 10 Hz state log."""

    def __init__(self, uri: str, with_zrange: bool):
        self.uri = uri
        self.pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
        self.battery_v = None
        self.last_log = 0.0
        self.zrange = []          # (x, y, metres) samples, collected only while a scan is recording
        self.recording = False
        self.scf = None
        self.error = None
        self.lighthouse = {}      # deck + base-station bitmaps from the firmware, for /health and error messages
        if not MOCK:
            self._connect(with_zrange)

    def _connect(self, with_zrange):
        from cflib.crazyflie import Crazyflie
        from cflib.crazyflie.log import LogConfig
        from cflib.crazyflie.syncCrazyflie import SyncCrazyflie

        for attempt in range(1, CONNECT_TRIES + 1):    # a drone that just powered on needs a moment
            print(f"Connecting to {self.uri} (attempt {attempt}/{CONNECT_TRIES})...")
            self.scf = SyncCrazyflie(self.uri, cf=Crazyflie(rw_cache="./cache"))
            try:
                self.scf.open_link()
                break
            except Exception as e:                     # noqa: BLE001
                print(f"  link failed: {e}")
                if attempt == CONNECT_TRIES:
                    raise RuntimeError(
                        f"cannot reach {self.uri}. Check: drone powered and booted (LEDs on), battery, "
                        "no cfclient or other script connected to it, radio not on a USB hub, drone within a "
                        "few metres. `python3 scan_drones.py` lists the drones the radio can hear.") from e
                time.sleep(2.0)
        print(f"Connected to {self.uri}")
        lg = LogConfig(name="state", period_in_ms=100)
        for var in ("stateEstimate.x", "stateEstimate.y", "stateEstimate.z", "stateEstimate.yaw", "pm.vbat"):
            lg.add_variable(var, "float")
        if with_zrange:
            lg.add_variable("range.zrange", "uint16_t")
        lg.data_received_cb.add_callback(self._on_log)
        self.scf.cf.log.add_config(lg)
        lg.start()
        self._watch_lighthouse(LogConfig)

    def _watch_lighthouse(self, LogConfig):
        """Second, slow log block: why there is (or isn't) a Lighthouse position. Optional: old firmware may lack it."""
        try:
            self.lighthouse["deck"] = self.scf.cf.param.get_value("deck.bcLighthouse4") == "1"
        except Exception:                              # noqa: BLE001
            self.lighthouse["deck"] = None
        try:
            self.lighthouse["estimator"] = self.scf.cf.param.get_value("stabilizer.estimator")
        except Exception:                              # noqa: BLE001
            self.lighthouse["estimator"] = None
        if self.lighthouse["deck"] is False or self.lighthouse["estimator"] not in (None, "2"):
            print("!" * 70 + f"\n{self.uri}: Lighthouse deck detected={self.lighthouse['deck']}, "
                  f"estimator={self.lighthouse['estimator']} (2 = Kalman).\nWithout the deck the drone uses the "
                  "complementary estimator: x=y=0 and z = barometer height (~200 m).\nFix: power off, reseat the "
                  "Lighthouse deck firmly, power on, restart this server.\n" + "!" * 70)
        try:
            lh = LogConfig(name="lighthouse", period_in_ms=500)
            lh.add_variable("lighthouse.status", "uint8_t")
            for var in ("bsReceive", "bsActive", "bsGeoVal", "bsCalVal"):
                lh.add_variable(f"lighthouse.{var}", "uint16_t")
            lh.data_received_cb.add_callback(
                lambda _ts, data, _conf: self.lighthouse.update({k.split(".")[1]: v for k, v in data.items()}))
            self.scf.cf.log.add_config(lh)
            lh.start()
        except Exception as e:                         # noqa: BLE001
            print(f"Lighthouse status log unavailable: {e}")

    def lighthouse_report(self):
        """{deck, status, receiving, used, geometry, calibration}: base-station ids per bitmap, plus a hint."""
        lh = dict(self.lighthouse)
        ids = lambda mask: [i for i in range(16) if mask is not None and mask >> i & 1]  # noqa: E731
        out = {"deck": lh.get("deck"), "status": {0: "not receiving", 1: "receiving, no position",
                                                   2: "estimating position"}.get(lh.get("status"), lh.get("status")),
               "receiving": ids(lh.get("bsReceive")), "used": ids(lh.get("bsActive")),
               "geometry": ids(lh.get("bsGeoVal")), "calibration": ids(lh.get("bsCalVal"))}
        out["estimator"] = {"1": "complementary", "2": "kalman"}.get(lh.get("estimator"), lh.get("estimator"))
        if out["deck"] is False or out["estimator"] == "complementary":
            out["hint"] = ("Lighthouse deck not detected at boot (complementary estimator: x=y=0, z=barometer): "
                           "power off, reseat the deck firmly, power on, restart the server")
        elif "bsReceive" in lh and not out["receiving"]:
            out["hint"] = "no base station seen: are they powered (green LED)? is the deck facing up with a clear view?"
        elif "bsGeoVal" in lh and not out["geometry"]:
            out["hint"] = ("drone has no base-station geometry: write calibration.yaml to it "
                           "(cfclient > Lighthouse > Manage geometry > Load, then Write to Crazyflie)")
        elif out["receiving"] and not set(out["receiving"]) & set(out["geometry"]):
            out["hint"] = "the stations it sees have no geometry stored: re-write calibration.yaml"
        return out

    def _on_log(self, _ts, data, _conf):
        self.pose["x"] = data["stateEstimate.x"]
        self.pose["y"] = data["stateEstimate.y"]
        self.pose["z"] = data["stateEstimate.z"]
        self.pose["yaw"] = data["stateEstimate.yaw"]
        self.battery_v = round(data["pm.vbat"], 2)
        self.last_log = time.monotonic()
        if self.recording and "range.zrange" in data:
            self.zrange.append((self.pose["x"], self.pose["y"], data["range.zrange"] / 1000.0))

    def age_s(self):
        return None if not self.last_log else round(time.monotonic() - self.last_log, 2)

    def snapshot(self, state):
        out = {"ok": True, "x": round(self.pose["x"], 3), "y": round(self.pose["y"], 3),
               "z": round(self.pose["z"], 3), "yaw": round(self.pose["yaw"], 1),
               "battery_v": self.battery_v, "state": state}
        if not MOCK:
            age = self.age_s()
            out["age_s"] = age
            if age is None or age > POSE_STALE_S:
                out.update(ok=False, error=f"no pose from {self.uri} for {age}s")
            elif not _sane(self.pose):
                hint = self.lighthouse_report().get("hint", "check the deck can see the base stations; "
                                                             "power-cycle if it stays")
                out.update(ok=False, error=f"pose is far outside the cage: Lighthouse tracking lost ({hint})")
        return out


class Scout:
    """The flying drone. One motion command at a time; /land preempts whatever is running."""

    def __init__(self, link: Link):
        self.link = link
        self.state = "landed"                 # landed | flying (docked after /dock)
        self.takeoff_pos = None               # where /dock returns to
        self.busy = threading.Lock()          # held by takeoff/goto/scan/dock
        self.gen = 0                          # bumped by /land; stale motion stops
        self.gen_lock = threading.Lock()
        if not MOCK:
            from cflib.utils.reset_estimator import reset_estimator
            reset_estimator(link.scf)
            self.hlc = link.scf.cf.high_level_commander
        else:
            self.link.pose.update(x=CAGE["x0"], y=CAGE["y0"])
            self.link.battery_v = 4.0

    def _cur_gen(self):
        with self.gen_lock:
            return self.gen

    def _wait(self, seconds, gen):
        """Sleep in slices; False if /land interrupted."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._cur_gen() != gen:
                return False
            time.sleep(0.05)
        return self._cur_gen() == gen

    def _arm(self):
        try:                                   # Brushless; harmless on older firmware
            self.link.scf.cf.supervisor.send_arming_request(True)
            time.sleep(1.0)
        except Exception as e:                 # noqa: BLE001
            print(f"Arming request failed (continuing): {e}")

    def _disarm(self):
        try:                                   # Brushless idles its props while armed, even on the floor
            self.link.scf.cf.supervisor.send_arming_request(False)
        except Exception as e:                 # noqa: BLE001
            print(f"Disarm request failed: {e}")

    def _takeoff(self, z, gen):
        if self.state == "flying":
            return None
        if self.link.battery_v is not None and self.link.battery_v < MIN_BATTERY_V:
            return f"battery {self.link.battery_v} V is below {MIN_BATTERY_V} V"
        if not (snap := self.pose())["ok"]:
            return f"refusing takeoff: {snap['error']}"
        if (err := _outside_cage(snap["x"], snap["y"], margin=TAKEOFF_MARGIN_M)):
            return f"refusing takeoff: {err} (takeoff allows {TAKEOFF_MARGIN_M} m more)"
        if self.takeoff_pos is None or self.state == "docked":    # home = the pad, not wherever /land left it
            self.takeoff_pos = {"x": round(self.link.pose["x"], 3), "y": round(self.link.pose["y"], 3)}
            print(f"Home (dock target) set to {self.takeoff_pos}")
        print(f"Taking off to z={z}")
        if MOCK:
            self.link.pose["z"] = z
            self.state = "flying"
            return None
        self._arm()
        if self._cur_gen() != gen:             # /land arrived while arming
            self._disarm()
            return "interrupted by land"
        duration = max(2.0, z / SPEED)
        self.hlc.takeoff(z, duration)
        self.state = "flying"
        if not self._wait(duration + 0.3, gen):
            return "interrupted by land"
        return None

    def _goto(self, x, y, z, gen):
        p = self.link.pose
        dist = math.dist((p["x"], p["y"], p["z"]), (x, y, z))
        duration = max(1.0, dist / SPEED)
        if self._cur_gen() != gen:
            return "interrupted by land"
        if MOCK:
            if not self._wait(duration / MOCK_TIME_SCALE, gen):
                return "interrupted by land"
            p.update(x=x, y=y, z=z)
            return None
        self.hlc.go_to(x, y, z, 0.0, duration, relative=False)
        if not self._wait(duration + 0.2, gen):
            return "interrupted by land"
        return None

    def _motion(self, fn):
        """Run fn(gen) under the busy lock; refuse if another motion is running."""
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "error": "busy: another flight command is running (use /land to abort)"}
        try:
            return fn(self._cur_gen())
        finally:
            self.busy.release()

    # ── commands ─────────────────────────────────────────────────────────────

    def takeoff(self, z):
        if not 0.1 <= z <= CAGE["z_max"]:
            return {"ok": False, "error": f"z={z:.2f} is outside 0.1..{CAGE['z_max']}"}

        def run(gen):
            err = self._takeoff(z, gen)
            return {"ok": False, "error": err, "pose": self.pose()} if err else {"ok": True, "pose": self.pose()}
        return self._motion(run)

    def goto(self, x, y, z):
        if (err := _outside_cage(x, y, z)):
            return {"ok": False, "error": err}
        if self.state != "flying":
            return {"ok": False, "error": "not flying: call /takeoff first"}

        def run(gen):
            err = self._goto(x, y, z, gen)
            return {"ok": False, "error": err, "pose": self.pose()} if err else {"ok": True, "pose": self.pose()}
        return self._motion(run)

    def scan(self, x0, y0, x1, y1):
        lx, hx, ly, hy = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        for cx, cy in ((lx, ly), (hx, hy)):
            if (err := _outside_cage(cx, cy)):
                return {"ok": False, "error": err, "findings": []}
        pts, cy, flip = [], ly, False
        while cy <= hy + 1e-6:
            pts += [(hx, cy), (lx, cy)] if flip else [(lx, cy), (hx, cy)]
            cy, flip = cy + LANE_M, not flip

        def run(gen):
            if (err := self._takeoff(FLY_Z, gen)):
                return {"ok": False, "error": err, "findings": []}
            print(f"Scanning {len(pts)} waypoints over ({lx},{ly})-({hx},{hy})")
            self.link.zrange.clear()
            self.link.recording = True
            try:
                for px, py in pts:
                    if (err := self._goto(px, py, FLY_Z, gen)):
                        return {"ok": False, "error": err, "findings": []}
            finally:
                self.link.recording = False
            if MOCK:
                f = {"id": "f1", "x": round((lx + hx) / 2, 2), "y": round((ly + hy) / 2, 2), "dz": 0.15, "by": "mock"}
                return {"ok": True, "findings": [f]}
            return {"ok": True, "findings": _zrange_findings(self.link.zrange, self.link.uri)}
        return self._motion(run)

    def dock(self):
        """Back to the home pad (first takeoff spot) and land. Works from the air or after /land elsewhere."""
        if self.state == "docked":
            return {"ok": True, "docked": True, "pose": self.pose()}
        if self.takeoff_pos is None:
            return {"ok": False, "docked": False, "error": "no home recorded: the drone has not taken off yet"}

        def run(gen):
            tx, ty = self.takeoff_pos["x"], self.takeoff_pos["y"]
            if self.state != "flying" and math.dist((self.link.pose["x"], self.link.pose["y"]), (tx, ty)) < 0.1:
                self.state = "docked"                  # already sitting on the pad
                return {"ok": True, "docked": True, "pose": self.pose()}
            if (err := self._takeoff(FLY_Z, gen)):
                return {"ok": False, "docked": False, "error": err}
            cruise_z = max(self.link.pose["z"], 0.3)
            print(f"Docking: back to ({tx}, {ty}) at z={cruise_z:.2f}, then down to {DOCK_Z}")
            for z in (cruise_z, DOCK_Z):              # across at height, then descend over the pad
                if (err := self._goto(tx, ty, z, gen)):
                    return {"ok": False, "docked": False, "error": err}
            self._send_land(duration=DOCK_Z / 0.15)
            if not self._wait(DOCK_Z / 0.15 + 0.3, gen):
                return {"ok": False, "docked": False, "error": "interrupted by land"}
            self.state = "docked"
            return {"ok": True, "docked": True, "pose": self.pose()}
        return self._motion(run)

    def land(self):
        """Preempt any running motion and land in place. Returns immediately."""
        with self.gen_lock:
            self.gen += 1
        if self.state != "flying":
            return {"ok": True, "state": self.state}
        print("Landing in place")
        self._send_land(duration=2.0)
        self.state = "landed"
        return {"ok": True, "state": "landed"}

    def _send_land(self, duration):
        if MOCK:
            self.link.pose["z"] = 0.0
            return
        self.hlc.land(0.0, duration)

        def stop_after():
            time.sleep(duration + 0.5)
            self.hlc.stop()
            self._disarm()
        threading.Thread(target=stop_after, daemon=True).start()

    def pose(self):
        return {**self.link.snapshot(self.state), "home": self.takeoff_pos}    # home: where /dock returns to


def _zrange_findings(samples, by):
    """One hit: the centroid of samples that dip more than DIP_M below the median floor distance."""
    if not samples:
        return []
    base = sorted(s[2] for s in samples)[len(samples) // 2]
    blob = [s for s in samples if base - s[2] > DIP_M]
    if not blob:
        return []
    hit = {"id": "f1", "x": round(sum(s[0] for s in blob) / len(blob), 2),
           "y": round(sum(s[1] for s in blob) / len(blob), 2),
           "dz": round(base - min(s[2] for s in blob), 3), "by": by}
    print(f"Found object at ({hit['x']}, {hit['y']}), dz={hit['dz']} m")
    return [hit]


# ── startup ──────────────────────────────────────────────────────────────────

if not MOCK:
    import cflib.crtp
    print("Initializing cflib drivers...")
    cflib.crtp.init_drivers()

scout = Scout(Link(SCOUT_URI, with_zrange=True))
target = None
if MOCK:
    target = Link("mock-target", with_zrange=False)
    tx, ty = (float(v) for v in os.getenv("MOCK_TARGET", "1.0,0.3").split(","))
    target.pose.update(x=tx, y=ty)
    target.battery_v = 4.0
elif TARGET_URI:
    try:
        target = Link(TARGET_URI, with_zrange=False)
    except Exception as e:                    # noqa: BLE001  the scout must still fly without the target
        print(f"Target {TARGET_URI} not reachable: {e}")
        target = None
print(f"Geofence: {CAGE}")


# ── endpoints ────────────────────────────────────────────────────────────────

def _f(name, default):
    return float(request.args.get(name, default))


@app.errorhandler(Exception)
def _on_error(e):
    print(f"ERROR {request.path}: {e!r}")
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/health")
def health():
    return jsonify({"ok": True, "mock": MOCK, "cage": CAGE,
                    "scout": scout.link.snapshot(scout.state).get("error") or "connected",
                    "scout_lighthouse": None if MOCK else scout.link.lighthouse_report(),
                    "target": ("connected" if target else "none") if not target or MOCK
                    else target.snapshot("landed").get("error") or "connected"})


@app.route("/pose")
def pose():
    """?cf=scout|target. {ok, x, y, z, yaw, battery_v, state}; the scout adds home (its pad, null before takeoff)."""
    if request.args.get("cf", "scout") == "target":
        if target is None:
            return jsonify({"ok": False, "error": "no target drone"})
        return jsonify(target.snapshot("landed"))
    return jsonify(scout.pose())


@app.route("/takeoff")
def takeoff():
    return jsonify(scout.takeoff(_f("z", FLY_Z)))


@app.route("/goto")
def goto():
    """Absolute x, y, z in the Lighthouse frame. Blocks until arrival or /land."""
    p = scout.link.pose
    return jsonify(scout.goto(_f("x", p["x"]), _f("y", p["y"]), _f("z", FLY_Z)))


@app.route("/scan")
def scan():
    """Lawnmower sweep at FLY_Z; the drone keeps hovering afterwards (call /land or /dock)."""
    return jsonify(scout.scan(_f("x0", CAGE["x0"]), _f("y0", CAGE["y0"]), _f("x1", CAGE["x1"]), _f("y1", CAGE["y1"])))


@app.route("/land")
def land():
    """Land in place. Returns immediately and aborts any running /goto, /scan or /dock."""
    return jsonify(scout.land())


@app.route("/dock")
def dock():
    """Fly back to the home pad (first takeoff spot) and land; takes off first if it was landed elsewhere."""
    return jsonify(scout.dock())


if __name__ == "__main__":
    print(f"Starting Drone Server (MOCK={MOCK}) on port {PORT}...")
    # threaded=True so /land can cut into a blocking /goto or /scan
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
