"""One command for the whole system: the rover server, the drone server and the mothership, with their logs in one
terminal and a safe Ctrl-C (rover stopped and scout landed before anything shuts down).

Lenient on purpose: a piece that is missing a package, or does not come up, is reported in one line and skipped;
everything else keeps running and the dashboard shows that robot offline. A server already running on its port is
used as is. Routine "200 OK" request lines are hidden so real errors stay visible.

    python start.py demo      everything simulated inside the mothership (mock arena, all scenarios work)
    python start.py sim       the real rover and drone SERVERS without hardware (fake EP, no radio), floor arena:
                              exercises every HTTP hop the floor uses
    python start.py floor     real EP (found on the network) + real Crazyradio + mothership on the floor arena

    --rover  auto | <ep-ip> | mock | sim | off | http://host:8000   override the rover (sim = inside the mothership)
    --drone  radio | mock | sim | http://host:8001                  override the drone
    --camera <index> | <url> | mock                                 the railing/overhead camera (optional)
    --no-browser                                                    don't open the dashboard

Examples: real rover, no drone yet:  python start.py floor --drone mock
          the drone server runs on another laptop:  python start.py floor --drone http://10.0.0.7:8001
Standard library only, so it can tell you what is missing before anything else is installed.
"""
import argparse
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTS = {"rover": 8000, "drone": 8001, "mothership": 8002}
COLORS = {"rover": "33", "drone": "36", "mothership": "35", "start": "32"}
MODES = {                     # mode -> (rover, drone, arena)
    "demo": ("sim", "sim", None),
    "sim": ("mock", "mock", "manager/arena.floor.json"),
    "floor": ("auto", "radio", "manager/arena.floor.json"),
}
NEEDS = {                     # what each piece imports, and where it comes from
    "mothership": (["fastapi", "uvicorn", "pydantic", "requests", "PIL", "yaml"], "requirements-manager.txt"),
    "rover": (["PIL"], "rover/requirements.txt"),
    "rover-real": (["av"], "rover/requirements.txt"),
    "drone": (["flask"], "requirements-drone.txt"),
    "drone-radio": (["cflib"], "requirements-drone.txt"),
    "camera": (["cv2"], "requirements-manager.txt"),
}
QUIET = re.compile(r'"(GET|POST|HEAD) [^"]*" 2\d\d')     # a successful request: polled several times a second
TTY = sys.stdout.isatty()
_print_lock = threading.Lock()


def say(name: str, text: str):
    tag = f"{name:>10} │ "
    if TTY:
        tag = f"\033[{COLORS.get(name, '0')}m{tag}\033[0m"
    with _print_lock:
        print(tag + text, flush=True)


def is_url(v: str) -> bool:
    return v.startswith(("http://", "https://"))


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def get_json(url: str, timeout: float = 2.0):
    """-> (http status, parsed body) or (None, None) when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (OSError, ValueError):
        return None, None


class Piece:
    """One child process with its output prefixed, and a URL that says when it is ready."""

    def __init__(self, name: str, argv: list, env: dict, ready_url: str, ready_s: float, needs: list):
        self.name, self.argv, self.env, self.ready_url, self.ready_s = name, argv, env, ready_url, ready_s
        self.needs, self.proc = needs, None

    def start(self):
        kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.proc = subprocess.Popen(self.argv, cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1", **self.env},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **kw)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            if not QUIET.search(line):
                say(self.name, line.rstrip())

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self) -> bool:
        t_end = time.monotonic() + self.ready_s
        while time.monotonic() < t_end:
            if not self.alive():
                return False
            if get_json(self.ready_url, timeout=1.0)[0] is not None:
                return True
            time.sleep(0.3)
        return False

    def stop(self, grace_s: float = 8.0):
        """Ctrl-C first (each server stops its robot on the way out), then terminate, then kill."""
        if not self.alive():
            return
        try:
            if os.name == "nt":
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.proc.send_signal(signal.SIGINT)
            self.proc.wait(grace_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def missing(keys: list) -> str:
    """'' when everything imports, else what is missing and the pip line that fixes it."""
    gone, reqs = [], []
    for key in keys:
        mods, req = NEEDS[key]
        lost = [m for m in mods if importlib.util.find_spec(m) is None]
        if lost:
            gone += lost
            reqs.append(req)
    return f"missing {', '.join(gone)} (pip install -r {' -r '.join(dict.fromkeys(reqs))})" if gone else ""


def plan(args) -> tuple:
    """-> (pieces to start, the mothership's ROVER and DRONE)."""
    rover, drone, arena = MODES[args.mode]
    rover, drone = args.rover or rover, args.drone or drone
    arena = args.arena or arena
    pieces = []
    py = [sys.executable, "-u"]

    if rover in ("sim", "off") or is_url(rover):
        rover_url = "mock" if rover in ("sim", "off") else rover
    else:
        env = {"PORT": str(PORTS["rover"])}
        if rover == "mock":
            env["MOCK"] = "1"
        elif rover != "auto":
            env["ROBOT_HOST"] = rover
        pieces.append(Piece("rover", py + ["rover/robot_server.py"], env, f"http://localhost:{PORTS['rover']}/health",
                            30, ["rover"] + ([] if rover == "mock" else ["rover-real"])))
        rover_url = f"http://localhost:{PORTS['rover']}"

    if drone == "sim" or is_url(drone):
        drone_url = "mock" if drone == "sim" else drone
    else:
        env = {"PORT": str(PORTS["drone"]), "MOCK": "1" if drone == "mock" else "0"}
        if drone == "mock":
            env.setdefault("MOCK_TIME_SCALE", os.getenv("MOCK_TIME_SCALE", "5"))
        pieces.append(Piece("drone", py + ["drone_server.py"], env, f"http://localhost:{PORTS['drone']}/health", 45,
                            ["drone"] + ([] if drone == "mock" else ["drone-radio"])))
        drone_url = f"http://localhost:{PORTS['drone']}"

    env = {"PORT": str(PORTS["mothership"]), "ROVER": rover_url, "DRONE": drone_url}
    if arena:
        env["ARENA"] = str(ROOT / arena)
    needs = ["mothership"]
    if args.camera:
        env["OVERHEAD_CAM"] = args.camera
        needs.append("camera")
    if rover_url == "mock" or drone_url == "mock":
        env.setdefault("MOCK_TIME_SCALE", os.getenv("MOCK_TIME_SCALE", "5"))
    pieces.append(Piece("mothership", py + ["-m", "manager.server"], env,
                        f"http://localhost:{PORTS['mothership']}/state", 30, needs))
    return pieces, rover_url, drone_url


def summary(pieces, rover_url: str, drone_url: str):
    host = lan_ip()
    lines = [f"Dashboard   http://localhost:{PORTS['mothership']}/   (other laptops: http://{host}:{PORTS['mothership']}/)"]
    if rover_url != "mock":
        _, h = get_json(rover_url + "/health")
        state = ("EP connected, battery %s%%" % h.get("battery") if h and h.get("ok") else
                 (h or {}).get("error", "no answer"))
        lines.append(f"Rover       {rover_url}   {state}   (keyboard teleop: {rover_url}/)")
    else:
        lines.append("Rover       simulated inside the mothership")
    if drone_url != "mock":
        _, h = get_json(drone_url + "/health")
        lines.append(f"Drone       {drone_url}   {'scout ' + str(h.get('scout')) if h else 'no answer'}"
                     f"{'   (mock radio)' if h and h.get('mock') else ''}")
    else:
        lines.append("Drone       simulated inside the mothership")
    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        lines.append("AI          no GEMINI_API_KEY: built-in plans and manual control only")
    for line in lines:
        say("start", line)
    say("start", "Ctrl-C stops the rover, lands the scout and shuts everything down.")


def safe_stop(rover_url: str, drone_url: str):
    """Before any process goes away: rover zero speed, scout down. Best effort, a dead server is fine."""
    for url in ([rover_url + "/stop"] if rover_url != "mock" else []) + ([drone_url + "/land"] if drone_url != "mock" else []):
        get_json(url, timeout=1.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=sorted(MODES), default="sim")
    ap.add_argument("--rover")
    ap.add_argument("--drone")
    ap.add_argument("--camera")
    ap.add_argument("--arena", help="arena file (default: floor arena for sim/floor, mock arena for demo)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        sys.exit(f"Python 3.10+ needed (this is {sys.version.split()[0]}). On a Mac: uv venv --python 3.12 .venv")
    pieces, rover_url, drone_url = plan(args)
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    started = []
    for p in pieces:
        port = PORTS[p.name]
        if not port_free(port):
            if get_json(p.ready_url)[0] is not None:
                say("start", f"{p.name}: a server already answers on :{port}, using it")
            else:
                say("start", f"⚠ {p.name} not started: port {port} is taken by something else")
            continue
        if (gone := missing(p.needs)):
            say("start", f"⚠ {p.name} not started: {gone}")
            continue
        say("start", f"starting {p.name} ...")
        p.start()
        started.append(p)
        if not p.wait_ready():
            say("start", f"⚠ {p.name} is not answering yet ({'exited' if not p.alive() else 'still starting'}; "
                         "see its lines above). Carrying on without it.")
    if get_json(f"http://localhost:{PORTS['mothership']}/state")[0] is None:
        say("start", "✗ the mothership is not running, so there is no dashboard (see its lines above)")
    else:
        summary(pieces, rover_url, drone_url)
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{PORTS['mothership']}/")
    if started:
        while not stopping.wait(0.5):
            for p in started:
                if p.proc.poll() is not None and not getattr(p, "reported", False):
                    p.reported = True
                    say("start", f"⚠ {p.name} exited with code {p.proc.returncode}; the others keep running")
    say("start", "stopping: rover stop, scout land, then the servers ...")
    safe_stop(rover_url, drone_url)
    for p in reversed(started):
        p.stop()


if __name__ == "__main__":
    main()
