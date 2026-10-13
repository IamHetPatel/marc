# Reference

## Mission manager API (`:8002`)

JSON in and out. The dashboard uses exactly these calls; so can a script or `curl`.

| Method | Path | Body | What it does |
| --- | --- | --- | --- |
| GET | `/state` | | Everything live: mission, plan, robots, findings, messages, metrics |
| POST | `/mission` | `{text}` | Plan a mission from a sentence (Gemini, or the built-in plan) |
| POST | `/plan` | `{title, steps}` | Submit your own list of steps |
| GET | `/scenarios` | | Ready-made missions for the current field |
| POST | `/scenarios/{name}/load` | | Load one (its plan still waits for approval) |
| POST | `/approve` | `{plan_version}` | Start the plan you have seen |
| POST | `/change` | `{text}` | A new instruction mid-mission, or the answer to a question |
| POST | `/manual` | `{robot, action, args}` | Run one robot action by hand, through the same safety checks |
| POST | `/stop` | | Rover stops and drone lands, in parallel, without the AI |
| POST | `/mentor` | `{present}` | The safety-pilot switch: unlocks or locks the drone |
| POST | `/reset` | | Clear the finished mission |
| GET | `/report`, `/report.html` | | The mission report as data, or as a page that prints to PDF |
| GET | `/arena` | | The field: bounds, flight cage, zones, launch pad, rover size |
| GET | `/images/{id}.jpg` | | Photos taken during the mission |
| GET | `/rover/video` | | The rover's live camera (MJPEG) |
| GET | `/overhead.jpg` | | The overhead camera's latest annotated frame |
| POST | `/rover/home`, `/rover/anchor` | `{cf}` | Set the rover's position by hand when no Crazyflie rides on it |

Interactive docs while the server runs: http://localhost:8002/docs

## Robot actions

A plan is a list of these. The planner sees each action's description, which comes straight from the code
([manager/tools.py](../manager/tools.py)). Places are written as coordinates or references that are resolved when
the step runs: `$finding.f1`, `$zone.field_a`, `$zone.disposal`, `$home`, `$start`.

| Robot | Action | What it does |
| --- | --- | --- |
| drone | `drone_search` | Fly lanes over an area and report what the scout detects (checkpoint) |
| drone | `drone_mark` | Hover over a spot so it shows on the map |
| drone | `drone_home` | Fly back to the launch pad and land |
| drone | `drone_takeoff`, `drone_goto`, `drone_land` | Basic flight |
| drone | `drone_scan`, `drone_dock` | Height-sensor sweep; landing on the rover (docking setup only) |
| rover | `rover_goto` | Drive to a place; for a finding, stop short of it (`standoff` or `gap`) and face it |
| rover | `rover_drive` | Move relative to where it is |
| rover | `rover_search` | Turn until the camera sees the object |
| rover | `rover_approach` | Close the last distance until the object is in gripper reach |
| rover | `rover_inspect` | Photograph it and decide: ours, hostile, or not a drone (checkpoint) |
| rover | `rover_pick_up` | Inspect, lower the arm, approach, grip, lift and check, in one step |
| rover | `rover_grip`, `rover_arm`, `rover_check_grip`, `rover_snapshot` | Gripper, arm presets, grip check, a photo |
| rover | `rover_calibrate_rider` | Measure how the riding Crazyflie is mounted (manual control only) |
| manager | `request_authorization` | Ask the operator before anything hazardous is touched |
| manager | `ask_human` | Ask the operator any question (checkpoint) |
| manager | `camera_locate` | Find an object with the overhead camera (checkpoint) |
| manager | `report` | Tell the operator what happened |

## Rover server (`:8000`)

[rover/robot_server.py](../rover/robot_server.py). Every endpoint is a GET and returns JSON `{ok, ...}`.
+y is left and +z is counter-clockwise everywhere; the server converts to the SDK's signs.

| Path | Parameters | What it does |
| --- | --- | --- |
| `/health` | | Link status, battery, camera, round-trip times |
| `/pose` | `reset=1` | Wheel odometry since start: x, y (m), yaw (deg) |
| `/speed` | `x`, `y`, `z`, `t` | Velocity (m/s, m/s, deg/s) for `t` seconds, then stop |
| `/drive` | `x`, `y`, `z` | Blocking relative move (m, m, deg) |
| `/grip` | `action=open\|close` | Gripper |
| `/arm` | `preset=pick\|carry\|home`, or `x`, `y` (mm) | Arm |
| `/stop` | | Stop now, also interrupts `/drive` |
| `/snapshot`, `/video` | | One JPEG, or the MJPEG stream |

## Drone server (`:8001`)

[drone/drone_server.py](../drone/drone_server.py). A new flight command waits for the previous one; `/land`
interrupts anything.

| Path | Parameters | What it does |
| --- | --- | --- |
| `/health` | | Radio links, flight cage, Lighthouse deck and base-station status |
| `/pose` | `cf=scout\|target` | Position, yaw, battery and state; the scout also reports its launch pad |
| `/takeoff` | `z` | Take off (allowed wherever tracking is good) |
| `/goto` | `x`, `y`, `z` | Fly to a point inside the flight cage |
| `/scan` | `x0`, `y0`, `x1`, `y1` | Lane sweep with the downward range sensor |
| `/dock` | | Back to the launch pad, settle, land |
| `/land` | | Land where it is |

## Settings

Environment variables, all optional. `start.py` sets the common ones for you.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ROVER`, `DRONE` | `mock` | `mock`, or the robot server's URL |
| `ARENA` | `manager/arena.json` | The field file; `manager/arena.floor.json` is the measured floor |
| `GEMINI_API_KEY` | | Turns on AI planning, supervision and camera understanding |
| `AI` | `on` | `off` ignores the key (same as `start.py --no-ai`) |
| `MODEL` | `gemini-robotics-er-2-preview` | The Gemini model |
| `PERCEPTION` | `auto` | `gemini`, `map` (positions instead of images), `mock`, or `none` |
| `ROVER_RIDER` | | `target`: the Crazyflie riding on the rover is its position |
| `ROVER_RIDER_MODE` | `filtered` | `blend` mixes in wheel odometry |
| `OVERHEAD_CAM` | | A webcam index, stream URL or `mock` for the overhead camera |
| `SCENARIO` | | Load this scenario when the server starts |
| `MOCK_TIME_SCALE` | `1` | Run the simulator faster than real time |
| `MOCK_FAIL` | | Inject failures, e.g. `goto:0.2,grip:0.3` |
| `CHANGE_NEEDS_APPROVAL` | `0` | A re-planned mission waits for approval again |
| `GEOFENCE` | `1` | Drone server: `0` turns the flight-cage limits off (the tracking check stays) |
| `SCOUT_URI`, `TARGET_URI` | | Drone server: radio addresses of the scout and the riding Crazyflie |
| `ROBOT_HOST` | `auto` | Rover server: the EP's address, or `auto` to find it |

## Tests

```bash
.venv/bin/python -m pytest              # simulated robots and fake robot servers
.venv/bin/python -m pytest -m ui        # the dashboard in headless Chromium (pip install -r tests/requirements-ui.txt)
.venv/bin/python -m pytest -m live      # against the real Gemini API (needs a key, uses quota)
```
