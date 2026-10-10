import os
import time
from flask import Flask, request, jsonify

app = Flask(__name__)
# Mock mode by default to unblock testing
MOCK = os.getenv("MOCK", "1") == "1"

# Mock state
mock_pose = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}

if not MOCK:
    # Initialize cflib here for the actual Crazyflie
    print("Initializing real Crazyflie drone (cflib)...")
    # import cflib.crtp
    # cflib.crtp.init_drivers()
    pass

@app.route("/takeoff")
def takeoff():
    """Take off to a default height."""
    if MOCK:
        mock_pose["z"] = 0.6
        print(f"[MOCK] Drone took off to z={mock_pose['z']}m")
        return jsonify({"status": "mock_ok"})
    
    # TODO: cflib takeoff sequence
    return jsonify({"status": "ok"})

@app.route("/land")
def land():
    """Land the drone."""
    if MOCK:
        mock_pose["z"] = 0.0
        print("[MOCK] Drone landed")
        return jsonify({"status": "mock_ok"})
    
    # TODO: cflib land sequence
    return jsonify({"status": "ok"})

@app.route("/goto")
def goto():
    """Fly to an absolute coordinate in the shared frame."""
    x = float(request.args.get("x", mock_pose["x"]))
    y = float(request.args.get("y", mock_pose["y"]))
    z = float(request.args.get("z", mock_pose["z"]))
    
    if MOCK:
        mock_pose["x"] = x
        mock_pose["y"] = y
        mock_pose["z"] = z
        print(f"[MOCK] Drone flew to x={x}, y={y}, z={z}")
        return jsonify({"status": "mock_ok"})
    
    # TODO: cflib goto
    return jsonify({"status": "ok"})

@app.route("/scan")
def scan():
    """Sweep an area and return coordinates of any objects found."""
    x0 = float(request.args.get("x0", 0.0))
    y0 = float(request.args.get("y0", 0.0))
    x1 = float(request.args.get("x1", 0.0))
    y1 = float(request.args.get("y1", 0.0))
    
    if MOCK:
        # Mock finding something right in the middle of the requested area
        mid_x = round((x0 + x1) / 2, 2)
        mid_y = round((y0 + y1) / 2, 2)
        print(f"[MOCK] Scanned area ({x0},{y0}) to ({x1},{y1}). Found object at ({mid_x}, {mid_y})")
        return jsonify({
            "status": "mock_ok", 
            "objects_found_at": [{"x": mid_x, "y": mid_y, "found_by": "mock"}]
        })
    
    # TODO: use the logic from fleet.py scout_area() here
    return jsonify({"status": "ok", "objects_found_at": []})

@app.route("/pose")
def pose():
    """Get the current position of the drone (from Lighthouse deck)."""
    if MOCK:
        return jsonify(mock_pose)
    
    # TODO: read pose from the drone's log blocks
    return jsonify({"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0})

if __name__ == "__main__":
    print(f"Starting Drone Server (MOCK={MOCK}) on port 8001...")
    app.run(host="0.0.0.0", port=8001, threaded=True)
