"""Stream a USB webcam to the mothership over the hotspot. Run it on the laptop the webcam is plugged into.

    pip install opencv-python
    python camera/stream_webcam.py                          # webcam 0, 1920x1080, port 8003
    python camera/stream_webcam.py --cam 1 --width 3840 --height 2160

It prints the line to use on the mothership PC, e.g.
    python start.py floor --camera http://192.168.1.23:8003

    GET /snapshot.jpg   the newest frame (what the mothership fetches, about 5 times a second)
    GET /video          live MJPEG, for a browser
    GET /               a page that shows the video
    GET /health         resolution, frame age

1080p is plenty for a 5 x 3 m cage (about 3 mm per pixel) and is much lighter on Wi-Fi than 4K. If the webcam is
unplugged or stops delivering frames, it is reopened by itself.
"""
import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Cage camera</title>
<body style="margin:0;background:#111;display:grid;place-items:center;height:100vh">
<img src="/video" style="max-width:100vw;max-height:100vh"></body>"""


class Webcam:
    """Keeps the newest frame from the webcam; JPEG-encodes it once per frame, only when someone asks."""

    def __init__(self, index: int, width: int, height: int):
        self.index, self.width, self.height = index, width, height
        self.status = "opening"
        self._lock = threading.Lock()
        self._frame, self._t, self._seq, self._jpeg = None, 0.0, 0, None
        threading.Thread(target=self._run, daemon=True).start()

    def _open(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY       # DirectShow opens fast on Windows
        cap = cv2.VideoCapture(self.index, backend)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))    # high resolutions over USB need MJPG
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self):
        while True:
            cap = self._open()
            if not cap.isOpened():
                self.status = f"cannot open webcam {self.index}; retrying"
                time.sleep(2)
                continue
            misses = 0
            while misses < 30:
                ok, frame = cap.read()
                if not ok:
                    misses += 1
                    time.sleep(0.05)
                    continue
                misses = 0
                with self._lock:
                    self._frame, self._t, self._jpeg = frame, time.monotonic(), None
                    self._seq += 1
                self.status = f"live {frame.shape[1]}x{frame.shape[0]}"
            self.status = "webcam stopped delivering frames; reopening"
            cap.release()
            time.sleep(1)

    def jpeg(self):
        """(seq, JPEG bytes) of the newest frame, or (seq, None) if there is no recent one."""
        with self._lock:
            if self._frame is None or time.monotonic() - self._t > 2.0:
                return self._seq, None
            if self._jpeg is None:
                ok, buf = cv2.imencode(".jpg", self._frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                self._jpeg = buf.tobytes() if ok else None
            return self._seq, self._jpeg

    def age_s(self):
        return None if self._frame is None else round(time.monotonic() - self._t, 2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        cam = self.server.cam
        try:
            path = self.path.split("?")[0]
            if path == "/snapshot.jpg":
                _, jpeg = cam.jpeg()
                if jpeg is None:
                    return self._send(503, json.dumps({"ok": False, "error": cam.status}).encode(), "application/json")
                return self._send(200, jpeg, "image/jpeg")
            if path == "/video":
                return self._mjpeg(cam)
            if path == "/health":
                body = {"ok": cam.age_s() is not None and cam.age_s() < 2.0, "status": cam.status, "age_s": cam.age_s()}
                return self._send(200, json.dumps(body).encode(), "application/json")
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass                                        # the viewer went away

    def _send(self, code, data, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _mjpeg(self, cam):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        last = None
        while True:
            seq, jpeg = cam.jpeg()
            if jpeg is None or seq == last:
                time.sleep(0.03)
                continue
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")
            last = seq


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main():
    ap = argparse.ArgumentParser(description="Stream a webcam to the mothership over the hotspot.")
    ap.add_argument("--cam", type=int, default=0, help="webcam index (0 = the first one)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--port", type=int, default=8003)
    a = ap.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    server.daemon_threads = True
    server.cam = Webcam(a.cam, a.width, a.height)
    url = f"http://{lan_ip()}:{a.port}"
    print(f"Webcam {a.cam} streaming on {url}  (open it in a browser to look)\n"
          f"On the mothership PC:  python start.py floor --camera {url}\nCtrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
