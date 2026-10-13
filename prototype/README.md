# Prototype

The first version of MARC, written on the first morning: single files where a language model calls robot functions
directly, with no planner, approval step or dashboard yet. The mission manager in [manager/](../manager/) replaced
them. They are kept because they show where the design started; `fleet.py` and `fleet_agent.py` still run
without hardware with `MOCK=1` (they need a Gemini key).

| File | What it does |
| --- | --- |
| [fleet.py](fleet.py) | Crazyflie scouts search in lanes and report height-sensor dips; the EP drives to a dip and its camera checks it. Introduced the riding-Crazyflie trick for a shared map |
| [brain.py](brain.py) | One robot in a look, decide, act, look loop with a vision model |
| [fleet_agent.py](fleet_agent.py) | The same idea for ROS 2 robots through rosbridge |
| [gesture_teleop.py](gesture_teleop.py) | Drive the rover with hand gestures in front of a webcam |

```bash
pip install google-genai requests
export GEMINI_API_KEY=...
MOCK=1 python prototype/fleet.py "Search the left half of the arena, then bring me whatever you find"
```
