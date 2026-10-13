# Mothership plan: drone-recovery demo (execution spec for coding agents)

**Read this first if you are an agent assigned a work package (WP).** It tells you what to build, which files to touch, the exact interfaces, and how to prove you are done. The older [ai-manager-plan.md](ai-manager-plan.md) explains *why* the manager is built the way it is (plan → approve → execute, safety in code). Where the two disagree, **this file wins**.

- **Branch:** `feat/ai-manager`. Commit per WP. Never push unless a human says "push".
- **Python:** 3.10+ on the Mac (`uv venv --python 3.12 .venv && source .venv/bin/activate && pip install -r requirements-manager.txt`).
- **Before and after every WP:** `pytest` must pass (currently ~113 tests, ~9 s, all on mocks).

---

## Status (AI squad work packages, built on mocks)

| WP | State | Where |
| --- | --- | --- |
| **M1** farmland config, `DOCK_MODE`, mock world | ✅ | `config.py`, `arena.json`, `adapters/mock.py` |
| **M2** `drone_takeoff`, `drone_mark`, separation rule, target pose | ✅ | `tools.py`, `controller.Monitor` |
| **M3** dashboard at `/ui/` | ✅ redesigned as a field-operations console, light and dark; verified in headless Chromium | `manager/static/` |
| **M4** `POST /manual`, `GET /arena` | ✅ | `controller.manual`, `server.py` |
| **M5** `POST /plan`, scenarios | ✅ | `scenarios.py`, `scenarios/*.yaml` |
| **M6** triage, authorization gate, disposal, report | ✅ | `tools.py` (`_authorization_gate`), `GET /report` |
| **M7** simulated detector, `drone_search` | ✅ | `detector.py` |
| **M8** planner/supervisor on the farmland story | ✅ unit-tested with scripted LLMs; **not run against live Gemini** | `planner.py`, `supervisor.py`, `fallback_plans.py` |
| **O1** backup: overhead 4K webcam (ArUco calibration, rover tag → pose fix, Gemini `camera_locate`, overhead detector for `drone_search`, dashboard card, `overhead_fetch` scenario, `OVERHEAD_CAM=mock`) | ✅ on synthetic frames; real capture path opened on a laptop webcam; **not run on the floor** | `overhead.py`, `overhead_mock.py` |
| **O1** at any angle (railing camera): parallax towards `camera_floor_xy` | ✅ on synthetic frames | `overhead.py` |
| **R1** rover server | ✅ on the real EP in STA mode: health, pose, camera, drive; direction check passed. Arm presets and stop-mid-drive not checked yet | `rover/robot_server.py` |
| **D1** drone server | ✅ flown on the real scout by the drone squad; the mothership drives it in `tests/test_integration.py` (mock radio) | `drone_server.py` |
| **I1** integration: floor arena, `/health` telemetry, lost-tracking flag, rover video relay, keyless bring-up | ✅ against both servers (mock EP / mock radio); dashboard checked in headless Chromium | `arena.floor.json`, `controller.Monitor`, `server.py` |
| **F1** floor bring-up | 🟡 cage measured; zones, home, overhead tags, arm presets still provisional | [integration-plan.md](integration-plan.md) |

~300 tests on mocks (`pytest`), plus opt-in `pytest -m ui` (real browser) and `pytest -m live` (real Gemini). Nothing here has touched a real robot or the real Gemini API.

**Dashboard design (M3), for whoever touches it next:** an app shell that does not scroll as a page at laptop size (map beside a slim camera and robot-status column; operator panel on the right; a collapsible drawer with log, findings, manual control and scenarios). Whatever needs a human (approve, the authorization question, the result) is the first card in the operator panel, and first of all on a phone. Warm graphite and "survey paper" themes (toggle in the bar, remembered, follows the system by default), IBM Plex bundled in `static/fonts/` so it works offline. The three robot colours were validated for colour-blind safety on both surfaces (all-pairs, worst ΔE 17 against a target of 8) and a test pins them; status colours always come with an icon and a word. The DOM is updated in place, so robot markers glide between the 500 ms polls and plan steps animate their state; `prefers-reduced-motion` turns the motion off. Opt-in `pytest -m ui` drives all of it in a real browser.

**Where the build differs from this spec (and why):**

- **Order:** M7 was built before M5 so scenario loading registers its targets with a real detector, and M3 last so it could cover every endpoint. Each WP is its own commit.
- **Search pattern (M7):** lanes are `√2·r` apart and inset `r/√2` from the edges, not `1.6·r`. A property test over the whole area found that 1.6·r (with the edge inset) leaves a few-millimetre blind strip along the boundary; `√2·r` is the widest spacing that closes it.
- **`ask_human` is a checkpoint:** the supervisor sees the operator's answer (an empty search asks "Search field B?" and acts on the reply). It was described that way but not wired until M8.
- **`rover_inspect`** does not invent a finding for an empty floor (the spec's "create one 0.6 m ahead" applies only when something is visible).
- **Scenario id = file name.** The `name:` key inside the file is ignored, so `POST /scenarios/{name}/load` is unambiguous; ids must be plain slugs (no path tricks).
- **`report.yaml` is written atomically**, and an older report with the same mission id (a previous server run) is kept as `report.<timestamp>.yaml`.
- **`validate_drafts(..., dock_mode=...)`** landed with M1, not M2, because flipping the default to no-docking would otherwise leave the validator rejecting plans the prompt no longer warns about.
- **Fallback plan (M8):** a recovery mission always delivers to `$zone.disposal`; the rule supervisor redirects our own drone to `$home` after the inspection and drops the authorization question.
- **A Stop spends the epoch** (`cancel` stays set), so `manual()` renews it; `approve` is refused while a manual action is still moving robots.

**Flag for the floor (F1):** in `t2_mark_and_drive`, `t3_recover_hostile` and `t4_search_field_a` the scout hovers over the marked spot and then `drone_land` lands it **in place, i.e. at the wreck**. The dashboard map shows it landing exactly on the finding. On the real cage that is the scout touching down on or beside the target Crazyflie. Probably wanted: a `drone_goto` back to the launch pad before `drone_land`. Left as the spec wrote it; it is one extra line in each YAML.

**Also open:** `:8002` has no authentication (anyone on the venue Wi-Fi can approve or stop); `arena.json` numbers are placeholders until F1; the Gemini prompts (triage cues, planner rules) are untested against the real model.

---

## 0. The demo in one paragraph

An enemy drone has crashed in farmland and may carry explosives. A **scout Crazyflie** searches the field and finds it, then hovers over it to **mark the spot**. The **RoboMaster EP** drives there, its camera checks what it is, and, **after a human authorizes it**, the EP grips the drone and carries it to a disposal zone. Everything is controlled from one place: the **mothership** (our server on port 8002 plus its dashboard). The cage floor is the "farmland". Lighthouse plays the role GPS would play outdoors.

Hardware we have: 2 Crazyflies with Lighthouse decks (**scout** + **target**, which plays the crashed drone), 1 RoboMaster EP with gripper, arm and front camera, a Lighthouse system with 4 base stations (see `drone.yaml`), and the team laptops (Macs + one Windows laptop with Python 3.8).

---

## 1. Decisions (do not re-litigate these inside a WP)

### 1.1 Plain Python + HTTP/JSON, not ROS 2

| | ROS 2 | Plain Python HTTP (chosen) |
| --- | --- | --- |
| Crazyflie | Crazyswarm2: Ubuntu 22.04/24.04 only, colcon workspace | `cflib` works on Mac, Windows and Linux; we already have the code in `fleet.py` |
| RoboMaster EP | `robomaster_ros`: Linux, ROS 2 Foxy…Iron, a **forked** SDK built from source | Official `robomaster` SDK on the Windows laptop (Python 3.8), which already works |
| Our laptops | Macs (ROS 2 is not practical there) + Windows | Runs everywhere |
| Venue network | DDS multicast discovery is often blocked on eduroam/guest Wi-Fi | One TCP port per server; debug with `curl` or a browser |
| Setup time | Hours per laptop, risky on hackathon day | Minutes |
| What we'd gain | RViz, rosbag, standard messages | The dashboard map covers what we need from RViz |

**Rule:** no ROS anywhere. If someone already has Crazyswarm2 running on Ubuntu, they may use it *inside* the drone server, but it must still expose the HTTP contract in §4.2. Sources: [robomaster_ros README](https://github.com/jeguzzi/robomaster_ros), [Crazyswarm2 installation](https://imrclab.github.io/crazyswarm2/installation.html).

### 1.2 Star topology: robots never talk to each other

```
                   ┌───────────────────────────── Mac (Het) ─────────────────────────────┐
 browser ─────────►│ mothership  :8002   (manager/server.py + dashboard at /ui/)         │
                   │      │ HTTP                                                          │
                   │      ├──────────► drone_server.py :8001 ── Crazyradio USB ──► scout CF │
                   │      │            (can run on this Mac)              └──────► target CF│
                   └──────┼──────────────────────────────────────────────────────────────┘
                          │ HTTP over team Wi-Fi
                          ▼
                 Windows laptop: robot_server.py :8000 ── Wi-Fi/USB ──► RoboMaster EP
```

- Only **two** network links: browser → mothership, and mothership → rover laptop. Running the drone server on the same Mac as the mothership (Crazyradio plugged into the Mac) removes a third link.
- **Use a team hotspot or travel router**, not eduroam. Eduroam often blocks laptop-to-laptop traffic, and the EP can't log in to it.
- Every message is a plain HTTP GET/POST with JSON. Every position is **metres in the Lighthouse frame**, every yaw is **degrees, counter-clockwise**.

### 1.3 Plans come from three sources; the AI is optional

The mothership executes **plans** (lists of steps from the action vocabulary). A plan can come from:

1. **Manual:** a dashboard button runs one action immediately (`POST /manual`).
2. **Scenario:** a hand-written plan in `scenarios/*.yaml` (`POST /scenarios/{name}/load`).
3. **AI:** a sentence turned into a plan by Gemini (`POST /mission`, already built).

All three go through the same `tools.run_action` (validation + safety). If the AI layer gets cut, sources 1 and 2 still give a full demo. **No WP may make the demo depend on the LLM.**

### 1.4 No docking

Both drones are taken (scout + target), so nothing rides on the EP. Consequences:

- The rover's position comes from the **start cross + wheel odometry** (`localization.py` already supports this: `use_drone_fix=False`). The camera approach corrects the last few centimetres.
- The rule "rover must not move while the drone is in the air" is replaced by a **separation rule** (WP-M2).
- `drone_dock` stays in the code but is unused when `DOCK_MODE=0`, the new default.

### 1.5 The "detector" is simulated, honestly

The Crazyflies have no camera. The scout "detects" the target with a **sensor model**: while searching at height `z`, the target counts as seen when it lies inside the footprint a real downward camera would have (`r = z · tan(FOV/2)`, FOV 60°, so r ≈ 0.35 m at 0.6 m), plus ±5 cm noise and a 5 % miss chance. The target's true position comes from the **target Crazyflie's own Lighthouse pose** (live), or from the scenario file for dummy objects. The scout must really fly the search pattern; it never reads the answer directly. In the pitch: *"Lighthouse stands in for RTK-GPS, and the simulated detector stands in for a downward camera. Everything after detection is real."*

### 1.6 Demo tiers: each one is a complete demo

| Tier | What works | Needs WPs |
| --- | --- | --- |
| **T0** | Dashboard shows both robots live (pose, battery, online) on a map | R1, D1, M1, M3 |
| **T1** | Manual control from the dashboard: drone takeoff/goto/land, rover drive/grip/arm, camera snapshot | + M4 |
| **T2** | Scripted scenario: scout flies to the target and marks it; rover drives there and stops 0.6 m short | + M2, M5 |
| **T3** | Rover camera approach + grip + carry to the disposal zone (with a human authorize step) | + M6, floor calibration |
| **T4** | Scout really **searches** with the simulated detector, instead of knowing the coordinates | + M7 |
| **T5** | AI planner on the farmland story: triage, re-planning, mission sentence → plan | + M8 |

Target times: **T0–T1 Sat 18:00 · T2 Sat 22:00 · T3 Sun 11:00 · T4/T5 by Sun 13:30 if time allows.** If a tier slips, record the video at the tier below.

---

## 2. Work package overview

| WP | Title | Owner | Depends on | Size |
| --- | --- | --- | --- | --- |
| **R1** | `robot_server.py`: rover HTTP server on the Windows laptop | Rover squad | — | M |
| **D1** | `drone_server.py`: drone HTTP server (scout + target) | Drone squad | — | M |
| **M1** | Config + arena + mock changes for the farmland story (`DOCK_MODE=0`) | AI squad | — | S |
| **M2** | Drone tools without docking: `drone_takeoff`, `drone_mark`, separation rule, multi-CF pose | AI squad | M1 | M |
| **M3** | Dashboard served by the mothership (`/ui/`) | AI squad / dashboard person | M1 (can start on today's `/state`) | L |
| **M4** | `POST /manual` + `GET /arena`: manual control through the safety layer | AI squad | M2 | S |
| **M5** | Scenarios: `POST /plan`, scenario files, `GET /scenarios`, load endpoint | AI squad | M2 | M |
| **M6** | Triage + authorization gate + disposal + report | AI squad | M5 | M |
| **M7** | Simulated detector + `drone_search` | AI squad | M2 | M |
| **M8** | AI planner/supervisor prompts and fallback plan for the farmland story | AI squad | M6, M7 | S |
| **F1** | Floor bring-up checklist (measure, calibrate, sign checks) | Everyone, with a mentor | R1, D1 | — |

R1 and D1 belong to the rover and drone squads. Their specs are here so every agent builds against the same contract.

---

## 3. Shared contracts

### 3.1 Units and frames

- Metres, Lighthouse frame (origin and axes as set in cfclient's Lighthouse geometry). Yaw in degrees, counter-clockwise, 0 = +x.
- **Rover body frame** (used in `adapters/base.py`): x forward, **y left**, z = turn-left degrees. `rover/robot_server.py` uses this frame on every endpoint and converts to the SDK's +y = right / +z = clockwise itself, so `ROVER_SPEED_Y_SIGN` / `ROVER_DRIVE_Y_SIGN` stay at their default `1`.
- Every response is JSON with `"ok": true|false`. Errors use `{"ok": false, "error": "<text>"}` with HTTP 200, so clients can read them. Use non-200 only for "server broken".

### 3.2 Step format (plans from any source)

The same shape as `models.StepDraft`. It's flat, and fields an action doesn't use are omitted:

```json
{"robot": "drone", "action": "drone_mark", "target": "$finding.f1", "why": "Hover over the wreck so everyone sees it"}
```

References: `$finding.<id>`, `$zone.<name>`, `$home`, or `"x,y"`.

---

## 4. Robot servers (other squads; specs for their agents)

### 4.1 WP-R1: `robot_server.py` (Windows laptop, Python 3.8, port 8000)

> **As built (`rover/robot_server.py`):** the endpoints, lock, watchdog and `MOCK=1` rules below hold, but it talks DJI's **plaintext SDK** over TCP (like the rover squad's `control1.py`) instead of the `robomaster` package. So it runs on any Mac/Linux laptop on the hotspot, with the EP in STA mode (`ROBOT_HOST=<ep-ip>`). Extras: `/health` also reports camera and last action, `/pose?reset=1`, `/arm` with no params reads the position (for tuning presets), and a teleop page at `/`. See [rover/README.md](../../rover/README.md).

**Goal:** wrap the RoboMaster SDK in a small threaded HTTP server.

**Dependencies:** `pip install robomaster opencv-python` (Python 3.8). Use the standard library's `http.server.ThreadingHTTPServer` with no other web framework, to keep the Windows install trivial.

**Connection:** `ep = robot.Robot(); ep.initialize(conn_type="sta")`. `"sta"` means the EP joins the team hotspot. Use `"ap"` if the laptop joins the EP's own Wi-Fi, or `"rndis"` for a USB cable. Choose the one that keeps the laptop on the team network.

**Endpoints** (all `GET` with query parameters, all return JSON unless noted):

| Path | Params | Behaviour | SDK hint |
| --- | --- | --- | --- |
| `/health` | — | `{"ok": true, "battery": <pct>}` | `ep.battery.sub_battery_info` |
| `/pose` | — | `{"ok": true, "x", "y", "yaw"}`: odometry since server start, **x fwd, y left, yaw CCW deg** | `ep.chassis.sub_position(cs=0, freq=10, …)` + `sub_attitude` (convert the SDK signs!) |
| `/speed` | `x, y, z, t` | Velocity for `t` seconds, then **auto-stop** (must stop even if no further request arrives) | `ep.chassis.drive_speed(x, y, z, timeout=t)` |
| `/drive` | `x, y, z` | Blocking relative move (m, m, deg) | `ep.chassis.move(x, y, z, xy_speed=0.3).wait_for_completed()` |
| `/stop` | — | Zero speed immediately; **must not wait for the SDK lock** | `ep.chassis.drive_speed(0, 0, 0)` |
| `/grip` | `action=open\|close` | | `ep.gripper.open(power=50)` / `close(power=50)`, then sleep 1 s |
| `/arm` | `preset=pick\|carry\|home` | Move to an absolute preset (tune on the floor) | `ep.robotic_arm.moveto(x, y).wait_for_completed()` |
| `/snapshot` | — | **JPEG bytes**, `Content-Type: image/jpeg` | `ep.camera.start_video_stream(display=False)` once; `read_cv2_image()` → `cv2.imencode('.jpg', …)` |

**Rules:**
- One `threading.Lock` around every SDK motion, gripper or arm call; `/stop` and `/pose` skip it.
- Positive `y` in this API means **left**. Convert internally (the SDK's +y is right). Write the sign check result in the file's docstring.
- If no `/speed` or `/drive` arrives for 1 s while moving, stop (watchdog thread).
- `MOCK=1` mode without the SDK: integrate commanded speeds into a fake pose and return a grey JPEG. This lets the mothership team test the network path.

**Done when:** from the Mac, `curl "http://<win-ip>:8000/drive?x=0.5&y=0&z=0"` moves the EP 0.5 m forward, `/pose` shows x ≈ 0.5, `/snapshot` opens as an image in a browser, `/grip?action=close` closes the gripper, and `/speed?x=0.2&y=0&z=0&t=1` stops by itself after 1 s.

### 4.2 WP-D1: `drone_server.py` (Mac with the Crazyradio, port 8001)

**Goal:** wrap `cflib` for two named Crazyflies: `scout` (flies) and `target` (sits on the floor, powered, and only reports its pose).

**Dependencies:** `pip install cflib fastapi uvicorn` (Mac: `brew install libusb`). FastAPI runs every sync endpoint in a thread pool, which gives us `/land` during `/goto` for free.

**Configuration (env):** `SCOUT_URI=radio://0/80/2M/E7E7E7E7E1`, `TARGET_URI=radio://0/80/2M/E7E7E7E7E9`. Both drones use the same channel and data rate, so one radio serves both. `TARGET_URI` is optional. If it's empty, `/pose?cf=target` returns `{"ok": false, "error": "no target drone"}`.

**Endpoints** (`GET`, JSON):

| Path | Params | Behaviour | cflib hint |
| --- | --- | --- | --- |
| `/pose` | `cf=scout\|target` (default `scout`) | `{"ok", "x", "y", "z", "yaw", "battery_v", "state": "landed"\|"flying"}` from a 10 Hz log stream | `LogConfig` with `stateEstimate.x/y/z/yaw`, `pm.vbat` |
| `/takeoff` | `z=0.6` | Arm if needed, take off, block until done (~2 s) | `cf.platform.send_arming_request(True)` (Brushless only); `cf.high_level_commander.takeoff(z, 2.0)` |
| `/goto` | `x, y, z` | Fly to an absolute point at ≤ 0.4 m/s and block until it arrives (duration = distance / 0.3 s, min 1 s) | `high_level_commander.go_to(x, y, z, 0, duration)` |
| `/land` | — | Land in place (z → 0 over 2 s), works **while another call is running** | `high_level_commander.land(0.0, 2.0)` then `stop()` |
| `/health` | — | `{"ok", "scout": "connected"\|"…", "target": …}` | |

**Rules:**
- Keep both links open for the whole run (`SyncCrazyflie(...).open_link()` once at startup). Reset the scout's estimator before the first takeoff (`reset_estimator`).
- **Geofence in the server too:** refuse `/goto` outside the cage (`CAGE="x0,y0,x1,y1,zmax"` env) with `{"ok": false}`.
- If the scout's battery is below 3.3 V, refuse `/takeoff`.
- The Lighthouse geometry (computed with cfclient; raw samples in `drone.yaml`) must be **written to both Crazyflies** via cfclient → Lighthouse tab. Otherwise the target reports nonsense.
- `MOCK=1`: no radio. The scout moves instantly to the `/goto` point, and the target sits at `MOCK_TARGET="2.4,1.1"`.

**Done when:** with a mentor present, `curl :8001/takeoff`, then `curl ":8001/goto?x=1&y=1&z=0.6"`, then `curl :8001/land` flies the scout. `curl ":8001/pose?cf=target"` returns the target's position, and moving the target by hand changes it.

---

## 5. Mothership work packages (AI squad)

Key files you will touch: `manager/config.py`, `manager/arena.json`, `manager/models.py`, `manager/tools.py`, `manager/adapters/*.py`, `manager/controller.py`, `manager/server.py`, `manager/state.py`, `manager/perception.py`, `manager/planner.py`, `manager/fallback_plans.py`. **Read [ai-manager-plan.md §17](ai-manager-plan.md) before changing the controller or executor.** It records the threading and epoch rules.

### WP-M1: farmland config, `DOCK_MODE`, mock world

1. **`config.py`:** add `dock_mode: bool = False` (env `DOCK_MODE`, default `"0"`), `search_z: float = 0.6`, `mark_z: float = 1.0`, `detector_fov_deg: float = 60.0`, `detector_noise_m: float = 0.05`, `detector_miss_p: float = 0.05`, `separation_m: float = 0.5`, and `scenario: str = ""` (env `SCENARIO`).
2. **`arena.json`:** replace the zones with `field_a`, `field_b`, `base` (rover start; `home` lies inside it), `disposal`, and keep `test_zone` = the whole cage. Keep `"calibrated": false` and the placeholder numbers until F1. Replace `mock_objects` with one entry: `{"label": "crashed drone", "kind": "hostile_drone", "x": 2.4, "y": 1.1, "height": 0.05, "grippable": true, "color": "#c0392b"}`.
3. **`models.MockObject`:** add `kind: str = "unknown"` (`own_drone | hostile_drone | decoy | unknown`).
4. **`adapters/mock.py`:** when `cfg.dock_mode` is false, the mock drone starts `state="landed"` at `arena.home` offset by +0.3 m in x (a launch pad beside the rover), and `takeoff()` works from `landed`. `dock()` returns `{"ok": false, "error": "docking disabled"}`. Add `MockDrone.pose(cf="scout")`: for `cf="target"`, return the first `hostile_drone`/`own_drone` object as `{"ok": true, "x", "y", "z": 0.0, "state": "landed"}`.
5. **`runtime.build_ctx`:** pass `use_drone_fix=cfg.dock_mode and (...)`, so there's never a drone fix when docking is off.
6. **`planner.RULES` rule 2:** only add it to the prompt when `dock_mode` is true (M8 rewrites the rest).

**Tests:** update the fixtures that assume `docked` at start; add `test_mock_no_dock.py` (the drone starts landed, takes off, target pose readable). **Done when** `pytest` is green and `ROVER=mock DRONE=mock python -m manager.cli "test" --auto-approve --mentor` still finishes.

### WP-M2: drone tools without docking

1. **`adapters/base.py` + `drone_http.py` + `mock.py`:** `DroneAdapter.pose(cf: str = "scout")`. `DroneHttp.pose` passes `cf` as a query parameter. `DroneHttp.takeoff(z)` passes `z`.
2. **`tools.py`:** new actions, using the existing `@action` decorator:
   - `drone_takeoff` (robot `drone`, `mentor=True`): checks battery, takes off to `cfg.search_z`, sets `ctx.drone_out=True`. Returns `{ok, pose}`.
   - `drone_mark` (robot `drone`, `needs=(("target",), ("x","y"))`, `mentor=True`): resolves the location, takes off if not flying, flies to `(x, y, cfg.mark_z)` and hovers. The docstring says it hovers over a spot so people and the rover can see where it is. **It also registers the spot as a finding** (`store.add_findings([{x, y, by: "scout"}])`; dedupe already merges it with an existing one), so later steps can use `$finding.f1`. This matters: `rover_goto` only stops 0.6 m short for *findings*; for a raw `"x,y"` it drives onto the object. Returns `{ok, pose, finding_id}`.
   - Keep `drone_goto`, `drone_land`, `drone_scan`, `drone_dock`.
3. **Separation rule** in `run_action`, replacing the `drone_out` block **when `not cfg.dock_mode`**. For any `moves_rover` action, when the drone is flying with `z < cfg.mark_z - 0.1` and the rover's *destination* is within `cfg.separation_m` of the drone's x/y, refuse with `blocked="separation"`. Destinations: `rover_goto` → the resolved target; other moving actions → the current rover pose. When `dock_mode` is true, keep today's rule unchanged.
4. **`planner.validate_drafts`:** only apply the "rover motion while drone out" check when `dock_mode` is true. Thread the flag through as a parameter; don't import config inside the function.
5. **Monitor** (`controller.Monitor.poll`): also poll `ctx.drone.pose(cf="target")` and store it as `store.robots["target"] = {online, pose}`. The dashboard shows it as ground truth. Failures here must **not** trigger the watchdog.

**Tests:** `drone_mark` flies to `mark_z`; the rover is blocked by separation when the drone is low over its goal and allowed when the drone is at `mark_z`; `/state.robots.target` appears with mocks. **Done when** `pytest` is green.

### WP-M3: dashboard at `/ui/` (no build step)

**Files:** `manager/static/index.html`, `manager/static/app.js`, `manager/static/style.css`. Plain HTML + vanilla JS, no framework or CDN, so it works offline at the venue. In `server.py`: `app.mount("/ui", StaticFiles(directory=<manager/static>, html=True))` and `GET /` → redirect to `/ui/`.

**Data:** poll `GET /state` every 500 ms and `GET /arena` once (M4 adds `/arena`; until then, hard-code the zones from `arena.json`). Images come from `GET /images/{id}.jpg`.

**Layout:** desktop grid; it must also work on a laptop at 1280 px. Sections, top to bottom and left to right:

| Section | Shows (from `/state`) | Controls |
| --- | --- | --- |
| **Header bar** | Mission status (`mission.status`), plan source (`mission.source`, from M5), mentor toggle state, connection lights for rover/drone/target (`robots.*.online`), last RTTs (`metrics`) | **STOP** (big, red, always visible) → `POST /stop`; mentor toggle → `POST /mentor` |
| **Map** (SVG) | Arena bounds, cage, zones with names, rover (triangle pointing along yaw + `pose_source`), scout (circle + z label), target ground truth (hollow red circle, labelled "truth"), findings (✕ + id + category), disposal zone highlighted | Click on map → fills x/y fields in Manual |
| **Mission** | Current plan: one row per step with status colour, `why`, action, result summary; previous plan greyed when a re-plan happened; `mission.reason` | **Approve** (sends current `plan_version`), mission text box → `POST /mission`, change box → `POST /change`, **Reset** |
| **Question / authorization** | `mission.pending_question`, when set, in a yellow box | **Yes** / **No** → `POST /change {"text": "yes"}` / `"no"` |
| **Scenarios** | List from `GET /scenarios` (M5) | **Load** → `POST /scenarios/{name}/load` |
| **Manual** | Last manual action + result (`manual`, from M4) | Drone: Takeoff, Land, Go to (x, y, z), Mark (x, y). Rover: Drive fwd 0.3 m, back 0.3 m, turn ±45°, Go to (x, y), Arm pick/carry/home, Grip open/close, Snapshot. All → `POST /manual` |
| **Camera** | Latest image (`last_image_id`), auto-refresh when the id changes | — |
| **Findings** | Table: id, x, y, category, hazardous, authorized, status, thumbnail | — |
| **Log** | `messages` (role-coloured), newest at the bottom, auto-scroll | — |

**Rules:** every button disables itself while its request is in flight and shows the error text from a 409/422 response. The STOP button must work even if the rest of the page has a JS error, so give it its own inline `onclick`. Map scale: fit the arena bounds to the SVG width, with y pointing up.

**Done when:** `ROVER=mock DRONE=mock uvicorn manager.server:app --port 8002`, then http://localhost:8002/ shows the map with the rover, scout and target. Typing a mission and pressing Approve shows the steps progressing and the camera image updating, and STOP works mid-run.

### WP-M4: manual control + arena endpoint

1. **`Controller.manual(action: str, args: dict)`:**
   - Allowed only when `store.status in IDLE_STATES`; otherwise raise `Conflict`. Also raise `Conflict` if a manual action is already running.
   - Under the lock, build `ctx = dataclasses.replace(self.base_ctx, cancel=self._cancel, ask=None, drone_out=self._drone_in_air())`.
   - Start a daemon thread that runs `tools.run_action(ctx, action, args)`.
   - Record `store.manual = {"action", "args", "status": "running"|"done"|"failed", "result"}`, and `store.add_image` for `rover_snapshot`.
   - **`stop()` must cancel it.** It already sets `self._cancel`; make sure the manual thread's ctx captured that same Event.
2. **New tool `rover_snapshot`** (robot `rover`): takes a snapshot, stores the image, returns `{ok, image_id}`.
3. **New tool `rover_drive`** (robot `rover`, `needs=(("x","y","z"),)`, `moves_rover=True`): a relative move clamped by `safety.clamp_move`. It's manual-only, so add a docstring note telling planners to prefer `rover_goto`.
4. **`state.py`:** add a `manual` field (default `None`) to `to_json()`. Only additive changes are allowed.
5. **`server.py`:**
   - `POST /manual {"robot", "action", "args": {}}` → 202 `{"ok": true}`. Validate that the action exists and belongs to `robot` (`validate_step`); otherwise 422.
   - `GET /arena` → the arena as JSON: bounds, cage, zones, home, camera.

**Tests:** manual goto runs on mocks and ends `done`; manual while running → 409; stop during a manual goto halts it. **Done when** green, and the M3 manual buttons work on mocks.

### WP-M5: scenarios and submitted plans

1. **`POST /plan {"title": str, "steps": [StepDraft, ...]}`** → `Controller.submit_plan(title, drafts, source="manual")`. It does the same as `start_mission`, but skips the planner:
   - `store.new_mission(title)`
   - check `planner.validate_drafts` (422 with the problems if any)
   - `restart_step_ids`, `set_plan(drafts_to_steps(...))`
   - `set_status("awaiting_approval", source=source)`

   Then Approve works exactly as before.
2. **`models.MissionInfo`:** add `source: Literal["ai", "fallback", "scenario", "manual"] = "ai"` (additive). The AI path sets `"ai"` or `"fallback"`.
3. **Scenario files** in `scenarios/` (repo root), YAML (add `pyyaml` to `requirements-manager.txt`):

   ```yaml
   name: hostile_drone_field_a
   title: "Hostile drone down in field A"
   mission: "A hostile drone crashed somewhere in field A. Find it, check it, and move it to the disposal zone if it is safe to handle."
   targets:                      # ground truth for the detector and the mock world
     - id: t1
       kind: hostile_drone       # own_drone | hostile_drone | decoy
       source: live              # live = pose from drone server cf=target; fixed = use x, y below
       x: 2.4                    # used for 'fixed', and as the mock position for 'live'
       y: 1.1
   plan:                         # optional scripted plan; if absent, loading starts an AI mission with `mission`
     - {robot: drone, action: drone_mark, target: "2.4,1.1", why: "Scout marks the crash site"}
     - {robot: rover, action: rover_goto, target: "$finding.f1", why: "Rover drives to the marked spot (stops 0.6 m short)"}
     - {robot: drone, action: drone_land, why: "Scout lands so the rover can work"}
     - {robot: rover, action: rover_inspect, question: "What is in front of the robot?", why: "Camera checks the object"}
     - {robot: manager, action: report, text: "Crash site reached and photographed.", why: "Tell the operator"}
   ```

   Ship these files: `t2_mark_and_drive.yaml` (the plan above, Tier T2), `t3_recover_hostile.yaml` (T3, see M6), `t4_search_field_a.yaml` (T4, uses `drone_search`), `own_drone_home.yaml` (own drone → `$home`), `decoy_bottle.yaml` (decoy → skipped).
4. **`manager/scenarios.py`:** `list_scenarios()`, `load_scenario(name) -> Scenario` (a pydantic model). Loading:
   1. Register the targets with the detector (M7). If anything is mocked, replace `world.objects` with the scenario targets.
   2. Plan present → `submit_plan(title, plan, source="scenario")`; otherwise → `start_mission(mission)`.
5. **Endpoints:** `GET /scenarios` → `[{name, title, has_plan}]`; `POST /scenarios/{name}/load` → `{"ok", "mission_id"}`.
6. **`cli.py`:** add `--scenario <name>` as an alternative to the mission text.

**Tests:** each shipped scenario loads, validates, and (with `--auto-approve`) runs to `done` on mocks. **Done when** green and T2 runs end to end on mocks from the dashboard.

### WP-M6: triage, authorization, disposal, report

1. **Perception** (`perception.py`):
   - Extend `_Inspection` with `category: Literal["own_drone", "hostile_drone", "not_a_drone", "unknown"]`, `hazardous: bool` and `reason: str`.
   - Gemini prompt: describe the visual cues the team uses on the floor (**red tape = hostile / possible payload, blue tape = ours**), and tell it to treat unknown drones as hazardous.
   - `MockPerception.inspect` reads `kind` from the nearest visible mock object (`decoy` → `not_a_drone`; `hostile_drone` → hazardous).
2. **`models.Finding`:** add `category`, `hazardous: Optional[bool]`, `authorized: bool = False`, `status: Literal["found", "inspected", "recovered", "skipped"] = "found"`. `rover_inspect` writes `category`/`hazardous`/`status="inspected"` to the nearest finding (it already does this for `label`).

   **Safety net:** if no finding is within 1.5 m (e.g. a manual drive), `rover_inspect` creates one 0.6 m ahead of the rover (`by: "rover"`).
3. **New tool `request_authorization`** (robot `manager`, `needs=(("finding_id",),)`):
   - Asks via `ctx.ask(f"Finding {id} looks like a {category}. Authorize the rover to pick it up? (yes/no)")`.
   - The answer starts with `y` → `authorized=True`, return `{ok, authorized: true}`. Otherwise `status="skipped"`, return `{ok: false, error: "operator declined"}`.
4. **Gate in `run_action`** (code, not prompt): `rover_grip` with `mode="close"`, where the nearest finding within 1.0 m of the rover is `hazardous and not authorized` → `_err("not authorized: ask the operator first", blocked="authorization")`.
5. **`rover_check_grip` success** → the nearest finding's `status="recovered"`. `rover_goto "$zone.disposal"` is just a goto.
6. **Report:**
   - `GET /report` → `{mission, started, finished, findings: [...], steps: [...]}`.
   - When a mission ends (`done` / `failed` / `stopped`), the controller writes it to `runs/<mission_id>/report.yaml`.
7. **`scenarios/t3_recover_hostile.yaml`**, the scripted T3 plan:
   1. drone_mark (registers f1)
   2. rover_goto `$finding.f1`
   3. drone_land
   4. rover_search "crashed drone"
   5. rover_inspect
   6. request_authorization f1
   7. rover_arm pick
   8. rover_grip open
   9. rover_approach "crashed drone"
   10. rover_grip close
   11. rover_arm carry
   12. rover_check_grip
   13. rover_goto `$zone.disposal`
   14. rover_grip open
   15. rover_arm home
   16. report

**Tests:** hostile without authorization → the grip is blocked; with "yes" → recovered; decoy → `not_a_drone`, and the plan's supervisor checkpoint skips it (RuleSupervisor: when the category is `not_a_drone`, decide `done` with reason "not a drone, nothing to recover"). **Done when** green and T3 runs on mocks, including answering the question from the dashboard.

### WP-M7: simulated detector + `drone_search`

1. **`manager/detector.py`:** `class SimDetector`, built from a target provider, the camera FOV, noise, miss probability and an rng.
   - `set_targets(list[Target])`, where `Target = {id, kind, source, x, y}`.
   - `truth() -> list[{id, x, y}]`: for `source=live`, read `drone.pose(cf="target")` (skip it on error); otherwise use the fixed x/y.
   - `observe(drone_pose: dict) -> list[{x, y, target_id}]`: every truth point within `r = z·tan(fov/2)` of the drone's x/y. Skip each with `miss_p`, and add Gaussian noise of `noise_m` to the reported x/y.
   - Built in `runtime.build_ctx` and placed on `Ctx` as `ctx.detector` (add the field with default `None`).
2. **Tool `drone_search`** (robot `drone`, `needs=(("target",), ("x0","y0","x1","y1"))`, `checkpoint=True`, `mentor=True`):
   1. Resolve the rectangle and clip it to the cage, reusing `drone_scan`'s code (factor it into a helper).
   2. Build a lawnmower of waypoints at `cfg.search_z`, with lane spacing `1.6·r` and waypoint spacing `r` along each lane.
   3. Take off if needed, then `goto` each waypoint. After each one, call `ctx.detector.observe(ctx.drone.pose())`. Stop at the first hit unless `mode == "all"`.
   4. Register hits with `store.add_findings([{x, y, by: "scout"}])`, and record `detection_error_m` (the distance from the hit to the truth target) in `store.metrics`.
   5. Return `{ok, findings, waypoints_flown, area}`.
   6. Respect `ctx.cancelled()` between waypoints.
   7. If nothing is found → `{ok: true, findings: []}` (the supervisor decides what to do next).
3. **Dashboard (M3):** draw the planned lawnmower path (put `search_path` in the step result) and the scout's trail.

**Tests:** search with the target inside the zone finds it within the noise; with `miss_p=1` it returns nothing; cancel mid-search stops it. **Done when** green and `t4_search_field_a.yaml` runs on mocks.

### WP-M8: AI planner on the farmland story

1. **`planner.RULES`:** rewrite for the recovery mission:
   - The roles: the scout searches and marks; the rover inspects, picks up and carries.
   - The order: `drone_search` → `drone_mark` → `rover_goto` → `drone_land` → `rover_search` → `rover_inspect` → `request_authorization` (when hazardous) → the pick sequence → `rover_goto $zone.disposal` (hostile) or `$home` (own drone) → `report`.
   - Never pick up without inspecting; decoys are reported, not picked up; one item per trip.
   - Docking rules only when `DOCK_MODE=1`.
2. **`fallback_plans.demo_plan`:** return the T3 recovery plan, with `drone_search` on the zone named in the text (`field_a` by default).
3. **`supervisor.py`:** the RuleSupervisor learns three cases:
   - A failed `request_authorization` → `done` (skip the item).
   - `not_a_drone` → `done`.
   - An empty `drone_search` → `ask_human` ("Nothing found in field A. Search field B?").
4. **`cli.py` default mission:** "A hostile drone crashed in field A. Find it and move it to the disposal zone."

**Tests:** recorded-LLM tests in `test_planning.py` updated to the new vocabulary; the fallback plan validates; `pytest -m live` once with a real key. **Done when** green and a typed mission produces a sensible plan on the dashboard.

---

## 6. WP-F1: floor bring-up checklist (with a mentor; write the results into `arena.json` and the server docstrings)

1. **Network:** all laptops on the team hotspot; `curl <win-ip>:8000/health` and `curl localhost:8001/health` from the mothership Mac.
2. **Lighthouse:** geometry written to **both** Crazyflies. Put the target on the floor at the cage corners and read `/pose?cf=target` → that gives the arena `bounds` and `drone_cage`.
3. **Zones:** use the target Crazyflie as a measuring wand. Hold it over each corner of `field_a`, `field_b`, `base` and `disposal`, then write the numbers into `arena.json`. Set `"calibrated": true`.
4. **Home:** tape a cross in `base`; place the EP on it facing +x; measure the cross with the wand → `home`.
5. **Rover sign checks:** `/drive?x=0&y=0.3&z=0` must move **left** (as seen from behind the rover); `/drive?z=45` must turn **counter-clockwise** from above. If not, fix it in `robot_server.py`, not in the mothership.
6. **Odometry check:** drive 2 m forward and 1 m left via the mothership's `rover_goto`, then compare with a wand measurement. Note the error, which goes into the pitch's error budget.
7. **Camera grip line:** put the cradle (target CF in an open-top cup) in the open gripper, take `/snapshot`, read the pixel row of the cup's base → `camera.grip_line_row`. Also note the image width/height.
8. **Arm presets:** find the `pick`, `carry` and `home` x/y values for `robotic_arm.moveto` and put them in `robot_server.py`.
9. **Scout:** takeoff/goto/land at `search_z` and `mark_z`; check that the battery lasts one T4 search (time it).
10. **The target cradle:** an open-top cup or cardboard holder with a handle the gripper can close on; red tape = hostile, blue = own. Lighthouse must still see the deck: check that `/pose?cf=target` is stable while it's in the cradle.

---

## 7. Rules for agents working on this repo

1. Do **only** your WP. If you need a change in another WP's area, write it under "Follow-ups" in your final message instead of doing it.
2. Never import `cflib`, `robomaster` or ROS anything under `manager/`. Robot code lives in the two servers.
3. **Don't rename or remove `/state` fields.** Adding fields is fine. The dashboard depends on them.
4. Safety checks go in `tools.run_action` or `safety.py`, never only in a prompt. `POST /stop` must work from every state and never call the LLM.
5. Every new tool: an `@action(...)` decorator, a docstring written for the LLM (units, frame, return value), a dict return (never raise into the executor), and a test in `tests/test_tools.py`.
6. Mock first: every feature must run with `ROVER=mock DRONE=mock` and no `GEMINI_API_KEY`.
7. Keep the code style: short module docstrings that explain *why*, no comments that restate the code, type hints on public functions.
8. Finish with: `pytest` output (pass count), what you changed (files), and how a human can see it working (one command or click path).

---

## 8. Risks

| Risk | Fallback |
| --- | --- |
| EP Wi-Fi to the team network is flaky | `conn_type="rndis"` (USB cable) between the EP and the Windows laptop |
| Grip on the real drone fails | T2 + inspect + "marked for disposal team" is still a full story; show the grip on mocks |
| Odometry drift is too large to reach the target | Shorter drives; `rover_search` + camera approach correct the error; or start the rover closer |
| Target Crazyflie pose jumps when gripped | Freeze the truth for the detector after the first detection (`Target.source` becomes `fixed` with the last good pose) |
| Gemini slow or down | Scenario plans (T2–T4) need no LLM; the AI is T5 only |
| Drone flight not allowed when we film | Real rover + mock drone (`DRONE=mock`), said openly in the video |
