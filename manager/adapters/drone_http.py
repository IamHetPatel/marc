"""HTTP client for drone/drone_server.py (:8001). The server must be threaded so /land works mid-scan."""
from .rover_http import HttpClient


class DroneHttp:
    def __init__(self, base: str):
        self.http = HttpClient(base)

    @property
    def rtt_ms(self):
        return self.http.last_rtt_ms

    def health(self) -> dict:
        """{ok, mock, cage, scout, scout_lighthouse, target}: drone_server.py's link and Lighthouse status."""
        return self.http.json("/health", timeout=2.0)

    def pose(self, cf: str = "scout") -> dict:
        return self.http.json("/pose", timeout=2.0, cf=cf)

    def takeoff(self, z: float = 0.6) -> dict:
        return self.http.json("/takeoff", timeout=15.0, z=z)

    def goto(self, x, y, z) -> dict:
        return self.http.json("/goto", timeout=30.0, x=x, y=y, z=z)

    def scan(self, x0, y0, x1, y1) -> dict:
        return self.http.json("/scan", timeout=180.0, x0=x0, y0=y0, x1=x1, y1=y1)

    def land(self) -> dict:
        return self.http.json("/land", timeout=1.0)

    def dock(self) -> dict:
        return self.http.json("/dock", timeout=30.0)
