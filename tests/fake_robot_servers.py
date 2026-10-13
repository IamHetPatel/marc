"""Fake rover (:8000) and drone (:8001) HTTP servers backed by the mock world.

They speak the protocol the rover and drone servers implement (docs/ai-manager-plan.md §10), so the real
RoverHttp / DroneHttp clients can be tested over actual sockets. Like rover/robot_server.py, the fake rover takes
y = LEFT on /speed; speed_right_positive=True mimics a server that passes raw SDK signs (+y = right) through.
"""
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Response


def rover_app(rover, *, speed_right_positive=False, hang=None):
    app = FastAPI()

    @app.get("/pose")
    def pose():
        return rover.pose()

    @app.get("/speed")
    def speed(x: float, y: float, z: float, t: float):
        if hang and hang.is_set():
            time.sleep(5)
        return rover.speed(x, -y if speed_right_positive else y, z, t)

    @app.get("/drive")
    def drive(x: float, y: float, z: float):
        return rover.drive(x, y, z)                                 # /drive: y = left (brain.py)

    @app.get("/grip")
    def grip(action: str):
        return rover.grip(action)

    @app.get("/arm")
    def arm(preset: str):
        return rover.arm(preset)

    @app.get("/snapshot")
    def snapshot():
        return Response(rover.snapshot(), media_type="image/jpeg")

    @app.get("/stop")
    def stop():
        return rover.stop()

    return app


def drone_app(drone):
    app = FastAPI()

    @app.get("/pose")
    def pose(cf: str = "scout"):
        return drone.pose(cf)

    @app.get("/takeoff")
    def takeoff(z: float = 0.6):
        return drone.takeoff(z)

    @app.get("/goto")
    def goto(x: float, y: float, z: float):
        return drone.goto(x, y, z)

    @app.get("/scan")
    def scan(x0: float, y0: float, x1: float, y1: float):
        return drone.scan(x0, y0, x1, y1)

    @app.get("/land")
    def land():
        return drone.land()

    @app.get("/dock")
    def dock():
        return drone.dock()

    return app


class Served:
    """Run a FastAPI app on a free local port in a background thread."""

    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        t0 = time.time()
        while not self.server.started and time.time() - t0 < 5:
            time.sleep(0.01)
        assert self.server.started, "fake robot server did not start"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=3)
