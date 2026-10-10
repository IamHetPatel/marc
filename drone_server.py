import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
# Change MOCK to "0" by default now since you have the drone connected!
MOCK = os.getenv("MOCK", "0") == "1"

URI = "radio://0/80/2M/E7E7E7E704"
FLY_Z = 0.6  # Default flight height in metres
DIP_M = 0.12 # zrange dip to detect an object

mock_pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}

# Globals for real drone state
scf = None
pc = None
drone_pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}
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
    
    # Arming request (required for Brushless Crazyflie)
    try:
        scf.cf.platform.send_arming_request(True)
        time.sleep(1.0)
    except AttributeError:
        pass

    def _log_cb(timestamp, data, logconf):
        drone_pose["x"] = data["stateEstimate.x"]
        drone_pose["y"] = data["stateEstimate.y"]
        drone_pose["z"] = data["stateEstimate.z"]
        drone_pose["yaw"] = data["stateEstimate.yaw"]
        if "range.zrange" in data:
            zrange_samples.append((drone_pose["x"], drone_pose["y"], data["range.zrange"] / 1000.0))

    lg = LogConfig(name="state", period_in_ms=100)
    lg.add_variable("stateEstimate.x", "float")
    lg.add_variable("stateEstimate.y", "float")
    lg.add_variable("stateEstimate.z", "float")
    lg.add_variable("stateEstimate.yaw", "float")
    lg.add_variable("range.zrange", "uint16_t") # mm
    lg.data_received_cb.add_callback(_log_cb)
    scf.cf.log.add_config(lg)
    lg.start()


@app.route("/takeoff")
def takeoff():
    """Take off to a default height."""
    global pc, stop_flag
    stop_flag = False
    if MOCK:
        mock_pose["z"] = FLY_Z
        print(f"[MOCK] Drone took off to z={mock_pose['z']}m")
        return jsonify({"status": "mock_ok"})
    
    if pc is None:
        print("Taking off...")
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(height=FLY_Z, velocity=0.5)
    return jsonify({"status": "ok"})

@app.route("/land")
def land():
    """Land the drone."""
    global pc, stop_flag
    stop_flag = True
    if MOCK:
        mock_pose["z"] = 0.0
        print("[MOCK] Drone landed")
        return jsonify({"status": "mock_ok"})
    
    if pc is not None:
        print("Landing...")
        pc.land(velocity=0.5)  # land() only takes velocity, not (height, velocity)
        pc = None
    return jsonify({"status": "ok"})

@app.route("/goto")
def goto():
    """Fly to an absolute coordinate in the shared frame."""
    global pc, stop_flag
    stop_flag = False
    x = float(request.args.get("x", mock_pose["x"] if MOCK else drone_pose["x"]))
    y = float(request.args.get("y", mock_pose["y"] if MOCK else drone_pose["y"]))
    z = float(request.args.get("z", mock_pose["z"] if MOCK else drone_pose["z"]))
    
    if MOCK:
        mock_pose["x"] = x
        mock_pose["y"] = y
        mock_pose["z"] = z
        print(f"[MOCK] Drone flew to x={x}, y={y}, z={z}")
        return jsonify({"status": "mock_ok"})
    
    if pc is None:
        # Auto-takeoff if they try to move while landed
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(FLY_Z, 1.0)
        
    print(f"Flying to {x}, {y}, {z}")
    pc.go_to(x, y, z)
    return jsonify({"status": "ok"})

@app.route("/scan")
def scan():
    """Sweep an area and return coordinates of any objects found based on Z-range dip."""
    global pc, stop_flag
    stop_flag = False
    x0 = float(request.args.get("x0", 0.0))
    y0 = float(request.args.get("y0", 0.0))
    x1 = float(request.args.get("x1", 0.0))
    y1 = float(request.args.get("y1", 0.0))
    
    if MOCK:
        mid_x = round((x0 + x1) / 2, 2)
        mid_y = round((y0 + y1) / 2, 2)
        print(f"[MOCK] Scanned area ({x0},{y0}) to ({x1},{y1}). Found object at ({mid_x}, {mid_y})")
        return jsonify({
            "status": "mock_ok", 
            "objects_found_at": [{"x": mid_x, "y": mid_y, "found_by": "mock"}]
        })
    
    if pc is None:
        pc = PositionHlCommander(scf, default_height=FLY_Z, default_velocity=0.3)
        pc.take_off(FLY_Z, 1.0)

    # Generate lawnmower path
    pts, y, flip = [], min(y0, y1), False
    while y <= max(y0, y1) + 1e-6:
        a, b = (min(x0, x1), max(x0, x1)) if not flip else (max(x0, x1), min(x0, x1))
        pts += [(a, y), (b, y)]
        y, flip = y + 0.4, not flip
    
    print(f"Scanning {len(pts)} points...")
    zrange_samples.clear()
    
    for px, py in pts:
        if stop_flag:
            print("Scan aborted by emergency stop!")
            if pc:
                pc.land(velocity=0.5)
                pc = None
            return jsonify({"status": "aborted", "objects_found_at": []})
        pc.go_to(px, py, FLY_Z)
        
    if pc:
        pc.land(velocity=0.5)
        pc = None
    
    # Process the scan to find obstacles
    if not zrange_samples:
        return jsonify({"status": "ok", "objects_found_at": []})
        
    base = sorted(s[2] for s in zrange_samples)[len(zrange_samples) // 2]  # median floor distance
    blob = [s for s in zrange_samples if base - s[2] > DIP_M]
    
    hits = []
    if blob:
        hit_x = round(sum(s[0] for s in blob) / len(blob), 2)
        hit_y = round(sum(s[1] for s in blob) / len(blob), 2)
        hits.append({"x": hit_x, "y": hit_y, "found_by": URI})
        print(f"Found object at {hit_x}, {hit_y}")
        
    return jsonify({"status": "ok", "objects_found_at": hits})

@app.route("/dock")
def dock():
    """Return to origin and land (docking station)."""
    global pc, stop_flag
    stop_flag = True # abort any scans
    
    if MOCK:
        mock_pose["x"] = 0.0
        mock_pose["y"] = 0.0
        mock_pose["z"] = 0.0
        print("[MOCK] Drone docked.")
        return jsonify({"status": "mock_ok"})
        
    if pc is None:
        return jsonify({"status": "already_docked"})
        
    print("Docking (returning to 0,0)...")
    pc.go_to(0.0, 0.0, FLY_Z)
    pc.land(velocity=0.5)
    pc = None
    return jsonify({"status": "ok"})

@app.route("/pose")
def pose():
    """Get the current position of the drone."""
    if MOCK:
        return jsonify(mock_pose)
    return jsonify(drone_pose)

if __name__ == "__main__":
    print(f"Starting Drone Server (MOCK={MOCK}) on port 8001...")
    # Threading is required so /land can interrupt a blocking /scan loop
    app.run(host="0.0.0.0", port=8001, threaded=True, debug=False, use_reloader=False)
