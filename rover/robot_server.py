"""RoboMaster EP HTTP server (WP-R1, docs/mothership-plan.md §4.1).

The mothership (manager/adapters/rover_http.py), curl and the browser teleop page all reach the robot through
this one process, because the EP takes only one SDK controller at a time. It speaks DJI's plaintext SDK over
TCP like control1.py does, so it needs no `robomaster` package and runs on macOS or Linux.

    python rover/robot_server.py                          # EP in STA mode on the hotspot: found automatically
    ROBOT_HOST=<ep-ip> python rover/robot_server.py       # or name it (skips the search)
    python rover/robot_server.py --find                   # just list the EPs on this network
    MOCK=1 python rover/robot_server.py                   # fake EP, no hardware

The server starts even if the EP is not on yet, and reconnects by itself after a battery swap or a Wi-Fi drop; while
the EP is away every call answers at once with the reason (HTTP 503, /health says why).
    curl "http://localhost:8000/drive?x=0.3"

API conventions (the contract in docs/mothership-plan.md §3.1): metres and degrees, body frame x forward,
y LEFT, z = turn LEFT (counter-clockwise). DJI documents the SDK as +y = right and +z = clockwise, so the
server negates both (SDK_Y_SIGN / SDK_Z_SIGN default to -1). Floor check (F1 step 5): /drive?y=0.3 must strafe
left seen from behind, /drive?z=45 must turn counter-clockwise seen from above; flip the env var that is wrong.
Sign check on the real EP (2026-10-10, firmware 00.00.00.89): PASSED with the defaults (forward, strafe left and
turn left all went the right way). Odometry then reported 0.273 m for a 0.2 m forward move; not yet measured.

Every endpoint is a GET returning JSON {"ok": true|false, ...}. A refused or invalid command is HTTP 200 with
"error"; HTTP 503 means the EP itself did not answer (the client turns that into RobotError).
"""
import io
import json
import math
import os
import re
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw

HOST = os.getenv("ROBOT_HOST", "auto")      # "auto": find the EP on this network (and again if its IP changes)
SDK_PORT = 40923
VIDEO_PORT = 40921
EVENT_PORT = 40925
BROADCAST_PORT = 40926
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("PORT", "8000"))
MOCK = os.getenv("MOCK") == "1"
SDK_Y_SIGN = float(os.getenv("SDK_Y_SIGN", "-1"))
SDK_Z_SIGN = float(os.getenv("SDK_Z_SIGN", "-1"))

# Safety caps live here, not in the callers (README §3.7).
MAX_SPEED = 0.5         # m/s, /speed is clamped to it, /drive?v= must be within it
MAX_YAW_RATE = 90.0     # deg/s
MAX_SPEED_T = 3.0       # s, /speed always stops by itself after min(t, this)
MAX_MOVE = 2.0          # m per /drive axis
MAX_TURN = 360.0        # deg per /drive
MAX_ARM_STEP = 100      # mm per relative /arm move
DRIVE_SPEED = 0.3       # m/s, /drive default
TURN_SPEED = 60.0       # deg/s, /drive default
GRIP_POWER = int(os.getenv("GRIP_POWER", "1"))   # 1..4; control1.py uses the gentlest
GRIP_WAIT = 1.5         # s for the gripper to finish before reading its state

# Absolute arm poses for `robotic_arm moveto`, mm (x forward, y up). PLACEHOLDERS: tune on the floor (F1 step 8)
# by nudging with /arm?x=..&y=.. and reading /arm, then write the numbers here.
ARM_PRESETS = {"home": (100, 50), "carry": (100, 120), "pick": (180, -30)}

DISPLAY_SIZE = (960, 540)   # MJPEG stream for the teleop page; /snapshot is full resolution
PAGE = Path(__file__).with_name("teleop.html")


class SdkError(Exception):
    """The EP answered, but not with ok (e.g. 'fail' for an arm pose out of reach)."""


class BadRequest(Exception):
    """Missing or out-of-range query parameter."""


# ---------------------------------------------------------------- the link to the robot

class SdkLink:
    """One TCP connection to the EP's plaintext SDK (port 40923). The lock makes every command exactly one
    request/reply, so /stop and the watchdog can interleave with a /drive that is waiting for the robot.

    A background thread keeps the link up: it dials (and with host "auto" first finds the EP on this network), and
    redials whenever a command fails. Requests never wait for that: while the EP is away they fail at once with the
    reason, so a dead battery or a Wi-Fi drop shows up on the dashboard instead of stalling every call."""

    INIT = ("robot mode free", "stream on")
    RETRY_S = 1.0

    def __init__(self, host: str, port: int = SDK_PORT, timeout: float = 5.0):
        self.auto = host == "auto"
        self.address = None if self.auto else host
        self.port, self.timeout = port, timeout
        self.error = "not connected yet"
        self._lock = threading.Lock()
        self._sock = None
        self._halt = threading.Event()

    @property
    def label(self) -> str:
        return f"{self.address or 'auto'}:{self.port}"

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def open(self, wait_s: float = 15.0) -> bool:
        """Start keeping the link up; wait up to wait_s for the first connection. False if the EP is not there yet."""
        threading.Thread(target=self._keep_connected, daemon=True, name="sdk-link").start()
        t_end = time.monotonic() + wait_s
        while not self.connected and time.monotonic() < t_end:
            time.sleep(0.1)
        return self.connected

    def close(self):
        self._halt.set()
        with self._lock:
            if self._sock is not None:
                try:
                    self._exchange(self._sock, "quit")
                except OSError:
                    pass
                self._drop()

    def cmd(self, command: str) -> str:
        with self._lock:
            if self._sock is None:
                raise ConnectionError(f"EP not connected: {self.error}")
            try:
                return self._exchange(self._sock, command)
            except OSError as e:
                self.error = str(e) or type(e).__name__
                self._drop()
                raise

    def _keep_connected(self):
        said = None
        while not self._halt.is_set():
            if self._sock is None:
                try:
                    sock = self._dial()
                except OSError as e:
                    self.error = str(e) or type(e).__name__
                    if self.error != said:                       # say each new reason once, not every second
                        print(f"EP not connected: {self.error}")
                        said = self.error
                else:
                    with self._lock:
                        self._sock, self.error = sock, None
                    print(f"EP connected at {self.label}")
                    said = None
            self._halt.wait(self.RETRY_S)

    def _dial(self) -> socket.socket:
        """A fresh, handshaken socket. With host "auto": the last address first, else whatever EP is on the network."""
        candidates = [self.address] if self.address else []
        if self.auto:
            candidates += [ip for ip in find_eps() if ip != self.address]
            if not candidates:
                raise ConnectionError("no EP found on this network (is it on, in router mode, on this hotspot?)")
        last = None
        for address in candidates:
            try:
                sock = self._handshake(address)
            except OSError as e:
                last = e
                continue
            self.address = address
            return sock
        raise last

    def _handshake(self, address: str) -> socket.socket:
        sock = socket.create_connection((address, self.port), timeout=min(self.timeout, 3.0))
        sock.settimeout(self.timeout)
        try:
            reply = self._exchange(sock, "command")
            if not reply.startswith("ok"):
                raise ConnectionError(f"EP at {address} refused SDK mode: {reply!r}")
            for command in self.INIT:
                reply = self._exchange(sock, command)
                if not reply.startswith("ok"):
                    print(f"warning: {command!r} -> {reply!r}")
        except socket.timeout:
            sock.close()
            raise ConnectionError(f"the SDK port at {address} is open but the EP ignored 'command'. Another controller "
                                  "probably holds it: force-quit the RoboMaster app / control1.py, or restart the EP") from None
        except OSError:
            sock.close()
            raise
        return sock

    def _drop(self):
        try:
            self._sock.close()
        except (OSError, AttributeError):
            pass
        self._sock = None

    def _exchange(self, sock: socket.socket, command: str) -> str:
        self._drain(sock)
        sock.sendall(command.encode("utf-8") + b";")
        data = sock.recv(1024)
        if not data:
            raise ConnectionError("EP closed the connection")
        sock.settimeout(0.2)                    # replies end with ';', wait briefly if one arrived split
        try:
            while not data.rstrip().endswith(b";"):
                more = sock.recv(1024)
                if not more:
                    break
                data += more
        except socket.timeout:
            pass
        finally:
            sock.settimeout(self.timeout)
        return data.decode("utf-8", "replace").strip().rstrip(";").strip()

    def _drain(self, sock: socket.socket):
        """Drop a late reply to an earlier command that timed out, so replies stay in step with commands."""
        sock.setblocking(False)
        try:
            while sock.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            sock.settimeout(self.timeout)


def find_eps(listen_s: float = 1.5) -> list:
    """RoboMaster EPs on this laptop's network. In router mode the EP broadcasts "robot ip <addr>" on UDP 40926; when
    that does not arrive (many hotspots drop broadcasts) the local /24 is scanned for its two SDK ports (40923
    commands, 40925 events). Only TCP connects, nothing is sent, so a running controller is not disturbed."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", BROADCAST_PORT))
            s.settimeout(listen_s)
            data, (sender, _) = s.recvfrom(1024)
            m = re.search(rb"(\d+\.\d+\.\d+\.\d+)", data)
            return [m.group(1).decode() if m else sender]
    except OSError:
        pass
    me = _lan_ip()
    if me == "localhost":
        return []
    prefix = me.rsplit(".", 1)[0]

    def is_ep(host: str) -> bool:
        for port in (SDK_PORT, EVENT_PORT):
            try:
                socket.create_connection((host, port), timeout=0.4).close()
            except OSError:
                return False
        return True
    hosts = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != me]
    with ThreadPoolExecutor(max_workers=64) as pool:
        return [h for h, ok in zip(hosts, pool.map(is_ep, hosts)) if ok]


class FakeEP:
    """Answers plaintext SDK commands like an EP, from a kinematic model in the SDK's own frame
    (x forward, y RIGHT, yaw clockwise). Used by MOCK=1 and the tests; touches no hardware."""

    label = "mock EP"
    address = None
    connected = True
    error = None
    ARM_REACH = ((70, 230), (-60, 160))     # mm; outside it the arm says 'fail', like the real one near the edge

    def __init__(self):
        self.log = []
        self.x = self.y = self.yaw = 0.0
        self.vel = (0.0, 0.0, 0.0)
        self.move = None
        self.arm = [100, 50]
        self.gripper = "closed"
        self.battery = 87
        self._grip_at = -1e9
        self._t = time.monotonic()
        self._lock = threading.Lock()

    def open(self, wait_s: float = 0.0) -> bool:
        self.cmd("command")
        return True

    def close(self):
        pass

    def cmd(self, command: str) -> str:
        with self._lock:
            self._advance()
            self.log.append(command)
            try:
                return self._reply(command.split())
            except (ValueError, IndexError):
                return "error"

    def describe(self) -> str:
        return (f"MOCK EP   x {self.x:+.2f} m   y {self.y:+.2f} m (SDK, +right)   yaw {self.yaw:+.0f} deg (SDK, +cw)"
                f"   gripper {self.gripper}   arm {self.arm[0]},{self.arm[1]} mm")

    def _advance(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        if self.move:
            t0, duration, (x0, y0, yaw0), (bx, by, dz) = self.move
            f = min(1.0, (now - t0) / duration)
            c, s = math.cos(math.radians(yaw0)), math.sin(math.radians(yaw0))
            self.x, self.y = x0 + f * (c * bx - s * by), y0 + f * (s * bx + c * by)
            self.yaw = yaw0 + f * dz
            if f >= 1.0:
                self.move = None
        elif any(self.vel):
            vx, vy, vz = self.vel
            steps = max(1, int(dt / 0.01))
            h = dt / steps
            for _ in range(steps):
                c, s = math.cos(math.radians(self.yaw)), math.sin(math.radians(self.yaw))
                self.x += (c * vx - s * vy) * h
                self.y += (s * vx + c * vy) * h
                self.yaw += vz * h

    def _reply(self, w):
        head = " ".join(w[:2])
        kv = lambda: dict(zip(w[2::2], map(float, w[3::2])))  # noqa: E731
        if w in (["command"], ["quit"]) or head in ("robot mode", "stream on", "stream off"):
            return "ok"
        if w[0] == "version":
            return "version 00.00.00.00"
        if head == "robot battery":
            return str(self.battery)
        if head == "chassis speed":
            a = kv()
            self.move, self.vel = None, (a.get("x", 0.0), a.get("y", 0.0), a.get("z", 0.0))
            return "ok"
        if head == "chassis move":
            a = kv()
            x, y, z = a.get("x", 0.0), a.get("y", 0.0), a.get("z", 0.0)
            duration = max(math.hypot(x, y) / a.get("vxy", 0.5), abs(z) / a.get("vz", 30.0), 0.01)
            self.vel = (0.0, 0.0, 0.0)
            self.move = (time.monotonic(), duration, (self.x, self.y, self.yaw), (x, y, z))
            return "ok"
        if head == "chassis position":
            return f"{self.x:.3f} {self.y:.3f} {self.yaw:.2f}"
        if head == "robotic_gripper status":
            if time.monotonic() - self._grip_at < 1.0:
                return "1"
            return "2" if self.gripper == "open" else "0"
        if w[0] == "robotic_gripper" and w[1] in ("open", "close"):
            self.gripper, self._grip_at = ("open" if w[1] == "open" else "closed"), time.monotonic()
            return "ok"
        if head == "robotic_arm position":
            return f"{self.arm[0]} {self.arm[1]}"
        if head in ("robotic_arm moveto", "robotic_arm move"):
            a = kv()
            x, y = int(a.get("x", 0)), int(a.get("y", 0))
            target = [x, y] if w[1] == "moveto" else [self.arm[0] + x, self.arm[1] + y]
            (x0, x1), (y0, y1) = self.ARM_REACH
            if not (x0 <= target[0] <= x1 and y0 <= target[1] <= y1):
                return "fail"
            self.arm = target
            return "ok"
        return "error"


# ---------------------------------------------------------------- robot behaviour

def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _clamp(v: float, cap: float) -> float:
    return max(-cap, min(cap, v))


def _f(v: float, digits: int = 3) -> str:
    return f"{(round(v, digits) or 0.0):.{digits}f}"     # never send "-0.000"


def _relative(p, ref) -> dict:
    """Pose p expressed in the frame of pose ref (both (x, y_left, yaw_ccw))."""
    dx, dy = p[0] - ref[0], p[1] - ref[1]
    c, s = math.cos(math.radians(ref[2])), math.sin(math.radians(ref[2]))
    return {"x": round(c * dx + s * dy, 3), "y": round(-s * dx + c * dy, 3), "yaw": round(_wrap(p[2] - ref[2]), 1)}


class Rover:
    """API semantics on top of a link (SdkLink or FakeEP): sign conversion, caps, blocking moves, auto-stop."""

    def __init__(self, link):
        self.link = link
        self.motion = threading.Lock()      # one long action at a time; /stop and /pose never take it
        self.cancel = threading.Event()     # set by /stop, interrupts a /drive, /grip or /arm that is waiting
        self.speed_until = None             # monotonic deadline of the active /speed
        self.velocity = (0.0, 0.0, 0.0)
        self.origin = None                  # odometry zero: the first pose read (or /pose?reset=1)
        self.last = "ready"
        self._battery = (None, -1e9)
        self.drive_speed, self.turn_speed = DRIVE_SPEED, TURN_SPEED
        self.grip_wait, self.poll_s = GRIP_WAIT, 0.1

    def setup(self):
        try:
            self.pose(reset=True)
        except (OSError, SdkError):
            pass                            # EP not there yet: the first pose that arrives becomes the zero

    def shutdown(self):
        try:
            self._halt()
            self.link.cmd("stream off")
        except (OSError, SdkError):
            pass

    # --- SDK helpers

    def _ok(self, command: str):
        reply = self.link.cmd(command)
        if not reply.lower().startswith("ok"):
            raise SdkError(f"robot said {reply!r} to {command!r}")

    def _query(self, command: str) -> list:
        reply = self.link.cmd(command)
        try:
            return [float(v) for v in reply.split()]
        except ValueError:
            raise SdkError(f"robot said {reply!r} to {command!r}") from None

    def _abs_pose(self):
        x, y, yaw = self._query("chassis position ?")[:3]
        return x, SDK_Y_SIGN * y, SDK_Z_SIGN * yaw

    def _arm_pos(self):
        x, y = self._query("robotic_arm position ?")[:2]
        return x, y

    def _speed_cmd(self, x, y, z) -> str:
        return f"chassis speed x {_f(x)} y {_f(SDK_Y_SIGN * y)} z {_f(SDK_Z_SIGN * z, 1)}"

    def _halt(self):
        self.speed_until, self.velocity = None, (0.0, 0.0, 0.0)
        self._ok("chassis speed x 0 y 0 z 0")

    def _busy(self) -> dict:
        return {"ok": False, "error": "busy: another drive, grip or arm action is running (try /stop)"}

    def _wait_settled(self, read, expected_s: float, tol) -> str:
        """Poll `read` until it stops changing (3 polls in a row) and at least half the expected time passed.
        The plaintext SDK says ok when it accepts a move, not when the move ends."""
        t0 = time.monotonic()
        deadline = t0 + 2 * expected_s + 3.0
        prev, still = read(), 0
        while True:
            if self.cancel.wait(self.poll_s):
                return "stopped"
            cur = read()
            still = 0 if any(abs(a - b) > t for a, b, t in zip(cur, prev, tol)) else still + 1
            prev = cur
            now = time.monotonic()
            if still >= 3 and now - t0 >= 0.5 * expected_s:
                return "done"
            if now > deadline:
                return "timeout"

    def moving(self) -> bool:
        return self.speed_until is not None or self.motion.locked()

    # --- endpoints

    def battery(self) -> Optional[int]:
        value, t = self._battery
        if time.monotonic() - t > 5.0 or not self.link.connected:
            value = int(self._query("robot battery ?")[0])
            self._battery = (value, time.monotonic())
        return value

    def _rel(self, p) -> dict:
        if self.origin is None:
            self.origin = p
        return _relative(p, self.origin)

    def pose(self, reset: bool = False) -> dict:
        p = self._abs_pose()
        if reset:
            self.origin = p
        return {"ok": True, **self._rel(p)}

    def stop(self) -> dict:
        self.cancel.set()
        self._halt()
        self.last = "stopped"
        return {"ok": True}

    def speed(self, x: float, y: float, z: float, t: float) -> dict:
        if t <= 0:
            raise BadRequest("t must be > 0 seconds")
        x, y, z = _clamp(x, MAX_SPEED), _clamp(y, MAX_SPEED), _clamp(z, MAX_YAW_RATE)
        t = min(t, MAX_SPEED_T)
        if not self.motion.acquire(blocking=False):
            return self._busy()
        try:
            if (x, y, z) == (0.0, 0.0, 0.0):
                self._halt()
            else:
                if (x, y, z) != self.velocity or self.speed_until is None:
                    self._ok(self._speed_cmd(x, y, z))
                    self.velocity = (x, y, z)
                self.speed_until = time.monotonic() + t
        finally:
            self.motion.release()
        self.last = f"speed x {x:g} y {y:g} z {z:g} for {t:g} s"
        return {"ok": True, "x": x, "y": y, "z": z, "t": t}

    def drive(self, x: float, y: float, z: float, v: Optional[float] = None, vz: Optional[float] = None) -> dict:
        if abs(x) > MAX_MOVE or abs(y) > MAX_MOVE:
            raise BadRequest(f"x and y must be within ±{MAX_MOVE:g} m")
        if abs(z) > MAX_TURN:
            raise BadRequest(f"z must be within ±{MAX_TURN:g} deg")
        if v is not None and not 0 < v <= MAX_SPEED:
            raise BadRequest(f"v must be in (0, {MAX_SPEED:g}] m/s")
        if vz is not None and not 0 < vz <= MAX_YAW_RATE:
            raise BadRequest(f"vz must be in (0, {MAX_YAW_RATE:g}] deg/s")
        v, vz = v or self.drive_speed, vz or self.turn_speed
        if not self.motion.acquire(blocking=False):
            return self._busy()
        try:
            self.cancel.clear()
            if self.speed_until is not None:
                self._halt()
            start = self._abs_pose()
            if (x, y, z) != (0.0, 0.0, 0.0):
                self._ok(f"chassis move x {_f(x)} y {_f(SDK_Y_SIGN * y)} z {_f(SDK_Z_SIGN * z, 1)}"
                         f" vxy {_f(v, 2)} vz {_f(vz, 1)}")
                expected = max(math.hypot(x, y) / v, abs(z) / vz)
                result = self._wait_settled(self._abs_pose, expected, (0.003, 0.003, 0.5))
            else:
                result = "done"
            end = self._abs_pose()
        finally:
            self.motion.release()
        moved = _relative(end, start)
        if result == "timeout":
            self._halt()
        if result != "done":
            self.last = f"drive {result}"
            return {"ok": False, "error": "stopped" if result == "stopped" else "move did not finish in time",
                    "moved": moved}
        self.last = f"drove x {moved['x']:+.2f} m, y {moved['y']:+.2f} m, turned {moved['yaw']:+.0f} deg"
        return {"ok": True, "moved": moved, "pose": self._rel(end)}

    def grip(self, action: str) -> dict:
        if action not in ("open", "close"):
            raise BadRequest("action must be open or close")
        if not self.motion.acquire(blocking=False):
            return self._busy()
        try:
            self.cancel.clear()
            self._ok(f"robotic_gripper {action} {GRIP_POWER}")
            self.cancel.wait(self.grip_wait)
            code = self.link.cmd("robotic_gripper status ?").strip()
        finally:
            self.motion.release()
        state = {"0": "closed", "1": "partly open", "2": "open"}.get(code, code)
        self.last = f"gripper {state}"
        return {"ok": True, "state": state}

    def arm(self, preset: Optional[str] = None, x: Optional[float] = None, y: Optional[float] = None) -> dict:
        if preset is None and x is None and y is None:
            ax, ay = self._arm_pos()
            return {"ok": True, "x": ax, "y": ay, "presets": ARM_PRESETS}
        if preset is not None:
            if preset not in ARM_PRESETS:
                raise BadRequest(f"preset must be one of {', '.join(ARM_PRESETS)}")
            command = "robotic_arm moveto x %d y %d" % ARM_PRESETS[preset]
        else:
            dx, dy = round(x or 0), round(y or 0)
            if abs(dx) > MAX_ARM_STEP or abs(dy) > MAX_ARM_STEP:
                raise BadRequest(f"relative arm moves must be within ±{MAX_ARM_STEP} mm")
            command = f"robotic_arm move x {dx} y {dy}"
        if not self.motion.acquire(blocking=False):
            return self._busy()
        try:
            self.cancel.clear()
            self._ok(command)
            result = self._wait_settled(self._arm_pos, 1.5, (1.0, 1.0))
            ax, ay = self._arm_pos()
        finally:
            self.motion.release()
        self.last = f"arm at {ax:.0f}, {ay:.0f} mm"
        if result != "done":
            return {"ok": False, "error": f"arm {result}", "x": ax, "y": ay}
        return {"ok": True, "x": ax, "y": ay}

    def watchdog(self, stop: threading.Event):
        """Ends every /speed at its deadline, even if no request ever arrives again."""
        while not stop.wait(0.05):
            until = self.speed_until
            if until is None or time.monotonic() < until or not self.motion.acquire(blocking=False):
                continue
            try:
                if self.speed_until is not None and time.monotonic() >= self.speed_until:
                    self._halt()
                    self.last = "stopped (speed time ran out)"
            except (OSError, SdkError) as e:
                print(f"watchdog could not stop the robot: {e}")
            finally:
                self.motion.release()


# ---------------------------------------------------------------- camera

class Camera:
    """Keeps the newest frame from `source` (an iterator of PyAV frames or PIL images) and encodes JPEG on demand,
    so decoding runs at stream rate but JPEG work happens only when someone asks."""

    def __init__(self, source, max_age: float = 2.0):
        self.source, self.max_age = source, max_age
        self.status = "starting"
        self._lock = threading.Lock()
        self._frame, self._t, self._seq, self._cache = None, 0.0, 0, {}
        self._stop = threading.Event()

    def start(self) -> "Camera":
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                for frame in self.source():
                    if self._stop.is_set():
                        return
                    with self._lock:
                        self._frame, self._t, self._cache = frame, time.monotonic(), {}
                        self._seq += 1
                    self.status = "live"
                self.status = "camera stream ended"
            except Exception as e:                      # PyAV raises many types; keep retrying
                self.status = f"camera error: {e}"
            self._stop.wait(1.0)

    def jpeg(self, size=None):
        """(seq, JPEG bytes), or (seq, None) when there is no recent frame."""
        with self._lock:
            if self._frame is None or time.monotonic() - self._t > self.max_age:
                return self._seq, None
            if size not in self._cache:
                img = self._frame.to_image() if hasattr(self._frame, "to_image") else self._frame
                if size:
                    img = img.resize(size, Image.BILINEAR)
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                self._cache[size] = buf.getvalue()
            return self._seq, self._cache[size]


def h264_frames(host: Optional[str]):
    if host is None:
        raise ConnectionError("waiting for the EP")       # auto mode before the first connection; the camera retries
    import av                                           # only the real robot needs PyAV
    url = f"tcp://{host}:{VIDEO_PORT}"
    options = {"fflags": "nobuffer", "flags": "low_delay", "probesize": "32768", "analyzeduration": "0"}
    try:
        container = av.open(url, mode="r", format="h264", timeout=(8, 8), options=options)
    except av.FFmpegError:
        container = av.open(url, mode="r", timeout=(8, 8), options=options)
    try:
        yield from container.decode(video=0)
    finally:
        container.close()


def fake_frames(ep: FakeEP, fps: float = 10.0):
    while True:
        img = Image.new("RGB", (1280, 720), (70, 76, 82))
        ImageDraw.Draw(img).text((40, 40), ep.describe(), fill=(235, 235, 235))
        yield img
        time.sleep(1.0 / fps)


# ---------------------------------------------------------------- HTTP

_REQUIRED = object()


def _num(q: dict, name: str, default=_REQUIRED):
    if name not in q:
        if default is _REQUIRED:
            raise BadRequest(f"missing parameter {name}")
        return default
    try:
        value = float(q[name])
    except ValueError:
        raise BadRequest(f"{name} must be a number, got {q[name]!r}") from None
    if not math.isfinite(value):
        raise BadRequest(f"{name} must be finite")
    return value


def _health(s, q):
    """Always answers while the server runs: with the EP away it says why (ok: false), so the dashboard can tell
    "rover server down" from "EP off or out of range"."""
    r = s.rover
    base = {"camera": s.camera.status, "robot": r.link.label, "mock": s.mock, "moving": r.moving(), "last": r.last}
    if not r.link.connected:
        return {"ok": False, "error": f"EP not connected: {r.link.error}", "battery": None, **base}
    return {"ok": True, "battery": r.battery(), **base}


ROUTES = {
    "/health": _health,
    "/pose": lambda s, q: s.rover.pose(reset=q.get("reset") in ("1", "true", "yes")),
    "/stop": lambda s, q: s.rover.stop(),
    "/speed": lambda s, q: s.rover.speed(_num(q, "x", 0.0), _num(q, "y", 0.0), _num(q, "z", 0.0), _num(q, "t", 1.0)),
    "/drive": lambda s, q: s.rover.drive(_num(q, "x", 0.0), _num(q, "y", 0.0), _num(q, "z", 0.0),
                                         _num(q, "v", None), _num(q, "vz", None)),
    "/grip": lambda s, q: s.rover.grip(q.get("action", "").lower()),
    "/arm": lambda s, q: s.rover.arm(q.get("preset"), _num(q, "x", None), _num(q, "y", None)),
}
QUIET = {"/health", "/pose", "/speed"}      # polled or repeated many times a second; don't flood the terminal


class RobotServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, rover: Rover, camera: Camera, link_label: str, mock: bool = False):
        self.rover, self.camera, self.link_label, self.mock = rover, camera, link_label, mock
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "robot_server"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if path == "/":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if path == "/video":
            return self._mjpeg()
        if path == "/snapshot":
            _, jpeg = self.server.camera.jpeg()
            if jpeg is None:
                return self._json({"ok": False, "error": f"no camera frame ({self.server.camera.status})"}, 503)
            return self._send(200, jpeg, "image/jpeg")
        route = ROUTES.get(path)
        if route is None:
            return self._json({"ok": False, "error": f"no endpoint {path}", "endpoints": sorted(ROUTES) + [
                "/snapshot", "/video"]}, 404)
        try:
            body = route(self.server, q)
        except (BadRequest, SdkError) as e:
            body = {"ok": False, "error": str(e)}
        except OSError as e:
            body = {"ok": False, "error": f"robot unreachable: {e}"}
            print(f"{self.path} -> {body['error']}")
            return self._json(body, 503)
        if path not in QUIET:
            print(f"{self.path} -> {json.dumps(body)}")
        self._json(body)

    def _json(self, body: dict, code: int = 200):
        self._send(code, json.dumps(body).encode("utf-8"), "application/json")

    def _send(self, code: int, data: bytes, content_type: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True       # the client gave up waiting; nothing to report

    def _mjpeg(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                seq, jpeg = self.server.camera.jpeg(DISPLAY_SIZE)
                if jpeg is None or seq == last:
                    time.sleep(0.03)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
                last = seq
        except (BrokenPipeError, ConnectionResetError):
            pass


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))                # no packet is sent; just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main():
    if "--find" in sys.argv[1:]:
        print("Looking for RoboMaster EPs on this network ...")
        found = find_eps()
        print("\n".join(found) if found else "none found (is the EP on, in router mode, on this hotspot?)")
        return
    link = FakeEP() if MOCK else SdkLink(HOST)
    rover = Rover(link)
    camera = Camera((lambda: fake_frames(link)) if MOCK else (lambda: h264_frames(link.address)))
    try:
        server = RobotServer((HTTP_HOST, HTTP_PORT), rover, camera, link.label, MOCK)
    except OSError as e:
        sys.exit(f"Cannot listen on port {HTTP_PORT} ({e}). Is another robot_server running? Set PORT=... to use another.")
    print("Looking for the EP on this network ..." if HOST == "auto" and not MOCK else f"Connecting to {link.label} ...")
    if link.open():
        rover.setup()
        print(f"Connected to {link.label}. Battery {rover.battery()}%.")
    else:
        print(f"EP not reachable yet ({link.error}).\nServing anyway; the link keeps trying and connects when the EP "
              "appears (ROBOT_HOST=<ip> skips the search).")
    camera.start()
    stop = threading.Event()
    threading.Thread(target=rover.watchdog, args=(stop,), daemon=True).start()
    base = f"http://{_lan_ip()}:{HTTP_PORT}"
    print(f"Rover API on {base}   teleop page: {base}/   try: curl {base}/health\nCtrl-C stops the robot and quits.")
    signal.signal(signal.SIGTERM, signal.default_int_handler)     # `kill` also stops the robot on the way out
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        camera.stop()
        rover.shutdown()
        server.server_close()
        link.close()


if __name__ == "__main__":
    main()
