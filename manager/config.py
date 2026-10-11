"""Runtime configuration: every knob is an env var, read once into a frozen Config.

    ROVER=mock|http://<win-ip>:8000   DRONE=mock|http://<drone-ip>:8001   MODEL=gemini-robotics-er-2-preview
"""
import os
from dataclasses import dataclass, field


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _fail_spec(spec: str) -> dict:
    """'goto:0.2,grip:0.3' -> {'goto': 0.2, 'grip': 0.3}"""
    out = {}
    for item in filter(None, spec.split(",")):
        key, _, p = item.partition(":")
        out[key.strip()] = float(p)
    return out


@dataclass(frozen=True)
class Config:
    # --- which robots
    rover: str = "mock"
    drone: str = "mock"
    # --- LLM
    model: str = "gemini-robotics-er-2-preview"
    model_fallbacks: tuple = ("gemini-robotics-er-1.5-preview", "gemini-2.5-flash")
    plan_timeout_s: float = 15.0
    runs_dir: str = "runs"
    # --- mission rules (plan §4, §8)
    change_needs_approval: bool = False
    mission_timeout_s: float = 600.0
    replans_per_step: int = 2
    replans_per_mission: int = 6
    # --- safety caps (plan §8)
    rover_max_speed: float = 0.3         # m/s
    rover_max_move_m: float = 1.0        # per relative move
    drone_min_battery_v: float = 3.3
    watchdog_fails: int = 4              # consecutive failed polls (0.5 s apart) while moving
    # --- rover server conventions: rover/robot_server.py takes y = LEFT on /speed and /drive (it converts to
    # the SDK's +y = right itself); -1 only for a server that speaks raw SDK signs
    rover_speed_y_sign: float = 1.0
    rover_drive_y_sign: float = 1.0
    rover_arm_mode: str = "preset"       # "preset" -> /arm?preset=pick ; "relative" -> /arm?x&y deltas
    # --- farmland story (docs/mothership-plan.md §1.4, §1.5): no docking, a simulated downward-camera detector
    dock_mode: bool = False              # DOCK_MODE=1 brings back "drone rides the rover plate" (ai-manager-plan §7)
    search_z: float = 0.6                # scout height while searching (m)
    mark_z: float = 0.4                  # scout height while hovering over a marked spot (m); the EP is ~0.3 m tall
    detector_fov_deg: float = 60.0
    detector_noise_m: float = 0.05
    detector_miss_p: float = 0.05
    rover_rider: str = ""                # ROVER_RIDER=target|scout: that Crazyflie rides on the EP and IS its position
    separation_m: float = 0.5            # a low drone must stay this far (x/y) from where the rover is going
    scenario: str = ""                   # SCENARIO=<name>: load this scenario at server start
    # --- overhead webcam backup (manager/overhead.py): OVERHEAD_CAM=0 | /dev/video0 | rtsp://... | frame.jpg
    overhead_cam: str = ""               # empty = no overhead camera
    overhead_width: int = 3840
    overhead_height: int = 2160
    overhead_target: str = "crashed drone"   # what drone_search looks for when the camera is its detector
    detector: str = "auto"               # auto (overhead if camera + Gemini, else sim) | sim | overhead
    # --- mock world
    mock_time_scale: float = 1.0         # >1 runs the simulated clock faster than wall time
    mock_fail: dict = field(default_factory=dict)
    mock_seed: int = 7
    perception: str = "auto"             # auto | mock | gemini | map (positions, no AI) | none
    # --- server
    host: str = "0.0.0.0"
    port: int = 8002

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            rover=os.getenv("ROVER", "mock"),
            drone=os.getenv("DRONE", "mock"),
            model=os.getenv("MODEL", cls.model),
            plan_timeout_s=_float("PLAN_TIMEOUT_S", cls.plan_timeout_s),
            runs_dir=os.getenv("RUNS_DIR", cls.runs_dir),
            change_needs_approval=_flag("CHANGE_NEEDS_APPROVAL"),
            dock_mode=_flag("DOCK_MODE"),
            search_z=_float("SEARCH_Z", cls.search_z),
            mark_z=_float("MARK_Z", cls.mark_z),
            detector_fov_deg=_float("DETECTOR_FOV_DEG", cls.detector_fov_deg),
            detector_noise_m=_float("DETECTOR_NOISE_M", cls.detector_noise_m),
            detector_miss_p=_float("DETECTOR_MISS_P", cls.detector_miss_p),
            separation_m=_float("SEPARATION_M", cls.separation_m),
            rover_rider=os.getenv("ROVER_RIDER", cls.rover_rider).strip().lower(),
            scenario=os.getenv("SCENARIO", cls.scenario),
            overhead_cam=os.getenv("OVERHEAD_CAM", cls.overhead_cam),
            overhead_width=int(os.getenv("OVERHEAD_WIDTH", cls.overhead_width)),
            overhead_height=int(os.getenv("OVERHEAD_HEIGHT", cls.overhead_height)),
            overhead_target=os.getenv("OVERHEAD_TARGET", cls.overhead_target),
            detector=os.getenv("DETECTOR", cls.detector),
            mission_timeout_s=_float("MISSION_TIMEOUT_S", cls.mission_timeout_s),
            rover_speed_y_sign=_float("ROVER_SPEED_Y_SIGN", cls.rover_speed_y_sign),
            rover_drive_y_sign=_float("ROVER_DRIVE_Y_SIGN", cls.rover_drive_y_sign),
            rover_arm_mode=os.getenv("ROVER_ARM_MODE", cls.rover_arm_mode),
            mock_time_scale=_float("MOCK_TIME_SCALE", cls.mock_time_scale),
            mock_fail=_fail_spec(os.getenv("MOCK_FAIL", "")),
            mock_seed=int(os.getenv("MOCK_SEED", cls.mock_seed)),
            perception=os.getenv("PERCEPTION", cls.perception),
            host=os.getenv("HOST", cls.host),
            port=int(os.getenv("PORT", cls.port)),
        )
