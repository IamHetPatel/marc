import os
import time
import io
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
# Run in mock mode by default on Mac, Windows users can override this when they connect the robot
MOCK = os.getenv("MOCK", "1") == "1"

# Mock state
mock_pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
mock_gripper = "open"

if not MOCK:
    # Windows teammates will initialize the real RoboMaster SDK here
    print("Initializing real RoboMaster EP...")
    # from robomaster import robot
    # ep_robot = robot.Robot()
    # ep_robot.initialize(conn_type="sta")
    pass

@app.route("/drive")
def drive():
    """Move relative to current pose. x=forward(m), y=left(m), z=turn_left(deg)"""
    x = float(request.args.get("x", 0.0))
    y = float(request.args.get("y", 0.0))
    z = float(request.args.get("z", 0.0))
    
    if MOCK:
        mock_pose["x"] += x
        mock_pose["y"] += y
        mock_pose["yaw"] += z
        print(f"[MOCK] Drove x={x}, y={y}, z={z}. New pose: {mock_pose}")
        return jsonify({"status": "mock_ok", "moved": {"x": x, "y": y, "z": z}})
    
    # ep_robot.chassis.move(x=x, y=y, z=z, xy_speed=0.5).wait_for_completed()
    return jsonify({"status": "ok"})

@app.route("/speed")
def speed():
    """Set wheels speed. x=forward, y=left, z=turn. Used by fleet.py for smooth control."""
    x = float(request.args.get("x", 0.0))
    y = float(request.args.get("y", 0.0))
    z = float(request.args.get("z", 0.0))
    t = float(request.args.get("t", 0.0)) # timeout
    
    if MOCK:
        print(f"[MOCK] Setting speed x={x}, y={y}, z={z} for {t}s")
        return jsonify({"status": "mock_ok"})
    
    # ep_robot.chassis.drive_speed(x=x, y=y, z=z)
    # time.sleep(t)
    return jsonify({"status": "ok"})

@app.route("/stop")
def stop():
    """Stop the robot immediately."""
    if MOCK:
        print("[MOCK] Stopped.")
        return jsonify({"status": "mock_ok"})
    
    # ep_robot.chassis.drive_speed(x=0, y=0, z=0)
    return jsonify({"status": "ok"})

@app.route("/grip")
def grip():
    """Open or close the gripper. action='open' or 'close'"""
    action = request.args.get("action", "open")
    if MOCK:
        global mock_gripper
        mock_gripper = action
        print(f"[MOCK] Gripper is now {action}")
        return jsonify({"status": "mock_ok", "gripper": action})
    
    # if action == "open":
    #     ep_robot.gripper.open(power=50)
    # else:
    #     ep_robot.gripper.close(power=50)
    # time.sleep(1)
    return jsonify({"status": "ok"})

@app.route("/arm")
def arm():
    """Move the arm relative to its current position. x=forward(mm), y=up(mm)"""
    x = float(request.args.get("x", 0.0))
    y = float(request.args.get("y", 0.0))
    if MOCK:
        print(f"[MOCK] Moved arm forward {x}mm, up {y}mm")
        return jsonify({"status": "mock_ok"})
    
    # ep_robot.robotic_arm.move(x=x, y=y).wait_for_completed()
    return jsonify({"status": "ok"})

@app.route("/snapshot")
def snapshot():
    """Return the latest camera image as a JPEG."""
    if MOCK:
        print("[MOCK] Returning blank snapshot.")
        # Return a tiny valid 1x1 black JPEG for testing
        tiny_jpeg = b'\xff\xd8\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xdb\x00C\x01\t\t\t\x0c\x0b\x0c\x18\r\r\x182!\x1c!22222222222222222222222222222222222222222222222222\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb7\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06\x12AQ\x07aq\x13"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a&\'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xfd\xfc\xa2\x8a(\xa0\x0f\xff\xd9'
        return send_file(io.BytesIO(tiny_jpeg), mimetype='image/jpeg')
    
    # img = ep_robot.camera.read_cv2_image()
    # is_success, buffer = cv2.imencode(".jpg", img)
    # return send_file(io.BytesIO(buffer), mimetype='image/jpeg')
    return "ok"

@app.route("/pose")
def pose():
    """Return the current odometry pose of the robot."""
    if MOCK:
        return jsonify(mock_pose)
    
    # pos = ep_robot.chassis.get_position() # Example, depends on exact API
    # return jsonify({"x": pos[0], "y": pos[1], "yaw": pos[2]})
    return jsonify({"x": 0.0, "y": 0.0, "yaw": 0.0})

if __name__ == "__main__":
    print(f"Starting RoboMaster Server (MOCK={MOCK}) on port 8000...")
    app.run(host="0.0.0.0", port=8000, threaded=True)
