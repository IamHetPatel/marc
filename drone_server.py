import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
MOCK = os.getenv("MOCK", "0") == "1"

URI = "radio://0/80/2M/E7E7E7E704"  # confirmed by cfclient (address ends in 04)
FLY_Z = 0.6   # Default flight height in metres
DOCK_Z = 0.27 # Plate height from arena.json plate_offset.dock_z
DIP_M = 0.12  # zrange dip (metres) to detect an object below

# --- Mock state ---
mock_pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "battery_v": 4.0, "state": "docked"}

# --- Real drone state ---
scf = None
pc = None
drone_pose   = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
drone_battery_v = 4.0          # updated by log callback
drone_state  = "docked"        # "docked" | "flying" | "landed"
takeoff_pos  = {"x": 0.0, "y": 0.0}  # saved at takeoff → used by /dock
zrange_samples = []
stop_flag = False

if not MOCK:
    import cflib.crtp
    from cflib.crazyflie import Crazyflie
    from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
    from cflib.positioning.position_hl_commander import PositionHlCommander
    from cflib.crazyflie.log import LogConfig
    from cflib.utils.reset_estimator import reset_estimator

    print("Initializing cflib drivers...")
    cflib.crtp.init_drivers()
    print(f"Connecting to {URI}...")
    scf = SyncCrazyflie(URI, cf=Crazyflie(rw_cache="./cache"))
    scf.open_link()
    print("Connected!")

    reset_estimator(scf)

    # Arming request (Brushless Crazyflie 2.1 — firmware v10 uses legacy fallback)
    try:
        scf.cf.supervisor.send_arming_request(True)
        time.sleep(1.0)
    except Exception:
        pass

    def _log_cb(timestamp, data, logconf):
        global drone_battery_v
        drone_pose["x"]   = data.get("stateEstimate.x",   drone_pose["x"])
        drone_pose["y"]   = data.get("stateEstimate.y",   drone_pose["y"])
        drone_pose["z"]   = data.get("stateEstimate.z",   drone_pose["z"])
        drone_pose["yaw"] = data.get("stateEstimate.yaw", drone_pose["yaw"])
        if "pm.vbat" in data:
            drone_battery_v = round(data["pm.vbat"], 2)
        if "range.zrange" in data:
            zrange_samples.append((drone_pose["x"], drone_pose["y"],
                                   data["range.zrange"] / 1000.0))

    lg = LogConfig(name="state", period_in_ms=100)
    lg.add_variable("stateEstimate.x",   "float")
    lg.add_variable("stateEstimate.y",   "float")
    lg.add_variable("stateEstimate.z",   "float")
    lg.add_variable("stateEstimate.yaw", "float")
    lg.add_variable("pm.vbat",           "float")   # battery voltage
    lg.add_variable("range.zrange",      "uint16_t") # mm → object detection
    lg.data_received_cb.add_callback(_log_cb)
    scf.cf.log.add_config(lg)
    lg.start()


# ── helpers ──────────────────────────────────────────────────────────────────

def _full_pose():
    """Return the full pose dict the AI Manager expects."""
    if MOCK:
        return mock_pose.copy()
    return {
        "x":         round(drone_pose["x"],   3),
        "y":         round(drone_pose["y"],   3),
        "z":         round(drone_pose["z"],   3),
        "yaw":       round(drone_pose["yaw"], 1),
        "battery_v": drone_battery_v,
        "state":     drone_state,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.route("/takeoff")
def takeoff():
    """Take off from the current position (= the plate). Saves the position for /dock."""
    global pc, stop_flag, drone_state, takeoff_pos
    stop_flag = False

    if MOCK:
        mock_pose["z"]     = FLY_Z
        mock_pose["state"] = "flying"
        print(f"[MOCK] Took off to z={FLY_Z}m")
        return jsonify({"status": "mock_ok", "pose": _full_pose()})

    if pc is None:
        # Save the current x,y so /dock can return here (= plate on the rover)
        takeoff_pos = {"x": round(drone_pose["x"], 3), "y": round(drone_pose["y"], 3)}
        print(f"Taking off from {takeoff_pos}...")
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(height=FLY_Z, velocity=0.5)
        drone_state = "flying"
    return jsonify({"status": "ok", "pose": _full_pose()})


@app.route("/land")
def land():
    """Land in place (emergency fallback — use /dock to return to plate)."""
    global pc, stop_flag, drone_state
    stop_flag = True   # also aborts any running /scan

    if MOCK:
        mock_pose["z"]     = 0.0
        mock_pose["state"] = "landed"
        print("[MOCK] Landed in place.")
        return jsonify({"status": "mock_ok"})

    if pc is not None:
        print("Landing in place...")
        pc.land(velocity=0.5)
        pc = None
        drone_state = "landed"
    return jsonify({"status": "ok"})


@app.route("/goto")
def goto():
    """Fly to an absolute coordinate in the Lighthouse frame."""
    global pc, stop_flag, drone_state
    stop_flag = False
    x = float(request.args.get("x", mock_pose["x"] if MOCK else drone_pose["x"]))
    y = float(request.args.get("y", mock_pose["y"] if MOCK else drone_pose["y"]))
    z = float(request.args.get("z", FLY_Z))

    if MOCK:
        mock_pose.update({"x": x, "y": y, "z": z, "state": "flying"})
        print(f"[MOCK] Flew to x={x}, y={y}, z={z}")
        return jsonify({"status": "mock_ok", "pose": _full_pose()})

    if pc is None:
        takeoff_pos.update({"x": round(drone_pose["x"], 3), "y": round(drone_pose["y"], 3)})
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(height=FLY_Z, velocity=0.5)
        drone_state = "flying"

    print(f"Flying to ({x}, {y}, {z})...")
    pc.go_to(x, y, z)
    return jsonify({"status": "ok", "pose": _full_pose()})


@app.route("/scan")
def scan():
    """Lawnmower sweep, returns findings in the format the AI Manager expects."""
    global pc, stop_flag, drone_state
    stop_flag = False
    x0 = float(request.args.get("x0", 0.0))
    y0 = float(request.args.get("y0", 0.0))
    x1 = float(request.args.get("x1", 2.0))
    y1 = float(request.args.get("y1", 2.0))

    if MOCK:
        mid_x = round((x0 + x1) / 2, 2)
        mid_y = round((y0 + y1) / 2, 2)
        print(f"[MOCK] Scanned ({x0},{y0})→({x1},{y1}). Hit at ({mid_x},{mid_y})")
        return jsonify({
            "status": "mock_ok",
            # AI Manager expects "findings" with id, x, y, dz
            "findings": [{"id": "f1", "x": mid_x, "y": mid_y, "dz": 0.15, "by": "mock"}]
        })

    if pc is None:
        takeoff_pos.update({"x": round(drone_pose["x"], 3), "y": round(drone_pose["y"], 3)})
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(height=FLY_Z, velocity=0.5)
        drone_state = "flying"

    # Build lawnmower path (0.4 m lanes)
    pts, cy, flip = [], min(y0, y1), False
    while cy <= max(y0, y1) + 1e-6:
        a = min(x0, x1) if not flip else max(x0, x1)
        b = max(x0, x1) if not flip else min(x0, x1)
        pts += [(a, cy), (b, cy)]
        cy, flip = cy + 0.4, not flip

    print(f"Scanning {len(pts)} waypoints...")
    zrange_samples.clear()

    for px, py in pts:
        if stop_flag:
            print("Scan aborted by stop flag!")
            if pc:
                pc.land(velocity=0.5)
                pc = None
                drone_state = "landed"
            return jsonify({"status": "aborted", "findings": []})
        pc.go_to(px, py, FLY_Z)

    if pc:
        pc.land(velocity=0.5)
        pc = None
        drone_state = "landed"

    if not zrange_samples:
        return jsonify({"status": "ok", "findings": []})

    # Detect objects by Z-range dip below the median (floor distance)
    base = sorted(s[2] for s in zrange_samples)[len(zrange_samples) // 2]
    blob = [s for s in zrange_samples if base - s[2] > DIP_M]

    findings = []
    if blob:
        hit_x = round(sum(s[0] for s in blob) / len(blob), 2)
        hit_y = round(sum(s[1] for s in blob) / len(blob), 2)
        dz    = round(base - min(s[2] for s in blob), 3)  # how high the object is
        findings.append({"id": "f1", "x": hit_x, "y": hit_y, "dz": dz, "by": URI})
        print(f"Found object at ({hit_x}, {hit_y}), dz={dz}m")

    return jsonify({"status": "ok", "findings": findings})


@app.route("/dock")
def dock():
    """Return to the saved takeoff position (the plate on the rover) and land."""
    global pc, stop_flag, drone_state
    stop_flag = True   # abort any running scan

    if MOCK:
        mock_pose.update({"x": 0.0, "y": 0.0, "z": DOCK_Z, "state": "docked"})
        print("[MOCK] Drone docked on plate.")
        return jsonify({"status": "mock_ok", "docked": True})

    if pc is None:
        drone_state = "docked"
        return jsonify({"status": "already_docked", "docked": True})

    tx, ty = takeoff_pos["x"], takeoff_pos["y"]
    print(f"Docking: returning to takeoff pos ({tx}, {ty}) at z={DOCK_Z}...")
    pc.go_to(tx, ty, DOCK_Z)     # descend to plate height first
    pc.land(velocity=0.3)        # slow land onto the plate
    pc = None
    drone_state = "docked"
    return jsonify({"status": "ok", "docked": True, "pose": _full_pose()})


@app.route("/pose")
def pose():
    """Full pose including battery_v and state — as required by the AI Manager."""
    return jsonify(_full_pose())


if __name__ == "__main__":
    print(f"Starting Drone Server (MOCK={MOCK}) on port 8001...")
    # threaded=True lets /land interrupt a blocking /scan
    app.run(host="0.0.0.0", port=8001, threaded=True, debug=False, use_reloader=False)
