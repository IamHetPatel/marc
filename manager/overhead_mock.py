"""OVERHEAD_CAM=mock: a synthetic top-down view of the mock world, so the overhead pipeline runs without hardware.

The frames are real images with real ArUco tags (floor tags at their arena.json positions, the rover tag on the mock
rover, drawn with the parallax a raised tag really has), so OverheadCamera calibrates and tracks exactly as it would
on the floor. Only Gemini is replaced: MockPointer answers "point to the X" from the mock world's objects.
"""
import math
import re

import cv2
import numpy as np

from .models import Arena
from .perception import GENERIC_WORDS, _PointItem, _Points

TAG_M = 0.16                    # printed tag size (black square), metres
OBJECT_R_M = 0.07
ROVER_SIZE_M = (0.32, 0.24)     # RoboMaster EP footprint, roughly


class SyntheticView:
    """A camera straight above the arena centre: floor point (metres) <-> pixel. Image rows grow downwards, arena y
    upwards, so seen from above the picture is not mirrored."""

    def __init__(self, arena: Arena, width: int = 1280, height: int = 720, margin_m: float = 0.25):
        b = arena.bounds.normalized()
        self.width, self.height = width, height
        self.k = min(width / (b.x1 - b.x0 + 2 * margin_m), height / (b.y1 - b.y0 + 2 * margin_m))   # px per metre
        self.cx, self.cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
        self.camera_height_m = arena.overhead.camera_height_m

    def to_px(self, x: float, y: float, height: float = 0.0) -> tuple:
        """Where a point `height` above the floor at (x, y) shows up: further from the nadir than its footprint."""
        s = 1.0 / max(1e-6, 1.0 - height / self.camera_height_m)
        x, y = self.cx + (x - self.cx) * s, self.cy + (y - self.cy) * s
        return self.width / 2 + (x - self.cx) * self.k, self.height / 2 - (y - self.cy) * self.k


def _bgr(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else (136, 136, 136)
    return b, g, r


class MockOverheadSource:
    def __init__(self, world, view: SyntheticView, arena: Arena):
        self.world, self.view, self.cfg = world, view, arena.overhead
        self.bounds = arena.bounds.normalized()
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.cfg.dictionary))
        self.tags = {tid: cv2.aruco.generateImageMarker(dictionary, tid, 120)
                     for tid in [*(int(k) for k in self.cfg.floor_tags), self.cfg.rover_tag_id]}

    def _square(self, cx: float, cy: float, yaw: float, size: float, height: float = 0.0) -> np.ndarray:
        """Pixel corners TL, TR, BR, BL of a square whose 'up' points along yaw (degrees)."""
        f = (math.cos(math.radians(yaw)), math.sin(math.radians(yaw)))
        r = (f[1], -f[0])                                          # right of 'up', seen from above
        h = size / 2
        pts = [(cx + (f[0] - r[0]) * h, cy + (f[1] - r[1]) * h), (cx + (f[0] + r[0]) * h, cy + (f[1] + r[1]) * h),
               (cx + (-f[0] + r[0]) * h, cy + (-f[1] + r[1]) * h), (cx + (-f[0] - r[0]) * h, cy + (-f[1] - r[1]) * h)]
        return np.float32([self.view.to_px(x, y, height) for x, y in pts])

    def _paste_tag(self, img, tid: int, corners: np.ndarray, quiet: np.ndarray):
        cv2.fillConvexPoly(img, np.int32(quiet), (255, 255, 255))
        tag = self.tags[tid]
        n = tag.shape[0]
        M = cv2.getPerspectiveTransform(np.float32([[0, 0], [n, 0], [n, n], [0, n]]), corners)
        warped = cv2.warpPerspective(tag, M, (img.shape[1], img.shape[0]), flags=cv2.INTER_NEAREST,
                                     borderValue=255)
        mask = np.zeros(img.shape[:2], np.uint8)
        cv2.fillConvexPoly(mask, np.int32(corners), 255)
        img[mask > 0] = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)[mask > 0]

    def read(self):
        v = self.view
        img = np.full((v.height, v.width, 3), 196, np.uint8)
        b = self.bounds
        cv2.rectangle(img, np.int32(v.to_px(b.x0, b.y1)), np.int32(v.to_px(b.x1, b.y0)), (176, 176, 176), 2)
        for tid, (x, y) in ((int(k), p) for k, p in self.cfg.floor_tags.items()):
            self._paste_tag(img, tid, self._square(x, y, 90.0, TAG_M), self._square(x, y, 90.0, TAG_M * 1.5))
        with self.world.lock:
            objects = [dict(o) for o in self.world.objects]
            rx, ry, ryaw = self.world.rover
        for o in objects:
            cv2.circle(img, np.int32(v.to_px(o["x"], o["y"], o.get("height", 0.0) / 2)), round(OBJECT_R_M * v.k),
                       _bgr(o.get("color", "#888888")), -1)
        h = self.cfg.rover_tag_height_m
        body = self._body(rx, ry, ryaw, h)
        cv2.fillConvexPoly(img, np.int32(body), (70, 70, 70))
        ox, oy = self.cfg.rover_tag_offset_m
        c, s = math.cos(math.radians(ryaw)), math.sin(math.radians(ryaw))
        tx, ty = rx + c * ox - s * oy, ry + s * ox + c * oy
        tag_yaw = ryaw - self.cfg.rover_tag_yaw_offset_deg
        self._paste_tag(img, self.cfg.rover_tag_id, self._square(tx, ty, tag_yaw, TAG_M, h),
                        self._square(tx, ty, tag_yaw, TAG_M * 1.3, h))
        return img

    def _body(self, x, y, yaw, height):
        L, W = ROVER_SIZE_M
        c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        return [self.view.to_px(x + c * dx - s * dy, y + s * dx + c * dy, height)
                for dx, dy in ((L / 2, W / 2), (L / 2, -W / 2), (-L / 2, -W / 2), (-L / 2, W / 2))]

    def close(self):
        pass


class MockPointer:
    """Stands in for Gemini pointing in OverheadCamera.locate: the mock objects whose label matches the target."""

    def __init__(self, world, view: SyntheticView):
        self.world, self.view = world, view

    def generate(self, prompt, schema, images=(), tag="llm", temperature=0.2):
        m = re.search(r"Point to every (.+?) on the floor", prompt)
        words = [w for w in (m.group(1) if m else "").lower().split() if w not in ("the", "a", "an")]
        generic = any(w in GENERIC_WORDS for w in words)
        with self.world.lock:
            objects = [dict(o) for o in self.world.objects]
        points = []
        for o in objects:
            if generic or any(w in o["label"].lower() for w in words):
                col, row = self.view.to_px(o["x"], o["y"], o.get("height", 0.0) / 2)   # where it is drawn
                points.append(_PointItem(point=[round(row / self.view.height * 1000),
                                                round(col / self.view.width * 1000)], label=o["label"]))
        return _Points(points=points)
