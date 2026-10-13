# FleetPilot: orchestration guide

One sentence in, a fleet of different robots out. An LLM/VLM splits the job between a fast drone (finds things) and a slow ground robot (verifies and fetches), checks its own work with the robot camera, and a human stays in control.

This README is for everyone (humans and coding agents) working on the repo. It explains **how the pieces talk to each other**, **what exists today**, and **what each squad must build against**. For the hackathon schedule, jury criteria and squad deadlines, see [FleetPilot – Hackathon Team Plan.md](FleetPilot%20–%20Hackathon%20Team%20Plan.md).

> **Status legend** used below: ✅ in this repo · 🟡 planned, specified in the Team Plan, not in this repo yet · ⚠️ known gap or sharp edge.

## Getting started

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt   # once (Python 3.10+)

.venv/bin/python start.py demo     # no hardware: everything simulated, all scenarios work
.venv/bin/python start.py sim      # no hardware, but through the real rover + drone servers on the floor map
.venv/bin/python start.py floor    # the real robots: EP in router mode on the hotspot, Crazyradio plugged in
```

The dashboard opens at http://localhost:8002/. Mix and match: `start.py floor --drone mock` (real rover, simulated
drone), `--rover <ep-ip>` (skip the search), `--drone http://<laptop>:8001` (drone server on another laptop),
`--camera 0` (cage camera on this PC). Webcam on another laptop on the hotspot: run `python camera/stream_webcam.py` there (`pip install opencv-python`), then `start.py floor --camera http://<that-laptop-ip>:8003`. A piece that cannot start (missing package, no robot) is reported in one line and the
rest keeps running; the dashboard shows that robot offline with the reason. Ctrl-C stops the rover and lands the
scout before shutting down. Plan and status of the integration: [docs/integration-plan.md](docs/integration-plan.md).

**Floor demo with the riding Crazyflie (the way we run it now)**

```bash
.venv/bin/python start.py floor --rover 192.168.2.1 --no-ai   # EP in direct Wi-Fi mode; no Gemini ("map mode")
```

The EP is most stable in **direct Wi-Fi mode**: the laptop joins the EP's own Wi-Fi and the EP is always 192.168.2.1.
That laptop is then off the hotspot, so run everything on it (Crazyradio in its USB port, dashboard on
http://localhost:8002/). Other laptops cannot open the dashboard and a webcam on another laptop is out of reach,
unless this laptop also has a second network (phone USB tethering or a USB Ethernet adapter). There is no internet
either, which `--no-ai` does not need. If the Crazyflies' radio drops while the EP streams, switch the EP's Wi-Fi to
5.8 GHz in the RoboMaster app (the Crazyradio uses 2.48 GHz).

1. The Crazyflie `...04` rides on the rover's back-right corner; with a radio drone `start.py` sets `ROVER_RIDER=target`.
   Its Lighthouse pose is then the rover's ONLY position source: no wheel odometry, start cross or anchor is mixed
   in, and tracking glitches (a read off the map, or a jump faster than the rover can drive) are thrown away. If the
   rider loses tracking for more than 1 s the rover stops and waits (map: rover drawn faded).
2. Once after mounting it: **Hands-on control → Calibrate rider** (needs 40 cm free around the rover). The rover
   drives 25 cm forward, turns half a circle and back; that measures where the Crazyflie sits and how it is turned.
   The result is kept in `runs/rider_mount.json` and wins over `rider_mount` in `manager/arena.floor.json`.
3. Measure `rover.grip_reach_m` in `manager/arena.floor.json`: from the centre of the four wheels to the middle of the
   open jaws with the arm in *pick*. `rover_approach` puts the bottle's spot exactly there (within 2 cm).
4. Put the bottle on the scenario's spot (`floor_*` scenarios: x -0.2, y 0.0) and run **Recover a hostile drone**.
   The rover drives up facing it, lines the gripper up on the spot, grips, and carries it to the disposal zone.

Without a rider (`ROVER_RIDER` unset) the old way still works: place the rover on the start cross and click
**Rover is on the start cross**, or anchor it from a Crazyflie.

---

## 1. Architecture

```mermaid
flowchart TB
    H([Human: mission text/voice, Approve / Change / Stop])
    D[Dashboard ✅ /ui/<br/>reads /state every 0.5 s]
    M[AI manager ✅ :8002<br/>/mission /approve /stop /state + dashboard]
    L{{LLM/VLM<br/>Gemini Robotics-ER 2<br/>tool calling}}
    A1[Rover adapter<br/>HTTP :8000]
    A2[Drone adapter<br/>HTTP :8001 drone_server.py ✅]
    A3[ROS adapter<br/>rosbridge websocket ✅]
    R1[RoboMaster EP<br/>rover/robot_server.py ✅]
    R2[Crazyflie scouts<br/>cflib + Lighthouse]
    R3[Turtlebot / ROSbot<br/>Nav2, backup]

    H --> D --> M --> L
    L -- "tool calls" --> A1 & A2 & A3
    A1 --> R1
    A2 --> R2
    A3 --> R3
    R1 & R3 -. "camera JPEG" .-> L
```

Four layers: **dashboard → AI manager → adapters → robots**. The rule that keeps it parallelisable: *the LLM only ever sees plain Python tool functions* (`scout_area`, `ground_go_to`, …). It never knows which robot is real, simulated or mocked. If a robot dies, swap its adapter; the mission keeps running.

### The control loop (every orchestrator in this repo follows it)

```
plan → (human approves) → act via tools → look (camera) → verify → continue / re-plan / report
```

- **Plan**: LLM turns the sentence into steps and picks which robot does what.
- **Act**: LLM calls tools. We use `google-genai` *automatic function calling*, so the SDK runs the call loop and executes our Python functions for us.
- **Verify**: a camera frame goes back to the model ("is this the red cup? is it grippable?"). Never trust dead-reckoning alone.
- **Human**: approve / change / stop. ⚠️ Not implemented in code yet (see §5).

---

## 2. Files in this repo

| File | What it is | Orchestration role |
| --- | --- | --- |
| [fleet.py](fleet.py) | **Main multi-robot orchestrator.** Crazyflie scouts + RoboMaster EP, ROS-free, one shared Lighthouse frame. | The reference implementation of drone-finds / rover-fetches. |
| [brain.py](brain.py) | **Single-robot closed loop.** Look → decide → act → look, VLM sees the camera each turn. | The verify-with-camera pattern, against the rover HTTP API. |
| [fleet_agent.py](fleet_agent.py) | **ROS 2 adapter layer** over rosbridge (`roslibpy`). One `Robot` class per platform. | Backup path if we get a Turtlebot/ROSbot instead of (or in addition to) the EP. |
| [gesture_teleop.py](gesture_teleop.py) | Webcam hand gestures → `/speed`. | Manual override / demo extra. Not part of the AI loop. |
| [rover/robot_server.py](rover/robot_server.py) | HTTP server (:8000) wrapping the EP's plaintext SDK; runs on any Mac/Linux laptop on the hotspot. Also serves a keyboard teleop page at `/`. `MOCK=1` runs it without hardware. | The rover API every file above calls. Setup: [rover/README.md](rover/README.md). |
| [rover/control1.py](rover/control1.py) | The original stand-alone browser teleop (EP in AP mode). | Manual driving only; can't run alongside `robot_server.py` (the EP takes one controller). |
| [drone_server.py](drone_server.py) | HTTP server (:8001) for the scout Crazyflie (and the target Crazyflie's pose) over cflib: preemptible flights, geofence `CAGE`, Lighthouse diagnostics in `/health`. `MOCK=1` without a radio. [scan_drones.py](scan_drones.py) lists the drones the radio hears; [calibration.yaml](calibration.yaml) is the Lighthouse base-station geometry. | The drone API the mothership calls. Install: `requirements-drone.txt`. |
| [manager/](manager/) | The mothership (:8002): planner, executor, safety, telemetry, and the dashboard at `/ui/`. | Orchestrates both servers. Real floor: `ARENA=manager/arena.floor.json`. |

All Python files use env vars for config; none take a config file.

| Env var | Used by | Meaning |
| --- | --- | --- |
| `GEMINI_API_KEY` | all LLM files | Gemini credentials. |
| `MODEL` | all LLM files | Defaults to `gemini-robotics-er-2-preview`. Fallback: `gemini-robotics-er-1.5-preview`. |
| `ROBOT` | fleet, brain, gesture_teleop | Rover server base URL, default `http://localhost:8000`. |
| `SCOUTS` | fleet | Comma-separated Crazyflie radio URIs of flying drones. |
| `RIDER` | fleet | Radio URI of the **unarmed** Crazyflie taped to the EP (gives the EP its pose). |
| `ROBOTS` | fleet_agent | `name=host:port,…` of rosbridge endpoints. |
| `MOCK=1` | fleet, fleet_agent | Fake robots, no hardware, no `cflib`/`roslibpy` import. ⚠️ `brain.py` has **no** mock mode. |

---

## 3. Shared conventions (the contract)

Break these and the robots end up in different places. Everyone builds against them.

1. **One frame.** Every position is **metres in the drone's Lighthouse frame**. Arena is roughly `x 0..4`, `y 0..3`.
2. **Yaw in degrees** at the HTTP/cflib boundary, **radians** for Nav2 goals (`fleet_agent.go_to`).
3. **How the rover gets a pose in that frame** (shared-map options, best first):
   1. ✅ *Rider* (what `fleet.py` implements): a spare Crazyflie + Lighthouse deck taped on the EP, motors never armed. We only *read* `stateEstimate.x/y/yaw`.
   2. 🟡 Fallback: start the EP on a tape cross at a known spot/heading and use wheel odometry (`/pose`). Drifts over a few metres.
   3. 🟡 ArUco + overhead camera.
4. **Sensing split.** Drones cannot identify things. They only report *"something sticks up from the floor at (x, y)"*, detected as a dip in the downward range sensor (`range.zrange`) below the median floor distance by `DIP_M` (0.12 m). The rover's camera + VLM does identification.
5. **Tool docstrings are the prompt.** `google-genai` turns each function's name, type hints and docstring into the tool description the model sees. Write them for the model: state units, frame, limits and side effects.
6. **Tools return dicts/strings, not exceptions.** Return `{"arrived": False, …}` so the model can re-plan. Don't raise into the SDK loop.
7. **Safety caps live in the adapter, not the prompt.** Don't rely on the model to behave:
   - `fleet_agent.drive`: linear clamped to ±0.2 m/s, duration hard-capped at 5 s, zero-velocity published at the end.
   - `fleet.ground_go_to`: speed clamped to ±0.3 m/s, 30 s timeout, `/stop` on exit.
   - `brain.py`: system prompt asks for ≤ 1 m per step (⚠️ prompt only, not enforced in `move`).
8. **Flight rules.** Drones fly only in the cage/test zone, with a mentor present for every flight and field run.

---

## 4. How each orchestrator works

### 4.1 `fleet.py`: drone finds, rover fetches (main demo)

Tools exposed to the LLM:

| Tool | Does | Notes |
| --- | --- | --- |
| `scout_area(x_min, y_min, x_max, y_max)` | Splits a lawnmower path (0.4 m lane spacing, `FLY_Z` = 0.6 m) across all `SCOUTS`, flies them in parallel threads, returns `{"objects_found_at": [{x, y, found_by}]}`. | Blocking. Drones land afterwards. Max one hit per drone per run. |
| `ground_go_to(x, y, tolerance_m=0.15, timeout_s=30)` | Closed-loop P-controller (`k=0.8`) on the rider's absolute pose, sends `/speed`. Mecanum, so no turning needed. | Returns `{"arrived", "pose"}`. |
| `ground_gripper("open"\|"close")` | `GET /grip` | |
| `ground_look()` | Grabs `/snapshot`, asks the VLM "what object is directly in front and how far?" | Returns text. |

Typical run the model should produce: `scout_area` → pick a hit → `ground_go_to` → `ground_look` → `ground_gripper("open")` → approach → `ground_gripper("close")` → `ground_go_to(0, 0)`.

```bash
MOCK=1 python fleet.py "Search the left half of the arena, then bring me whatever you find"
```

⚠️ Verify on the floor before trusting: Lighthouse origin/axes (ask mentors), `SIGN_Y` (EP strafe sign; flip if it strafes the wrong way), arming on the brushless Crazyflie, and the `zrange` baseline at your flight height.

### 4.2 `brain.py`: single robot, look-act-look

Tools: `move(forward_m, left_m, turn_left_deg)`, `gripper(action)`, `arm(forward_mm, up_mm)`, `look()`. The system prompt enforces *small steps, look after every move, rotate 45° if the target is not visible*.

Flow in `run()`: take a snapshot → send the task and image → model calls tools (SDK loop) → if `look()` was called during that turn, attach the new JPEG to the next message and continue → stop when the model finishes a turn without calling `look()` or after `max_turns` (12).

⚠️ `look()` returns a string; the **image itself is attached one turn later**, not inside the tool result. The model sees what it asked for a message late.

### 4.3 `fleet_agent.py`: ROS 2 via rosbridge

`Robot(name, host, port, cmd_vel, cam, goal, twist_type)` is the adapter: publishes `/cmd_vel`, publishes Nav2 goals to `/goal_pose` (frame `map`), subscribes to a compressed camera topic. Tools: `drive`, `go_to`, `list_robots`. One `generate_content` call with the latest camera images attached.

⚠️ `go_to` is **fire-and-forget**: it returns "goal sent" and does not wait for arrival. Poll status or camera to confirm.
⚠️ Topic names/types differ per robot and ROS distro. Run `ros2 topic list -t` on the robot before trusting the defaults.
⚠️ Different coordinate frame (Nav2 `map`) than the Lighthouse frame. Using both in one mission needs the transform between them.

### 4.4 `gesture_teleop.py`

Webcam hand → `/speed` at 5 Hz: palm = stop, index finger = forward, fist = backward, hand left/right of centre = turn. Useful as a human override; independent of the LLM.

---

## 5. Plan vs. code: what's missing

The Team Plan specifies an HTTP service layer. Today the repo contains the *orchestration logic*, but not the services around it. Closing these gaps is the actual work.

| Planned (Team Plan) | State | Owner |
| --- | --- | --- |
| Rover server `:8000` with `/health /drive /speed /grip /arm /snapshot /stop /pose` | ✅ `rover/robot_server.py`: run on the real EP (STA mode; direction check passed); arm presets and stop-mid-drive still to check (F1) | Rover squad |
| Drone server `:8001` with `/takeoff /goto /scan /land /dock /pose /health` | ✅ `drone_server.py`: flown on the real scout; geofence (`CAGE`), battery check, `/land` preempts any flight; `/pose?cf=target` needs `TARGET_URI` | Drone squad |
| AI manager `:8002` with `/mission /approve /stop /state` | ✅ in `manager/` (on mocks; not yet on real robots). `fleet.py::run` stays the one-shot reference | AI manager squad |
| Human **approve / change / stop** gate | ✅ in `manager/` (plan → Approve → run, `409` on stale approvals, Stop from any state) | AI manager squad |
| Re-plan on failure, live plan change ("skip the left side…") | ✅ in `manager/` (supervisor checkpoints + `/change`; the live change needs a Gemini key) | AI manager squad |
| Dashboard reading `/state` | ✅ `manager/static/`, served at `/ui/`: map, plan, approvals, live rover video, robot health, manual control | Dashboard |
| `MOCK=1` everywhere | 🟡 Done in `fleet.py`, `fleet_agent.py` and `manager/` (`ROVER=mock DRONE=mock`); missing in `brain.py` | AI manager squad |
| Unified drone + rover on one map | ✅ in `fleet.py` via the rider trick; the digital-twin view is 🟡 | Shared-map squad |

### Rover HTTP API (`rover/robot_server.py`; every endpoint is a GET, curl-able, JSON `{ok, ...}`)

| Endpoint | Params | Used by | Meaning |
| --- | --- | --- | --- |
| `GET /health` | none | manager (floor check) | `{battery, camera, moving, last}`. |
| `GET /drive` | `x`, `y`, `z` (default 0), optional `v`, `vz` | brain, manager | **Blocking relative move**: forward m, left m, turn-left deg; returns `moved` and `pose`. Max 2 m / 360°. |
| `GET /speed` | `x`, `y`, `z`, `t` (default 1) | fleet, gesture_teleop, manager | **Velocity** (m/s, m/s left, deg/s left), clamped to 0.5 m/s / 90 °/s; stops by itself after `t` (max 3 s). |
| `GET /grip` | `action=open\|close` | brain, fleet, manager | Gripper; returns `state`. |
| `GET /arm` | `preset=pick\|carry\|home`, or `x`, `y` (mm, relative), or nothing (read position) | brain, manager | Arm. Presets are placeholders until F1. |
| `GET /snapshot` | none | brain, fleet, manager | Full-resolution JPEG; `503` while there is no frame. |
| `GET /stop` | none | fleet, manager | Zero velocity now; also interrupts a running `/drive`. |
| `GET /pose` | `reset=1` optional | manager | Wheel odometry since server start: `{x, y, yaw}`, y left, yaw CCW. |

**One convention: +y = left, +z/yaw = counter-clockwise on every endpoint.** The server converts to the SDK's +y = right / +z = clockwise (`SDK_Y_SIGN`, `SDK_Z_SIGN`). `fleet.py`'s `SIGN_Y` therefore should be `+1` against this server.
HTTP `503` means the server is up but the EP didn't answer; a refused command is `200` with `ok: false`.

### AI manager ✅ (mocks) / 🟡 (real robots)

**Current scope and work packages (drone-recovery demo, robot server contracts, dashboard): [docs/mothership-plan.md](docs/mothership-plan.md).** Read it first. Design rationale and the `/state` contract: [docs/ai-manager-plan.md](docs/ai-manager-plan.md).

#### AI manager quick start

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-manager.txt
.venv/bin/python -m pytest                                   # ~360 tests on mock robots, ~30 s

# the mothership: server + dashboard on :8002, fake robots, no API key needed
ROVER=mock DRONE=mock MOCK_TIME_SCALE=10 .venv/bin/python -m manager.server
open http://localhost:8002/                                  # the dashboard (light / dark toggle in the bar; press / to type a mission)

# a whole mission in the terminal instead
MOCK_TIME_SCALE=20 .venv/bin/python -m manager.cli --scenario t3_recover_hostile --auto-approve --mentor
MOCK_TIME_SCALE=20 .venv/bin/python -m manager.cli "A hostile drone crashed in field A. Find it and move it to the disposal zone." --mentor

# real robots, one env var each (mixed mode works); add GEMINI_API_KEY for AI planning
ROVER=http://<win-ip>:8000 DRONE=http://<drone-ip>:8001 GEMINI_API_KEY=... .venv/bin/python -m manager.server
```

#### On the real floor (three processes; any of them can be a mock)

```bash
# 1. rover: EP in STA mode on the hotspot (rover/README.md)
ROBOT_HOST=<ep-ip> .venv/bin/python rover/robot_server.py                  # :8000   (MOCK=1 for a fake EP)
# 2. drone: Crazyradio plugged in, base stations on (pip install -r requirements-drone.txt)
.venv/bin/python drone_server.py                                            # :8001   (MOCK=1 for no radio)
# 3. the mothership on the measured floor frame
ARENA=manager/arena.floor.json ROVER=http://localhost:8000 DRONE=http://localhost:8001 .venv/bin/python -m manager.server
open http://localhost:8002/          # live rover video, battery, Lighthouse status, manual control
```

Without `GEMINI_API_KEY` everything except reading the rover camera works (manual control, scripted plans up to the inspect step). The plan for the rest of the integration and the railing camera: [docs/integration-plan.md](docs/integration-plan.md).

Three ways to get a plan, all through the same validation and safety (the AI is optional): type a mission (`POST /mission`, Gemini or the built-in plan), load a **scenario** from [scenarios/](scenarios/) (`POST /scenarios/{name}/load`, no AI), or run one action by hand (`POST /manual`, between missions). The gripper will not close on a hazardous finding until the operator answers *yes* to the authorization question; that gate is in code and applies to every plan source.

#### Backup: overhead 4K webcam as the "drone camera"

If the scout cannot fly (or as its eyes when it does), a webcam on the ceiling looking straight down finds the object, and the rover fetches it. Code: [manager/overhead.py](manager/overhead.py).

- **Calibration:** 4+ ArUco tags (`DICT_4X4_50`, ids 0-3) taped flat on the floor **just outside the drivable area**, their centres measured in the Lighthouse frame and entered in `arena.json` → `overhead.floor_tags`. The pixel→metre homography is re-fitted on every frame (a bumped camera heals itself; a hidden tag keeps the last fit).
- **Rover pose:** tag 10 on the EP's top plate, its top edge towards the rover's front. Its height (`rover_tag_height_m`) and `camera_height_m` correct the parallax. A fresh sighting becomes the rover's position fix (`pose_source: overhead_camera`), and wheel odometry fills the gaps. This removes the start-cross drift.
- **Finding things:** Gemini points at the object in the frame, and the point goes through the homography. New action `camera_locate` (no robot moves). With the camera installed, `drone_search` also uses it as the scout's detector instead of the simulated one.
- **Dashboard:** an "Overhead" card shows the live annotated frame. If the yellow arena outline is crooked, the tag positions are wrong.

```bash
.venv/bin/python -m manager.overhead tags overhead_tags/          # print these (same size, flat, matte)
.venv/bin/python -m manager.overhead check 0 --save check.jpg     # tags seen, reprojection error, rover pose
OVERHEAD_CAM=0 ROVER=http://<win-ip>:8000 DRONE=http://<drone-ip>:8001 GEMINI_API_KEY=... .venv/bin/python -m manager.server
# then load the scenario "overhead_fetch" (no scout flight), or type a mission that mentions the overhead camera

# no hardware: a synthetic top-down render of the mock world, with real tags and the real pipeline
ROVER=mock DRONE=mock OVERHEAD_CAM=mock MOCK_TIME_SCALE=10 SCENARIO=overhead_fetch .venv/bin/python -m manager.server
```

Env vars: `OVERHEAD_CAM` (`0`, `/dev/video0`, a stream URL, a `.jpg`, or `mock`), `OVERHEAD_WIDTH` / `OVERHEAD_HEIGHT` (default 3840×2160, captured as MJPG), `OVERHEAD_TARGET` (what `drone_search` looks for, default "crashed drone"), `DETECTOR=auto|sim|overhead`.
⚠️ For the floor: measure the tag centres and the camera height; check that the rover heading matches (set `rover_tag_yaw_offset_deg` if the tag was stuck on rotated). Gemini overhead pointing is untested against the real model. The mock overhead does not feed the mock rover's localization, because the mock view runs on wall-clock time and the mock rover on the scaled clock.

Env vars: `ROVER` / `DRONE` (`mock` or a URL), `DOCK_MODE=1` (old "drone rides the rover" mode), `SCENARIO=<name>` (load at start), `MOCK_TIME_SCALE` (simulated clock speed-up), `MOCK_FAIL=goto:0.2,grip:0.3` (failure injection), `CHANGE_NEEDS_APPROVAL=1`, `PERCEPTION=mock|gemini`, `SEARCH_Z` / `MARK_Z` / `SEPARATION_M` / `DETECTOR_*`, `ROVER_SPEED_Y_SIGN` / `ROVER_DRIVE_Y_SIGN` / `ROVER_ARM_MODE`. Opt-in test layers: `pytest -m live` (real Gemini) and `pytest -m ui` (the dashboard in headless Chromium; `requirements-ui-tests.txt`).

---

## 6. Rules for working in parallel (humans and agents)

1. **Stay in your lane.** One squad owns one piece (Team Plan, "Who does what"). If you need a change in someone else's interface, ask them; don't edit their file.
2. **Code against the contract in §3 and §5, not against another squad's implementation.** Build with `MOCK=1` first, then the real robot.
3. **Never move the units or the frame.** Metres, Lighthouse frame, degrees at the boundary.
4. **Every new capability becomes a tool with a docstring** the model can read. If the model can't see it, it can't use it.
5. **Keep safety logic out of the LLM.** Clamps, timeouts, `/stop`, geofence checks go in adapters.
6. **Log every problem and its fix** (this is the "technical depth" score). One line is enough.
7. **A squad is "done" only when its test from the Team Plan passes** (e.g. rover: drives 1 m, grabs a cup, returns a photo).

## 7. Quick start

```bash
pip install google-genai requests          # everyone
pip install cflib                          # drone laptop (mac: brew install libusb)
pip install roslibpy                       # only for the ROS backup path
pip install -r rover/requirements.txt      # rover server (PyAV + Pillow), any laptop on the hotspot
pip install -r requirements-drone.txt      # drone server (Flask + cflib)
pip install mediapipe==0.10.14 opencv-python   # only for gesture_teleop

export GEMINI_API_KEY=...
MOCK=1 python fleet.py "Find anything on the floor and bring it to (0, 0)."   # no hardware needed
MOCK=1 python fleet_agent.py "List the robots and spin the first one 90 degrees left."
ROBOT=http://<windows-laptop-ip>:8000 python brain.py "Drive to the red cup, grab it, and turn around"
```
