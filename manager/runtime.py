"""Wiring: config -> adapters -> Ctx. ROVER and DRONE are chosen independently, so mixed mode works
(real rover + mock drone = Level 3)."""
from typing import Optional

from .adapters.base import RealClock
from .adapters.drone_http import DroneHttp
from .adapters.mock import build_mock
from .adapters.rover_http import RoverHttp
from .config import Config
from .llm import LLM, make_llm
from .localization import Localization
from .models import load_arena
from .perception import make_perception
from .safety import Safety
from .state import Store
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

    # a mock drone's "docked" pose says nothing about where a REAL rover is
    trust_drone_fix = mock_rover or not mock_drone
    loc = Localization(rover, drone, arena, clock, use_drone_fix=trust_drone_fix)
    perception = make_perception(cfg, arena, llm, world if mock_rover else None)
    safety = Safety(cfg, arena, store, rover, drone)
    ctx = Ctx(cfg=cfg, arena=arena, store=store, rover=rover, drone=drone, loc=loc, perception=perception,
              safety=safety, clock=clock, faults=faults)
    return ctx, world
