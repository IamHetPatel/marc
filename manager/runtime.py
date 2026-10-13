"""Wiring: config -> adapters -> Ctx. ROVER and DRONE are chosen independently, so mixed mode works
(real rover + mock drone = Level 3)."""
import logging
import random
from typing import Optional

from .adapters.base import RealClock
from .adapters.drone_http import DroneHttp
from .adapters.mock import build_mock
from .adapters.rover_http import RoverHttp
from .config import Config
from .llm import LLM, make_llm
from .controller import Controller
from .detector import SimDetector
from .llm import LLMError
from .localization import MOUNT_FILE, Localization, RiderLocalization
from .models import load_arena
from .perception import make_perception
from .planner import Planner
from .safety import Safety
from .state import Store
from .supervisor import LLMSupervisor
from .tools import Ctx


def build_ctx(cfg: Optional[Config] = None, store: Optional[Store] = None, llm: Optional[LLM] = None):
    """-> (ctx, world). `world` is the MockWorld when anything is mocked, else None."""
    cfg = cfg or Config.from_env()
    store = store or Store()
    arena = load_arena()
    mock_rover, mock_drone = cfg.rover == "mock", cfg.drone == "mock"

    world = faults = None
    clock = RealClock()
    if mock_rover or mock_drone:
        world, m_rover, m_drone, sim_clock, faults = build_mock(arena, cfg)
        if mock_rover:
            clock = sim_clock                    # the simulated clock drives every sleep/timeout
    rover = m_rover if mock_rover else RoverHttp(cfg.rover, cfg)
    drone = m_drone if mock_drone else DroneHttp(cfg.drone)

    overhead = build_overhead(cfg, arena, llm, world)
    # no docking -> never a drone fix. With docking, a mock drone's "docked" pose says nothing about a REAL rover.
    # Only a real overhead camera steers the rover, and only a real one (the mock view runs on wall-clock time,
    # the mock rover on the scaled simulated clock).
    trust_drone_fix = cfg.dock_mode and (mock_rover or not mock_drone)
    overhead_fix = overhead is not None and not mock_rover and cfg.overhead_cam != "mock"
    if cfg.rover_rider and cfg.rover_rider_mode != "blend":   # the riding Crazyflie is the ONLY position source
        loc = RiderLocalization(rover, drone, arena, clock, rider=cfg.rover_rider,
                                mount_file=None if mock_rover else MOUNT_FILE)
    else:
        loc = Localization(rover, drone, arena, clock, use_drone_fix=trust_drone_fix,
                           overhead=overhead if overhead_fix else None, rider=cfg.rover_rider)
    perception = make_perception(cfg, arena, llm, world if mock_rover else None)
    safety = Safety(cfg, arena, store, rover, drone)
    ctx = Ctx(cfg=cfg, arena=arena, store=store, rover=rover, drone=drone, loc=loc, perception=perception,
              safety=safety, clock=clock, faults=faults, detector=build_detector(cfg, drone, overhead),
              overhead=overhead)
    if hasattr(perception, "bind"):
        perception.bind(ctx)                    # map perception reads the rover pose and the targets
    if cfg.rover_rider == "target" and hasattr(ctx.detector, "live_ok"):
        ctx.detector.live_ok = False            # the target Crazyflie rides on the rover: it is not the wreck
    return ctx, world


def build_overhead(cfg: Config, arena, llm, world=None):
    """The overhead webcam, started, or None when OVERHEAD_CAM is unset. OVERHEAD_CAM=mock renders the mock world
    instead (needs a mock robot). OpenCV is imported only here."""
    if not cfg.overhead_cam:
        return None
    from .overhead import OverheadCamera, OverheadError, open_source
    if cfg.overhead_cam == "mock":
        if world is None:
            raise LLMError("OVERHEAD_CAM=mock needs ROVER=mock or DRONE=mock")
        from .overhead_mock import MockOverheadSource, MockPointer, SyntheticView
        view = SyntheticView(arena)
        source, llm = MockOverheadSource(world, view, arena), MockPointer(world, view)
    else:
        try:
            source = open_source(cfg.overhead_cam, cfg.overhead_width, cfg.overhead_height)
        except OverheadError as e:                  # a missing camera must not keep the mothership from starting
            logging.getLogger("manager.runtime").warning("overhead camera off: %s", e)
            return None
    return OverheadCamera(source, arena.overhead, arena.bounds, llm=llm).start()


def build_detector(cfg: Config, drone, overhead):
    """What drone_search sees with: the overhead camera (real) or the simulated downward camera."""
    mode = cfg.detector
    if mode == "auto":
        mode = "overhead" if overhead is not None and overhead.llm is not None else "sim"
    if mode == "overhead" and overhead is None:
        logging.getLogger("manager.runtime").warning("DETECTOR=overhead but no overhead camera: using the simulated one")
        mode = "sim"
    if mode == "overhead":
        from .overhead import OverheadDetector
        return OverheadDetector(overhead, cfg.overhead_target, cfg.detector_fov_deg)
    return SimDetector(drone, cfg.detector_fov_deg, cfg.detector_noise_m, cfg.detector_miss_p,
                       random.Random(cfg.mock_seed + 2))


def build_controller(cfg: Optional[Config] = None, llm: Optional[LLM] = None, auto_approve: bool = False,
                     use_env_llm: bool = True) -> Controller:
    """Everything wired and ready; call .start() to run the telemetry monitor.
    `llm=None` with `use_env_llm=True` uses Gemini when a key is set, else the canned plan + rule supervisor."""
    cfg = cfg or Config.from_env()
    store = Store()
    if llm is None and use_env_llm:
        llm = make_llm(cfg, on_latency=lambda ms: store.set_metric("llm_latency_ms_last", ms))
    ctx, world = build_ctx(cfg, store, llm)
    overhead = ctx.overhead is not None
    ctrl = Controller(ctx, Planner(llm, ctx.arena, cfg.dock_mode, overhead),
                      LLMSupervisor(llm, ctx.arena, dock_mode=cfg.dock_mode, overhead=overhead),
                      auto_approve=auto_approve)
    ctrl.world = world                      # the MockWorld (or None); tests and demos poke it
    return ctrl
