"""Fixed overhead webcam: the backup "drone camera" (4K, looking straight down on the whole field).

It does three jobs, all in the Lighthouse frame (metres):
  * calibration  ArUco tags taped flat on the floor at known positions give a pixel -> floor homography. It is
                 re-measured on every frame, so a bumped camera heals itself; with a tag hidden the last one is kept.
  * rover pose   one more tag on the rover's top plate gives an absolute x, y, yaw (localization.py uses it as a fix)
  * locate       Gemini points at a described object in the frame; the point goes through the homography

The camera can hang anywhere (ceiling, or a railing above one edge looking down at an angle): the floor tags fit a
homography for any view of a flat floor. Things above the floor (the rover tag) look further from the camera's nadir
(the floor point straight below the lens) than they are; scaling by (1 - h / camera_height) towards it removes that
parallax. For a straight-down camera the nadir is the image centre; otherwise set `camera_floor_xy`.

    python -m manager.overhead tags [DIR]               print-ready PNGs of the floor tags and the rover tag
    python -m manager.overhead check [CAM] [--save F]   open the camera, report calibration and the rover pose
"""
import logging
import math
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .models import Overhead, Rect
from .perception import _Points

log = logging.getLogger("manager.overhead")

PROCESS_PERIOD_S = 0.2          # tag detection at 5 Hz is plenty for a rover doing 0.3 m/s
UPLOAD_MAX_PX = 1600            # long side of the frame sent to Gemini (it downsamples anyway)
PREVIEW_MAX_PX = 1280           # long side of the dashboard preview
STALE_FRAME_S = 2.0


class OverheadError(Exception):
    """The overhead camera cannot answer: no frame, not calibrated, or no LLM to point with."""


# ---------------------------------------------------------------- frame sources
class CvCapture:
    """A webcam (device index or /dev/videoN) or a stream URL. A reader thread keeps only the newest frame, so
    processing never works on a stale buffered one."""

    def __init__(self, spec: str, width: int = 3840, height: int = 2160):
        src = int(spec) if spec.isdigit() else spec
        self.cap = cv2.VideoCapture(src)
        if not self.cap.isOpened():
            raise OverheadError(f"cannot open overhead camera {spec!r}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))    # 4K over USB needs MJPG
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._frame, self._lock, self._halt = None, threading.Lock(), threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="overhead-capture")
        self._thread.start()

    def _run(self):
        while not self._halt.is_set():
            ok, frame = self.cap.read()
            if ok:
                with self._lock:
                    self._frame = frame
            else:
                time.sleep(0.05)

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame

    def close(self):
        """Stop the reader before releasing: releasing under a running cap.read() crashes OpenCV."""
        self._halt.set()
        self._thread.join(timeout=2.0)
        self.cap.release()


class StillImage:
    """A photo standing in for the camera (testing calibration and Gemini pointing without hardware)."""

    def __init__(self, path: str):
        self.frame = cv2.imread(path)
        if self.frame is None:
            raise OverheadError(f"cannot read image {path!r}")

    def read(self):
        return self.frame

    def close(self):
        pass


class HttpStill:
    """A webcam on another laptop, served by camera/stream_webcam.py: fetches its newest JPEG a few times a second.
    Every fetch is a fresh request, so a Wi-Fi drop heals by itself; a frame older than STALE_FRAME_S is not handed
    out, so a dead link shows as "offline" instead of a frozen picture."""

    def __init__(self, url: str, period_s: float = 0.2):
        self.url = url if url.lower().endswith((".jpg", ".jpeg")) else url.rstrip("/") + "/snapshot.jpg"
        self.period_s = period_s
        self._frame, self._t, self._lock, self._halt = None, 0.0, threading.Lock(), threading.Event()
        threading.Thread(target=self._run, daemon=True, name="overhead-http").start()

    def _run(self):
        import urllib.request
        said = None
        while not self._halt.wait(self.period_s):
            try:
                with urllib.request.urlopen(self.url, timeout=3.0) as r:
                    frame = cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR)
            except (OSError, ValueError) as e:
                if str(e) != said:                     # say each new reason once
                    log.warning("overhead camera %s: %s", self.url, e)
                    said = str(e)
                continue
            if frame is not None:
                said = None
                with self._lock:
                    self._frame, self._t = frame, time.monotonic()

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame if time.monotonic() - self._t < STALE_FRAME_S else None

    def close(self):
        self._halt.set()


def open_source(spec: str, width: int = 3840, height: int = 2160):
    """A webcam index or /dev/videoN, an http:// camera/stream_webcam.py, an rtsp:// stream, or a still image."""
    if Path(spec).suffix.lower() in (".jpg", ".jpeg", ".png") and not spec.startswith(("http://", "https://")):
        return StillImage(spec)
    if spec.startswith(("http://", "https://")):
        return HttpStill(spec)
    return CvCapture(spec, width, height)


# ---------------------------------------------------------------- geometry
def _apply(H: np.ndarray, col: float, row: float) -> tuple:
    p = H @ np.array([col, row, 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def _rot(x: float, y: float, deg: float) -> tuple:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return c * x - s * y, s * x + c * y


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _shrink(frame: np.ndarray, max_px: int) -> tuple:
    """-> (smaller frame, scale) so that the long side is at most max_px."""
    h, w = frame.shape[:2]
    k = min(1.0, max_px / max(h, w))
    return (frame if k == 1.0 else cv2.resize(frame, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)), k


# ---------------------------------------------------------------- the camera
class OverheadCamera:
    def __init__(self, source, cfg: Overhead, bounds: Rect, llm=None, clock=time.monotonic):
        self.source, self.cfg, self.bounds, self.llm, self.clock = source, cfg, bounds, llm, clock
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.dictionary))
        self.aruco = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        self.floor = {int(k): (float(v[0]), float(v[1])) for k, v in cfg.floor_tags.items()}
        self.lock = threading.Lock()
        self.frame: Optional[np.ndarray] = None
        self.frame_t: Optional[float] = None
        self.H: Optional[np.ndarray] = None          # pixel -> floor (metres)
        self.H_t: Optional[float] = None
        self.reproj_err_m: Optional[float] = None
        self.floor_seen: list = []
        self.markers: tuple = ((), None)              # (corners, ids) of the last frame, for the preview
        self.rover: Optional[dict] = None             # {x, y, yaw, t}
        self.located: list = []                       # last locate() hits, for the preview
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="overhead")
            self._thread.start()
        return self

    def halt(self):
        self._halt.set()
        self.source.close()

    def _run(self):
        while not self._halt.wait(PROCESS_PERIOD_S):
            frame = self.source.read()
            if frame is None:
                continue
            try:
                self.process(frame)
            except Exception:                         # noqa: BLE001  the camera thread must never die
                log.exception("overhead frame processing failed")

    # ------------------------------------------------------------ per-frame work
    def process(self, frame: np.ndarray, t: Optional[float] = None):
        """Detect the tags in one BGR frame: refresh the homography (needs 4+ floor tags) and the rover pose."""
        t = self.clock() if t is None else t
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _ = self.aruco.detectMarkers(gray)
        tags = {} if ids is None else {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}
        seen = sorted(i for i in tags if i in self.floor)
        H, err = self.H, self.reproj_err_m
        if len(seen) >= 4:
            src = np.array([tags[i].mean(axis=0) for i in seen], dtype=np.float64)
            dst = np.array([self.floor[i] for i in seen], dtype=np.float64)
            fit, _ = cv2.findHomography(src, dst, 0)
            if fit is not None:
                H = fit
                err = float(np.mean([math.dist(_apply(H, *s), d) for s, d in zip(src, dst)]))
        with self.lock:
            self.frame, self.frame_t, self.floor_seen, self.markers = frame, t, seen, (corners, ids)
            if H is not self.H:
                self.H, self.H_t, self.reproj_err_m = H, t, err
        rid = self.cfg.rover_tag_id
        if H is not None and rid in tags:
            c = tags[rid]
            h = self.cfg.rover_tag_height_m
            cx, cy = self.pixel_to_floor(*c.mean(axis=0), height=h)
            fx, fy = self.pixel_to_floor(*((c[0] + c[1]) / 2), height=h)      # tag's top edge = rover's front
            yaw = _wrap(math.degrees(math.atan2(fy - cy, fx - cx)) + self.cfg.rover_tag_yaw_offset_deg)
            ox, oy = _rot(*self.cfg.rover_tag_offset_m, yaw)
            with self.lock:
                self.rover = {"x": cx - ox, "y": cy - oy, "yaw": yaw, "t": t}

    def pixel_to_floor(self, col: float, row: float, height: float = 0.0) -> tuple:
        """Image pixel (full resolution) -> floor point in metres. `height` is how far the thing seen sits above the
        floor; it is pulled towards the camera's nadir to cancel parallax. Raises OverheadError if not calibrated."""
        H, frame = self.H, self.frame
        if H is None:
            raise OverheadError(self._uncalibrated())
        x, y = _apply(H, col, row)
        if height > 0 and (self.cfg.camera_floor_xy or frame is not None):
            nx, ny = self.cfg.camera_floor_xy or _apply(H, frame.shape[1] / 2, frame.shape[0] / 2)
            k = max(0.0, 1.0 - height / self.cfg.camera_height_m)
            x, y = nx + (x - nx) * k, ny + (y - ny) * k
        return x, y

    def _uncalibrated(self) -> str:
        return (f"overhead camera not calibrated: {len(self.floor_seen)} of {len(self.floor)} floor tags in view, "
                "need 4")

    # ------------------------------------------------------------ what the rest of the manager reads
    def rover_pose(self) -> Optional[dict]:
        """Latest rover sighting {x, y, yaw, t, age_s} (t on this camera's clock), or None if never seen."""
        with self.lock:
            r = self.rover
        return None if r is None else {**r, "age_s": self.clock() - r["t"]}

    def status(self) -> dict:
        now = self.clock()
        with self.lock:
            fresh = self.frame_t is not None and now - self.frame_t < STALE_FRAME_S
            shape = None if self.frame is None else self.frame.shape
            r = self.rover
            return {
                "online": fresh,
                "resolution": None if shape is None else f"{shape[1]}x{shape[0]}",
                "calibrated": self.H is not None,
                "floor_tags_seen": len(self.floor_seen),
                "floor_tags_total": len(self.floor),
                "reproj_err_cm": None if self.reproj_err_m is None else round(self.reproj_err_m * 100, 1),
                "rover_seen": r is not None and now - r["t"] <= self.cfg.max_age_s,
                "pose": None if r is None else {"x": round(r["x"], 3), "y": round(r["y"], 3),
                                                "yaw": round(r["yaw"], 1)},
            }

    def locate(self, target: str) -> list:
        """Ask Gemini where `target` is in the latest frame. -> [{x, y, label, col, row}] inside the arena, in the
        order Gemini gave them. Raises OverheadError (no frame / not calibrated / no LLM) or the LLM's error."""
        if self.llm is None:
            raise OverheadError("pointing with the overhead camera needs GEMINI_API_KEY")
        frame = self.source.read()                    # the newest frame, processed now so image and homography match
        if frame is None:
            raise OverheadError("no frame from the overhead camera yet")
        self.process(frame)
        if self.H is None:
            raise OverheadError(self._uncalibrated())
        small, _ = _shrink(frame, UPLOAD_MAX_PX)
        prompt = (f"This is a top-down photo of a robot test field, taken by a camera on the ceiling. Point to every "
                  f"{target} on the floor. Ignore the square black-and-white printed markers and the wheeled ground "
                  "robot. Return JSON {\"points\": [{\"point\": [y, x], \"label\": <name>}]} with coordinates "
                  "normalised to 0-1000, best match first. Point at the centre of each object. "
                  "If there is none, return {\"points\": []}.")
        r = self.llm.generate(prompt, _Points, images=[encode_jpeg(small)], tag="overhead_point")
        h, w = frame.shape[:2]
        hits = []
        for p in r.points:
            if len(p.point) != 2:
                continue
            col, row = p.point[1] / 1000 * w, p.point[0] / 1000 * h
            x, y = self.pixel_to_floor(col, row)
            if self.bounds.contains(x, y):
                hits.append({"x": round(x, 3), "y": round(y, 3), "label": p.label or target, "col": col, "row": row})
        with self.lock:
            self.located = hits
        return hits

    def preview_jpeg(self, max_px: int = PREVIEW_MAX_PX) -> Optional[bytes]:
        """The latest frame with the tags, the arena outline (from the homography), the rover and the last located
        objects drawn on it. A crooked outline means the floor tag positions in arena.json are wrong."""
        with self.lock:
            frame, (corners, ids), H, rover, located = self.frame, self.markers, self.H, self.rover, self.located
        if frame is None:
            return None
        img = frame.copy()
        if ids is not None and len(ids):
            cv2.aruco.drawDetectedMarkers(img, corners, ids)
        lw = max(2, img.shape[1] // 600)
        if H is not None:
            inv = np.linalg.inv(H)
            b = self.bounds.normalized()
            outline = [_apply(inv, x, y) for x, y in ((b.x0, b.y0), (b.x1, b.y0), (b.x1, b.y1), (b.x0, b.y1))]
            cv2.polylines(img, [np.int32(outline)], True, (0, 200, 255), lw)
            if rover is not None:
                # drawn on the floor under the rover, so it sits off the tag by the parallax correction
                tip = _rot(0.3, 0.0, rover["yaw"])
                a = _apply(inv, rover["x"], rover["y"])
                z = _apply(inv, rover["x"] + tip[0], rover["y"] + tip[1])
                cv2.arrowedLine(img, np.int32(a), np.int32(z), (255, 120, 0), lw * 2, tipLength=0.25)
        for hit in located:
            c = (int(hit["col"]), int(hit["row"]))
            cv2.circle(img, c, lw * 12, (0, 0, 255), lw)
            cv2.putText(img, f'{hit["label"]} ({hit["x"]:.2f}, {hit["y"]:.2f})', (c[0] + lw * 14, c[1]),
                        cv2.FONT_HERSHEY_SIMPLEX, lw * 0.6, (0, 0, 255), lw)
        small, _ = _shrink(img, max_px)
        return encode_jpeg(small)


def encode_jpeg(img: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise OverheadError("JPEG encoding failed")
    return buf.tobytes()


# ---------------------------------------------------------------- the scout's detector, backed by the camera
class OverheadDetector:
    """Drop-in for SimDetector when the overhead camera is installed: the scout still flies its search pattern, and
    it "sees" whatever the overhead camera located inside its footprint. One Gemini call per search (cached for
    `refresh_s`), not one per waypoint."""

    def __init__(self, cam: OverheadCamera, target: str, fov_deg: float = 60.0, refresh_s: float = 10.0,
                 clock=time.monotonic):
        self.cam, self.target, self.fov_deg, self.refresh_s, self.clock = cam, target, fov_deg, refresh_s, clock
        self._hits: list = []
        self._t = -1e9

    def footprint_radius(self, z: float) -> float:
        return max(z, 0.0) * math.tan(math.radians(self.fov_deg / 2))

    def set_targets(self, targets: list):
        """Scenario targets describe the simulated world; the camera sees the real one, so they are ignored."""

    def truth(self) -> list:
        return [{"id": f"cam{i + 1}", "x": h["x"], "y": h["y"]} for i, h in enumerate(self._located())]

    def _located(self) -> list:
        if self.clock() - self._t >= self.refresh_s:
            try:
                self._hits = self.cam.locate(self.target)
            except Exception as e:                    # noqa: BLE001  an unanswered search finds nothing, it never crashes
                log.warning("overhead locate failed: %s", e)
                self._hits = []
            self._t = self.clock()
        return self._hits

    def observe(self, drone_pose: dict) -> list:
        r = self.footprint_radius(drone_pose.get("z") or 0.0)
        return [{"x": t["x"], "y": t["y"], "target_id": t["id"], "error_m": None} for t in self.truth()
                if math.hypot(t["x"] - drone_pose["x"], t["y"] - drone_pose["y"]) <= r]


# ---------------------------------------------------------------- floor tools
def write_tags(out_dir: str, cfg: Overhead, px: int = 800) -> list:
    """One PNG per tag (floor tags + the rover tag) with a white quiet zone and its id printed underneath."""
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg.dictionary))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for tid, name in [*((int(k), "floor") for k in cfg.floor_tags), (cfg.rover_tag_id, "rover")]:
        tag = cv2.aruco.generateImageMarker(dictionary, tid, px)
        page = cv2.copyMakeBorder(tag, px // 8, px // 4, px // 8, px // 8, cv2.BORDER_CONSTANT, value=255)
        label = f"{name} tag {tid} ({cfg.dictionary})" + ("  ^ rover front ^" if name == "rover" else "")
        cv2.putText(page, label, (px // 8, px + px // 8 + px // 10), cv2.FONT_HERSHEY_SIMPLEX, px / 900, 0, 2)
        p = out / f"{name}_tag_{tid}.png"
        cv2.imwrite(str(p), page)
        paths.append(p)
    return paths


def _check(spec: str, save: Optional[str]):
    from .models import load_arena
    arena = load_arena()
    cam = OverheadCamera(open_source(spec), arena.overhead, arena.bounds)
    try:
        for _ in range(50):
            frame = cam.source.read()
            if frame is not None:
                break
            time.sleep(0.1)
        else:
            raise SystemExit("no frame from the camera after 5 s")
        cam.process(frame)
        print(cam.status())
        missing = sorted(set(cam.floor) - set(cam.floor_seen))
        if missing:
            print(f"floor tags NOT seen: {missing}")
        if save:
            Path(save).write_bytes(cam.preview_jpeg(max_px=3840))
            print(f"annotated frame written to {save}")
    finally:
        cam.halt()


if __name__ == "__main__":
    import argparse

    from .models import load_arena

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tags", help="write printable tag PNGs")
    t.add_argument("dir", nargs="?", default="overhead_tags")
    c = sub.add_parser("check", help="open the camera once and report calibration and the rover pose")
    c.add_argument("cam", nargs="?", default="0", help="device index, /dev/videoN, stream URL or a .jpg")
    c.add_argument("--save", help="write the annotated frame to this file")
    a = ap.parse_args()
    if a.cmd == "tags":
        for p in write_tags(a.dir, load_arena().overhead):
            print(p)
    else:
        _check(a.cam, a.save)
