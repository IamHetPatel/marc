"""
fleet_agent.py — robot-agnostic LLM/VLM agent layer for the Mobile Robotics Hackathon.

Mac-friendly: talks to ROS 2 over rosbridge (websocket), so no local ROS install / DDS discovery needed.
On the robot or NUC:  ros2 launch rosbridge_server rosbridge_websocket_launch.xml
On your laptop:       pip install roslibpy google-genai
                      export GEMINI_API_KEY=...  ROBOTS="tb=192.168.1.20:9090,ep=192.168.1.21:9090"
                      python fleet_agent.py "Find the red box and tell me which robot is closer to it"
Set MOCK=1 to develop without hardware.

Before trusting topic names/types, check on the robot:  ros2 topic list -t
(e.g. some Jazzy stacks use geometry_msgs/TwistStamped on /cmd_vel; camera topic names differ per robot).
"""
import base64, math, os, sys, time

MOCK = os.getenv("MOCK") == "1"
MODEL = os.getenv("MODEL", "gemini-robotics-er-2-preview")  # fallback: gemini-robotics-er-1.5-preview

if not MOCK:
    import roslibpy


class Robot:
    """One adapter per robot. Swap topic names per platform; tools stay identical."""

    def __init__(self, name, host, port, cmd_vel="/cmd_vel", cam="/camera/image_raw/compressed",
                 goal="/goal_pose", twist_type="geometry_msgs/Twist"):
        self.name, self._last_jpeg = name, None
        if MOCK:
            return
        self.ros = roslibpy.Ros(host=host, port=port)
        self.ros.run(timeout=10)
        self.cmd = roslibpy.Topic(self.ros, cmd_vel, twist_type)
        self.goal = roslibpy.Topic(self.ros, goal, "geometry_msgs/PoseStamped")  # Nav2 bt_navigator listens here
        cam_t = roslibpy.Topic(self.ros, cam, "sensor_msgs/CompressedImage", throttle_rate=500)
        cam_t.subscribe(lambda m: setattr(self, "_last_jpeg", base64.b64decode(m["data"])))

    def drive(self, linear, angular, seconds):
        if MOCK:
            return f"[mock] {self.name} drove v={linear} w={angular} for {seconds}s"
        msg = {"linear": {"x": linear, "y": 0.0, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": angular}}
        end = time.time() + min(seconds, 5.0)  # hard safety cap
        while time.time() < end:
            self.cmd.publish(roslibpy.Message(msg))
            time.sleep(0.1)
        self.cmd.publish(roslibpy.Message({"linear": {"x": 0.0, "y": 0.0, "z": 0.0},
                                           "angular": {"x": 0.0, "y": 0.0, "z": 0.0}}))
        return f"{self.name} done"

    def go_to(self, x, y, yaw):
        if MOCK:
            return f"[mock] {self.name} navigating to ({x},{y})"
        self.goal.publish(roslibpy.Message({
            "header": {"frame_id": "map"},
            "pose": {"position": {"x": x, "y": y, "z": 0.0},
                     "orientation": {"x": 0.0, "y": 0.0, "z": math.sin(yaw / 2), "w": math.cos(yaw / 2)}}}))
        return f"{self.name} goal sent to ({x},{y}); poll status/camera to confirm arrival"

    def snapshot(self):
        return self._last_jpeg


def build_tools(fleet):
    """Plain Python functions -> google-genai auto function calling. Docstrings become tool descriptions."""

    def drive(robot: str, linear_mps: float, angular_radps: float, seconds: float) -> str:
        """Drive a robot open-loop. Keep |linear|<=0.2 and seconds<=5 indoors."""
        return fleet[robot].drive(max(-0.2, min(0.2, linear_mps)), angular_radps, seconds)

    def go_to(robot: str, x: float, y: float, yaw: float) -> str:
        """Send a Nav2 goal in the map frame (metres, radians)."""
        return fleet[robot].go_to(x, y, yaw)

    def list_robots() -> str:
        """List available robot names."""
        return ", ".join(fleet)

    return [drive, go_to, list_robots]


def run(task):
    from google import genai
    from google.genai import types

    spec = os.getenv("ROBOTS", "tb=localhost:9090")
    fleet = {}
    for item in spec.split(","):
        name, addr = item.split("=")
        host, port = addr.split(":")
        fleet[name] = Robot(name, host, int(port))

    client = genai.Client()
    parts = [types.Part.from_text(text=f"Task: {task}\nRobots: {', '.join(fleet)}. "
                                         "Plan step by step, call tools, verify with the images.")]
    for name, r in fleet.items():
        img = r.snapshot()
        if img:
            parts += [types.Part.from_text(text=f"Camera of {name}:"),
                      types.Part.from_bytes(data=img, mime_type="image/jpeg")]

    resp = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(tools=build_tools(fleet), temperature=0.2),
    )
    print(resp.text)


if __name__ == "__main__":
    run(" ".join(sys.argv[1:]) or "List the robots and spin the first one 90 degrees left.")
