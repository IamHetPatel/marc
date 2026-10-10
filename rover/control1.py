import io
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import av
from PIL import Image

# RoboMaster EP Wi-Fi connection
HOST = "192.168.2.1"
PORT = 40923
VIDEO_PORT = 40921

# Metres per second. Positive x is forward, positive y is to the robot's left.
SPEED = 0.4
# Degrees per second. Positive z rotates the rover to its left.
YAW = 60
# Metres. I key runs a one-shot chassis move this far forward.
MOVE_DISTANCE = 1.0
# Relative arm move in millimetres, 1 mm accuracy — same as:
# robotic_arm_ctrl.move(40, 50, True)
# https://robomaster-dev.readthedocs.io/en/latest/python/robotic_arm.html
# Text SDK: robotic_arm move x <mm> y <mm>
# Positive x = forward, positive y = up. After each ok move, that pose is center.
ARM_MOVE_UP = (40, 50)
ARM_MOVE_DOWN = (-40, -50)
# Smaller 1 mm-grid fallbacks if the example step is outside reach.
ARM_FALLBACKS = {
    "up": ((20, 30), (10, 20), (0, 20), (20, 0), (5, 5), (1, 1)),
    "down": ((-20, -30), (-10, -20), (0, -20), (-20, 0), (-5, -5), (-1, -1)),
}
# If the arm is stuck near the far edge on connect, pull it back here first.
ARM_SAFE = (100, 50)
DISPLAY_SIZE = (960, 540)
GUI_PORT = 8765

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>RoboMaster</title>
<style>
  body {
    margin: 0;
    background: #121212;
    color: #f2f2f2;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  main {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 16px;
  }
  img {
    width: min(960px, 100vw);
    aspect-ratio: 16 / 9;
    background: #000;
    object-fit: contain;
  }
  #status { font-size: 22px; margin: 0; }
  #hint, #grip-hint { margin: 0; color: #b5b5b5; font-size: 15px; }
  #grip-hint { margin-bottom: 8px; }
  .controls { display: flex; gap: 12px; }
  button {
    background: #2c2c2c;
    color: white;
    border: 0;
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 15px;
  }
</style>
</head>
<body>
<main>
  <img id="video" alt="Robot camera" src="/video">
  <p id="status">Starting camera...</p>
  <p id="hint">W forward &nbsp; S back &nbsp; A left &nbsp; D right &nbsp; Q rotate left &nbsp; E rotate right &nbsp; I move 1 m &nbsp; Space stop</p>
  <p id="grip-hint">O open &nbsp; C close &nbsp; Up arrow arm forward/up &nbsp; Down arrow arm back/down</p>
  <div class="controls">
    <button type="button" id="open">Open gripper</button>
    <button type="button" id="close">Close gripper</button>
    <button type="button" id="raise">Arm up</button>
    <button type="button" id="lower">Arm down</button>
  </div>
</main>
<script>
const held = new Set();
const driveKeys = new Set(["w", "a", "s", "d", "q", "e"]);

function sendGripper(action) {
  fetch("/gripper", {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: action
  }).catch(() => {});
}

document.getElementById("open").addEventListener("click", () => sendGripper("open"));
document.getElementById("close").addEventListener("click", () => sendGripper("close"));
document.getElementById("raise").addEventListener("click", () => sendArm("up"));
document.getElementById("lower").addEventListener("click", () => sendArm("down"));

function sendArm(direction) {
  fetch("/arm", {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: direction
  }).catch(() => {});
}

function sendMove() {
  held.clear();
  fetch("/move", {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: "forward"
  }).catch(() => {});
}

function sendDrive() {
  fetch("/drive", {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: [...held].join("")
  }).catch(() => {});
}

addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  if (key === " ") {
    held.clear();
    sendDrive();
    event.preventDefault();
    return;
  }
  if (key === "i") {
    event.preventDefault();
    if (!event.repeat) sendMove();
    return;
  }
  if (key === "o" || key === "c") {
    event.preventDefault();
    if (!event.repeat) sendGripper(key === "o" ? "open" : "close");
    return;
  }
  if (key === "arrowup" || key === "arrowdown") {
    event.preventDefault();
    if (!event.repeat) sendArm(key === "arrowup" ? "up" : "down");
    return;
  }
  if (!driveKeys.has(key) || event.repeat) return;
  event.preventDefault();
  held.add(key);
  sendDrive();
});

addEventListener("keyup", (event) => {
  const key = event.key.toLowerCase();
  if (!held.delete(key)) return;
  event.preventDefault();
  sendDrive();
});

addEventListener("blur", () => {
  if (held.size === 0) return;
  held.clear();
  sendDrive();
});

async function refreshStatus() {
  try {
    const response = await fetch("/status");
    document.getElementById("status").textContent = await response.text();
  } catch (error) {
    document.getElementById("status").textContent = "Reconnecting...";
  }
}
setInterval(refreshStatus, 400);
refreshStatus();
</script>
</body>
</html>
"""


def send(sock, command, quiet=False):
    """Send a command to the robot and return its response."""
    sock.sendall((command + ";").encode("utf-8"))
    response = sock.recv(1024).decode("utf-8").strip()
    if not quiet:
        print(f"{command} -> {response}")
    return response


def chassis_speed(sock, x, y, z=0):
    response = send(sock, f"chassis speed x {x:.1f} y {y:.1f} z {z:.0f}", quiet=True)
    if not response.startswith("ok"):
        print(f"Movement was not accepted: {response}")
    return response


def move_forward_result(sock, server):
    """Drive straight forward a fixed distance, then stop."""
    if server.current != (0.0, 0.0, 0.0):
        chassis_speed(sock, 0, 0, 0)
        server.current = (0.0, 0.0, 0.0)
    server.direction = f"Moving {MOVE_DISTANCE:g} m forward"
    response = send(
        sock,
        f"chassis move x {MOVE_DISTANCE:.1f} y 0 z 0 vxy {SPEED:.1f}",
    )
    server.current = (0.0, 0.0, 0.0)
    if not response.startswith("ok"):
        server.direction = "Stopped"
        return f"Move 1 m failed: {response}"
    server.direction = "Stopped"
    return f"Moved {MOVE_DISTANCE:g} m forward"


def direction_name(x, y, z):
    if x == 0 and y == 0 and z == 0:
        return "Stopped"
    parts = []
    if x > 0:
        parts.append("Forward")
    elif x < 0:
        parts.append("Backward")
    if y > 0:
        parts.append("left")
    elif y < 0:
        parts.append("right")
    if z > 0:
        parts.append("rotate left")
    elif z < 0:
        parts.append("rotate right")
    return " ".join(parts)


def gripper_result(sock, action):
    """Open or close the gripper at the lowest force, then read its position."""
    response = send(sock, f"robotic_gripper {action} 1", quiet=True)
    if not response.startswith("ok"):
        return f"Gripper {action} failed: {response}"

    time.sleep(1.5)
    status = send(sock, "robotic_gripper status ?", quiet=True)
    code = status.replace(";", "").strip()
    labels = {"0": "closed", "1": "partly open", "2": "open"}
    if code in labels:
        return f"Gripper {labels[code]}"
    return f"Gripper status: {status}"


def read_arm_position(sock):
    """Return the arm's x, y in millimetres, or None if the reply is not a pose."""
    response = send(sock, "robotic_arm position ?", quiet=True)
    parts = response.replace(";", "").split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def recover_arm(sock):
    """If the arm is near the edge of reach, moveto a known-good mid pose."""
    pos = read_arm_position(sock)
    if pos is None:
        return None
    x, y = pos
    if x < 150 and 20 < y < 120:
        return pos
    response = send(
        sock,
        f"robotic_arm moveto x {ARM_SAFE[0]} y {ARM_SAFE[1]}",
    )
    if response.startswith("ok"):
        time.sleep(2)
        return read_arm_position(sock) or ARM_SAFE
    for dx, dy in ((-40, 50), (-20, 30), (-40, 0), (0, 20)):
        response = send(sock, f"robotic_arm move x {int(dx)} y {int(dy)}")
        if response.startswith("ok"):
            time.sleep(1.5)
            return read_arm_position(sock)
    return read_arm_position(sock)


def arm_result(sock, direction, server):
    """Relative move like robotic_arm_ctrl.move(x, y, wait_for_complete=True).

    Units are millimetres with 1 mm accuracy. On success, wait for the move,
    read the pose, and treat that as the new center.
    """
    primary = ARM_MOVE_UP if direction == "up" else ARM_MOVE_DOWN
    steps = (primary,) + ARM_FALLBACKS[direction]
    last = "fail;"
    for dx, dy in steps:
        dx, dy = int(dx), int(dy)
        response = send(sock, f"robotic_arm move x {dx} y {dy}")
        last = response
        if not response.startswith("ok"):
            continue
        # wait_for_complete=True equivalent for the text SDK
        time.sleep(1.5)
        pos = read_arm_position(sock)
        if pos is not None:
            server.arm_center = (int(round(pos[0])), int(round(pos[1])))
            return (
                f"Arm moved {dx:+d},{dy:+d} mm → "
                f"center ({server.arm_center[0]} mm forward, "
                f"{server.arm_center[1]} mm up)"
            )
        return f"Arm moved {dx:+d},{dy:+d} mm"
    return f"Arm {direction} failed: {last}"


def speed_for_keys(keys):
    x = (SPEED if "w" in keys else 0) - (SPEED if "s" in keys else 0)
    y = (SPEED if "a" in keys else 0) - (SPEED if "d" in keys else 0)
    z = (YAW if "q" in keys else 0) - (YAW if "e" in keys else 0)
    return x, y, z


class CameraFeed:
    """Read the robot's H.264 camera stream and keep the newest JPEG."""

    def __init__(self):
        self.jpeg = None
        self.message = "Starting camera..."
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._container = None
        resample = getattr(Image, "Resampling", Image)
        self._resample = resample.BILINEAR

    def start(self, host):
        threading.Thread(target=self._run, args=(host,), daemon=True).start()

    def stop(self):
        self._stop.set()
        container = self._container
        if container is not None:
            try:
                container.close()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            return self.jpeg, self.message

    def _update(self, jpeg=None, message=None):
        with self._lock:
            if jpeg is not None:
                self.jpeg = jpeg
                self.message = ""
            if message is not None:
                self.message = message

    def _open(self, host):
        url = f"tcp://{host}:{VIDEO_PORT}"
        options = {
            "fflags": "nobuffer",
            "flags": "low_delay",
            "probesize": "32768",
            "analyzeduration": "0",
        }
        try:
            return av.open(url, mode="r", format="h264", timeout=(8, 8), options=options)
        except av.FFmpegError:
            return av.open(url, mode="r", timeout=(8, 8), options=options)

    def _run(self, host):
        try:
            container = self._open(host)
        except Exception as exc:
            self._update(message=f"Camera unavailable: {exc}")
            return

        self._container = container
        try:
            for frame in container.decode(video=0):
                if self._stop.is_set():
                    break
                image = frame.to_image().resize(DISPLAY_SIZE, self._resample)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=75)
                self._update(jpeg=buffer.getvalue())
        except Exception as exc:
            if not self._stop.is_set():
                self._update(message=f"Camera stopped: {exc}")
        finally:
            try:
                container.close()
            except Exception:
                pass


class RobotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, sock, camera):
        self.sock = sock
        self.camera = camera
        self.direction = "Stopped"
        self.gripper = ""
        self.arm_center = None
        self.current = (0.0, 0.0, 0.0)
        self.last_contact = time.time()
        self.control_lock = threading.Lock()
        super().__init__(address, RobotHandler)


class RobotHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send(self, code, body, content_type):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.last_contact = time.time()
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if self.path == "/status":
            _, message = self.server.camera.snapshot()
            text = message or self.server.direction
            if self.server.gripper:
                text = f"{text}  ·  {self.server.gripper}"
            self._send(200, text, "text/plain; charset=utf-8")
            return
        if self.path == "/video":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            try:
                while True:
                    jpeg, _ = self.server.camera.snapshot()
                    if jpeg is not None and jpeg is not last:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        last = jpeg
                    else:
                        time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", "replace")
        self.server.last_contact = time.time()
        if self.path == "/gripper":
            action = body.strip().lower()
            server = self.server
            if action not in ("open", "close"):
                self.send_error(400)
                return
            with server.control_lock:
                try:
                    server.gripper = gripper_result(server.sock, action)
                except OSError as exc:
                    server.gripper = f"Gripper failed: {exc}"
            self._send(200, server.gripper, "text/plain; charset=utf-8")
            return
        if self.path == "/arm":
            direction = body.strip().lower()
            server = self.server
            if direction not in ("up", "down"):
                self.send_error(400)
                return
            with server.control_lock:
                try:
                    server.gripper = arm_result(server.sock, direction, server)
                except OSError as exc:
                    server.gripper = f"Gripper failed: {exc}"
            self._send(200, server.gripper, "text/plain; charset=utf-8")
            return
        if self.path == "/move":
            server = self.server
            with server.control_lock:
                try:
                    server.gripper = move_forward_result(server.sock, server)
                except OSError as exc:
                    server.gripper = f"Move failed: {exc}"
                    server.direction = "Stopped"
                    server.current = (0.0, 0.0, 0.0)
            self._send(200, server.gripper, "text/plain; charset=utf-8")
            return
        if self.path != "/drive":
            self.send_error(404)
            return
        keys = {char for char in body.lower() if char in "wasdqe"}
        target = speed_for_keys(keys)
        server = self.server
        with server.control_lock:
            if target != server.current:
                try:
                    chassis_speed(server.sock, *target)
                    server.current = target
                    server.direction = direction_name(*target)
                except OSError as exc:
                    server.direction = f"Control lost: {exc}"
        self._send(200, "ok", "text/plain; charset=utf-8")


def watch_connection(server, stop):
    """Stop the robot if the browser page goes away while it is moving."""
    while not stop.is_set():
        time.sleep(0.2)
        moving = server.current != (0.0, 0.0, 0.0)
        quiet = time.time() - server.last_contact > 1.5
        if not moving or not quiet:
            continue
        with server.control_lock:
            if server.current == (0.0, 0.0, 0.0):
                continue
            try:
                chassis_speed(server.sock, 0, 0, 0)
            except OSError:
                pass
            server.current = (0.0, 0.0, 0.0)
            server.direction = "Stopped"


def serve_gui(sock, camera, arm_center=None):
    port = GUI_PORT
    server = None
    for candidate in range(GUI_PORT, GUI_PORT + 20):
        try:
            server = RobotServer(("127.0.0.1", candidate), sock, camera)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("No free port for the camera window")

    server.arm_center = arm_center

    stop = threading.Event()
    threading.Thread(target=watch_connection, args=(server, stop), daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"Camera window: {url}")
    print("Click the page, then hold W, A, S, D, Q, or E. Ctrl-C quits.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def main():
    print("Connecting to DJI RoboMaster...")

    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.settimeout(5)

        response = send(sock, "command")
        if not response.startswith("ok"):
            raise RuntimeError("Unable to enable SDK mode")

        print("Successfully connected!")
        send(sock, "version")
        send(sock, "robot battery ?")
        send(sock, "robot mode free")
        arm_center = recover_arm(sock)
        if arm_center:
            print(
                "Arm center is "
                f"({arm_center[0]:.0f} mm forward, {arm_center[1]:.0f} mm up). "
                "Up/down moves relative to that pose."
            )
        else:
            print("Could not read arm position yet; relative moves still work.")

        response = send(sock, "stream on")
        if not response.startswith("ok"):
            print("Camera stream was not accepted.")

        camera = CameraFeed()
        camera.start(HOST)
        try:
            serve_gui(sock, camera, arm_center)
        finally:
            camera.stop()
            try:
                chassis_speed(sock, 0, 0, 0)
                send(sock, "stream off", quiet=True)
            except OSError:
                pass


if __name__ == "__main__":
    main()
