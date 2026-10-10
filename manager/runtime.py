"""Wiring: config -> adapters -> Ctx. ROVER and DRONE are chosen independently, so mixed mode works
(real rover + mock drone = Level 3)."""
from typing import Optional

from .adapters.base import RealClock
from .adapters.drone_http import DroneHttp
from .adapters.mock import build_mock
from .adapters.rover_http import RoverHttp
from .config import Config
from .llm import LLM, make_llm
from .controller import Controller
from .localization import Localization
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

    # no docking -> never a drone fix. With docking, a mock drone's "docked" pose says nothing about a REAL rover
    trust_drone_fix = cfg.dock_mode and (mock_rover or not mock_drone)
    loc = Localization(rover, drone, arena, clock, use_drone_fix=trust_drone_fix)
    perception = make_perception(cfg, arena, llm, world if mock_rover else None)
    safety = Safety(cfg, arena, store, rover, drone)
    ctx = Ctx(cfg=cfg, arena=arena, store=store, rover=rover, drone=drone, loc=loc, perception=perception,
              safety=safety, clock=clock, faults=faults)
    return ctx, world


def build_controller(cfg: Optional[Config] = None, llm: Optional[LLM] = None, auto_approve: bool = False,
                     use_env_llm: bool = True) -> Controller:
    """Everything wired and ready; call .start() to run the telemetry monitor.
    `llm=None` with `use_env_llm=True` uses Gemini when a key is set, else the canned plan + rule supervisor."""
    cfg = cfg or Config.from_env()
    store = Store()
    if llm is None and use_env_llm:
        llm = make_llm(cfg, on_latency=lambda ms: store.set_metric("llm_latency_ms_last", ms))
    ctx, world = build_ctx(cfg, store, llm)
    ctrl = Controller(ctx, Planner(llm, ctx.arena, cfg.dock_mode),
                      LLMSupervisor(llm, ctx.arena, dock_mode=cfg.dock_mode), auto_approve=auto_approve)
    ctrl.world = world                      # the MockWorld (or None); tests and demos poke it
    return ctrl
