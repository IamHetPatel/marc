# How MARC was built

MARC was built in about 30 hours at a mobile robotics hackathon in Dresden, on Saturday 10 and Sunday 11 October
2026, by a team of seven. This folder keeps the plans we wrote while building it, unedited. They describe the state
of the project at the time they were written, so file names and details in them may have changed since.

## Timeline

**Saturday morning: one sentence, many robots.** The starting idea was general: type one sentence, and an AI splits
the job between different robots while a person approves the plan. The first commit is a single-file prototype
([prototype/](../../prototype/)) that flies Crazyflie scouts and drives a RoboMaster EP from a language model.

**Saturday midday: the mission manager.** The prototype became a proper service: a planner that writes a list of
robot steps, a person who approves it, an executor that runs it, a supervisor that re-plans, safety checks in code,
and a simulated world with about a hundred tests, so the whole loop worked before any robot was connected.

**Saturday afternoon: the use case.** With a RoboMaster EP and Crazyflies in hand, the problem became concrete:
find and recover crashed drones in a field, some of them possibly explosive. The plan gained triage (ours, hostile,
not a drone), an authorization gate before anything hazardous is gripped, a disposal zone and a mission report.
In parallel the drone server learned to fly the scout safely (preemptible flights, a measured geofence, Lighthouse
diagnostics), and the rover squad brought up the EP's SDK, a browser control and an HTTP server for it.

**Saturday evening: everything connected.** The rover and drone servers were wired into the mission manager and the
dashboard, one `start.py` started them all, and an overhead webcam was added as a backup "eye in the sky".

**Saturday night: one map for both robots.** The hardest part. The drone knows where it is in the Lighthouse frame;
the rover only counts wheel turns, and they disagreed by tens of centimetres. The fix was to put a second Crazyflie on
the rover with its motors off: its Lighthouse position *is* the rover's position. The same night the EP's link was
made robust against a busy Wi-Fi hotspot, and the field was measured at 5 × 3 m.

**Sunday: on the floor.** The flight cage was re-measured where Lighthouse reception is good, the zones were moved
inside it, the EP moved to its own direct Wi-Fi, and the Gemini free quota ran out, so the rover learned to line up
on the marked position instead of the image. The last changes before the 17:00 presentation were the pick-up flow
(park with the gripper just short of the object, grip, bring it back) and a filter that keeps Lighthouse glitches
from moving the rover on the map.

## What we learned

- **Share one frame first.** Every other feature depends on the drone and the rover agreeing where things are.
- **Tracking systems glitch.** A Lighthouse dropout reports positions metres away. Reject impossible jumps before
  anything acts on them.
- **Radios share the air.** The EP's video stream on a crowded hotspot dropped commands; its own Wi-Fi was stable.
- **Keep the AI optional.** A free API quota is gone after one or two runs. Built-in plans and position-based
  alignment kept the demo working without it.
- **Put safety in code.** The authorization gate, the geofence and STOP hold whatever plan the AI writes.

## The plans

| File | Written | What it was for |
| --- | --- | --- |
| [ai-manager-plan.md](ai-manager-plan.md) | Sat 12:56 | Design of the mission manager: plan, approve, execute, safety in code |
| [mothership-plan.md](mothership-plan.md) | Sat 15:02 | The drone-recovery use case, split into work packages with contracts |
| [integration-plan.md](integration-plan.md) | Sat 20:32 | Bringing the rover and drone servers together on the floor |
| [ai-layer-plan.md](ai-layer-plan.md) | Sun 00:35 | Model choice, quota handling and AI status on the dashboard |
| [convergence-plan.md](convergence-plan.md) | Sun 10:24 | One Lighthouse map for every sensor, and the floor test ladder |
