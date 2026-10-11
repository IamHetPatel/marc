# Convergence plan: one map, every sensor on it, a demo that runs without luck

**For:** the whole team and the coding agents working with them. Written Sunday 11 Oct, 10:30. Where this disagrees
with an older plan, this one wins. Older plans keep the details: [integration-plan.md](integration-plan.md) (how the
pieces run), [mothership-plan.md](mothership-plan.md) (contracts), [ai-layer-plan.md](ai-layer-plan.md) (LLM layer).

**Clock (from the Team Plan):** feature freeze **13:30** · video final 15:30 · hardware back **16:00** · pitch 17:00.
Everything in §4 is sized to fit before 13:30. §7 is for after the hackathon.

---

## 1. Where we really are

| Piece | State |
| --- | --- |
| Scout Crazyflie + drone server | Flies in the net on Lighthouse, cm-accurate, goes back to its pad. **Works.** (The scout's radio address was changed locally to `…E761` and not committed; the comment beside it still says `04`.) |
| Rover + rover server | Driven by hand from the dashboard. Its position is wheel counting from a guessed start, so "go to the drone's x, y" lands somewhere else. **The blocker.** |
| Mothership + dashboard | Plans, approval, safety, scenarios, report: all tested in simulation (389 tests). |
| Railing camera (4K, on the balcony, looking down at an angle) | Python code exists (`manager/overhead.py`: ArUco floor tags, pixel → metres, rover tag, Gemini pointing). The tags are on the carpet corners, but **their positions were never measured**, so the camera is not calibrated to our map. |
| Browser camera tracker (the "Camera tracking codex prompt") | **Not in any branch.** It is a separate browser app with its own click-calibration in its own coordinate frame. |
| Magenta ball on the rover's mast | Mounted. Nothing reads it yet. |
| Rover arm camera | Streams to the dashboard. Every use of it in a mission goes through Gemini. |
| Gemini | The robotics model's free tier (20 calls a day) is used up; the configured backups no longer exist. **Every "seeing" step depends on it today**, which is why no full scenario has run on the floor. |

**Root cause in one line:** each piece measures in its own frame (Lighthouse, wheel counts, camera pixels, browser clicks), and
all of the "seeing" depends on one rate-limited model.

---

## 2. The idea that ties it together

> **Lighthouse is the map. Everything that moves or is found gets a position on that map, from the best sensor for
> the job. The AI decides what to do, a person approves, and the robots do it.**

| Sensor | Answers | How it gets onto the map | Expected accuracy | If it fails |
| --- | --- | --- | --- | --- |
| Scout Crazyflie (Lighthouse deck) | Where is the scout? | Directly (Lighthouse) | ~1–2 cm | Land; the mission continues without marking |
| **Second Crazyflie riding at the rear of the rover** | Where is the rover, and which way is it facing? | Directly, minus the measured offset (`ROVER_RIDER=target`) | ~2–3 cm | Anchor from the scout + wheel counting |
| **Railing camera** | Where is the wreck? (and a cross-check of the rover) | ArUco tags on the carpet corners, **measured with a Crazyflie as a wand**, give pixel → map (homography) | ~3–5 cm near, worse far: **measure it** | Scenario with measured fixed coordinates |
| Magenta ball | Where is the rover, as seen from above? | Same homography, corrected for the ball's height (parallax) | ~5 cm, position only | Ignore it; the riding Crazyflie is primary |
| Rover arm camera | Is the object centred, in reach, and gripped? | Not needed: works in image pixels against the calibrated "grip line" | n/a | Scripted approach (fixed forward move) |
| Wheel odometry | Fills the gaps between position fixes | Carried forward from the last fix | drifts ~5–30 % | — |

**The story for the jury (honest):** "In a real field a camera drone would find the wreck from above. Our Crazyflies
are too small to carry one, so a fixed camera above the net plays the drone's eye. The scout still flies to mark the
spot, the rover still navigates on the same Lighthouse map, and a person still approves before anything hazardous is
touched." That uses every piece of hardware you have, and each has a real job.

---

## 3. Decisions on the brainstormed ideas

| Idea | Decision | Why |
| --- | --- | --- |
| Railing camera instead of a drone camera | **Yes: it becomes the "eye in the sky"** that finds the wreck | Drones without cameras can't detect anything; the camera can, and is fixed and repeatable |
| Second Crazyflie in a casing on the rover | **Yes: primary rover position** (code ready) | Same frame as the scout by construction; gives heading too; no drift |
| Magenta ball | **Yes, as cross-check and backup** rover position from the camera | Unique colour, cheap to detect; one ball gives position, not heading |
| Browser tracker (text file) | **Don't integrate it as an app. Port its two best ideas into `overhead.py`** (colour blob tracking with size/proximity checks; four-click calibration as a fallback when tags aren't visible) | Positions must reach the planner and the rover inside the mothership, on the Lighthouse map. A browser-local frame can't steer the rover |
| COCO-SSD object detection | **No** | It has no "drone" class, needs a CDN, and is less reliable than colour-coded targets for a demo |
| Colour-coded targets | **Yes: the backbone of perception today** | Deterministic, offline, fast, no quota. Detects the wreck from above and centres it for the gripper |
| Gemini robotics model for vision | **Optional extra, not the backbone** | 20 calls a day is about one floor run. Keep it for one showcase run or the narration |
| More LLM backends (more Gemini keys, Grok) | **Yes, for planning text only** | `gemini-3.8-flash` works with your key today (2.6 s); a key list and a second provider are small changes |
| ROS layer (teammate) | **Keep it a side track** | It doesn't fix the frame problem or the Wi-Fi link; if it holds a stable EP link it can sit behind the same `:8000` contract |
| "Perfect grasp point" | **Centroid of the colour blob on the grip line** (what `rover_approach` already does, without the LLM) | Real grasp-pose estimation is a post-hackathon topic (§7) |

### Colour code (pick colours the scene does not already contain)

The floor around the carpet is **red-brown tile**, the rover has **red cables** and a **magenta** ball, and the carpet is
grey. So avoid red and magenta for targets:

| Thing | Colour | Why |
| --- | --- | --- |
| Hostile / hazardous drone | **Yellow** (yellow-black hazard tape on the dummy wreck) | Reads as "danger" to the jury; nothing else in the scene is yellow |
| Our own crashed drone | **Blue** | Distinct from everything else |
| Decoy (false alarm) | **Green** (e.g. a green bottle or box) | Distinct |
| Rover marker | **Magenta** (already mounted) | Unique |

Detection only looks **inside the arena on the carpet** (from the homography), and ignores anything within ~0.35 m of
the rover (its own cables and ball).

---

## 4. Work packages for today (target: everything working by 13:00, freeze 13:30)

Run W1, W2 and W3 in parallel. Each has a check that says it's done.

### W1 · Railing camera onto the Lighthouse map (camera pair, ~45 min)

1. `python camera/stream_webcam.py` on the laptop the camera is plugged into (default 1080p is enough).
2. For **each ArUco tag on the carpet**: hold the landed scout (or the target Crazyflie) exactly on the tag's centre,
   read `curl localhost:8001/pose`, and write `x, y` into `manager/arena.floor.json` → `overhead.floor_tags` (by tag id).
3. Measure the camera: lens height above the floor (`camera_height_m`, roughly the balcony rail height) and the floor point
   straight below it (`camera_floor_xy`, measured with the Crazyflie under the camera or estimated from the net).
4. Check: `python -m manager.overhead check http://<laptop>:8003 --save check.jpg`. The yellow outline must sit on the
   carpet edge, and the reprojection error should be a few cm.
5. Put the scout on the floor at 4 spots and compare where the camera says the tag is with `/pose` (needs a printed tag on
   the scout, or just read the pixel). **Write the error down**: it's a pitch number.

**Done when:** `check` reports the tags, the outline matches the carpet, and you have a measured error.

### W2 · Local colour vision, no LLM needed (Het + an agent, ~60–75 min)

A new module `manager/colorvision.py`, plugged in where Gemini is today. Every interface already exists:

| Where | Today | With colour vision |
| --- | --- | --- |
| Railing camera: "where is the wreck?" (`OverheadCamera.locate`) | Gemini points in the frame | HSV blobs of the target colour, inside the arena mask, biggest plausible blob → homography → map x, y |
| Railing camera: rover marker (new) | — | Magenta blob → map x, y at the ball's height (parallax) → shown on the map, optional position fix |
| Rover camera: `perception.point` (search and approach) | Gemini points | Blob centroid (col, row) of the target colour |
| Rover camera: `perception.inspect` | Gemini describes | Colour → category (yellow = hostile, hazardous; blue = own; green = not a drone), confidence from blob size |
| Rover camera: `perception.holding` | Gemini looks at the gripper | Target-colour blob covering the gripper region at the bottom of the frame |

- Select it with `PERCEPTION=color` and `OVERHEAD_LOCATE=color`; the colour table lives in the arena file so it can be tuned
  on the floor without code changes.
- Port from the browser tracker: HSV thresholds with a hue tolerance, 4-connected components, minimum area, maximum size
  (no "largest red region" mistakes), proximity and size consistency between frames for the rover marker.
- **Tests on synthetic frames** (like `overhead_mock.py` already renders): a blob is found where it was drawn; the wrong
  colour, a too-small blob and a too-big blob are rejected; a blob next to the rover is ignored; the full
  `floor_recover_hostile` scenario runs on mocks with `PERCEPTION=color`.

**Done when:** a yellow box on the carpet appears as a finding on the dashboard map at the right spot, with no API key set.

### W3 · Rover on the map (rover squad, ~45 min)

1. Fit the second Crazyflie in its casing at the rear (flat, nose forward, deck sees the ceiling; the arm must not shade it).
2. Measure its centre relative to the rover's centre and enter it in `manager/arena.floor.json` as
   `"plate_offset": {"x": -0.20, "y": 0.0, "yaw": 0}`. **Behind the rover is a negative x.**
3. Drone server with `TARGET_URI=<its radio address>`; everything with `ROVER_RIDER=target .venv/bin/python start.py floor`.
4. The Robots panel must say "Lighthouse, from the Crazyflie it carries".
5. Manual "Go to x, y" to 3 tape marks; **measure the error** (target: under 10 cm).

**Done when:** the rover stops within ~10 cm of three marked points, three times in a row.

### W4 · Drone (drone squad, ~20 min)

Commit the scout address with a corrected comment; measure the launch pad → `launch_pad` in the floor arena; test mark
height 0.4 m over the carpet; check battery for 3 consecutive runs.

### W5 · LLM backup (optional, ~30 min, only after W2)

Planner and supervisor on `gemini-3.8-flash` by default (`MODEL=gemini-3.8-flash` already works); accept a comma-separated
`GEMINI_API_KEYS` and move to the next key on a quota error; optionally xAI Grok through its OpenAI-compatible API for text.
Vision never depends on it after W2.

---

## 5. Floor test ladder (12:00–13:30): each rung must pass before the next

| # | Test | Pass | Write down |
| --- | --- | --- | --- |
| 1 | Scout takeoff → "Go to" a tape mark → Back to pad | Within 5 cm, lands on the pad | error |
| 2 | Rover (riding Crazyflie) "Go to" 3 tape marks | Within 10 cm each | errors |
| 3 | Camera finds the yellow wreck at 5 spots | Within 10 cm of the wand reading | errors, mean |
| 4 | `floor_mark_and_drive` scenario | Scout marks, rover stops ~0.6 m short, facing it | runs / tries |
| 5 | Rover approach + grip on the yellow wreck, by hand ("Close in" then "Grip close") | Holding it | successes / 10 |
| 6 | Full recovery (camera finds → scout marks → rover approaches → you authorize → grip → disposal) | Mission complete + report | successes / tries, duration |
| 7 | False alarm with a green object | Rover reports "not a drone" and leaves it | yes/no |

Record **every** run of rungs 4–7 on video. The numbers go straight onto the "technical depth" slide.

**Rule for 13:30:** whatever rung you reached is the demo. If rung 6 isn't reliable, the video shows rungs 4 + 5 and the
dashboard shows rung 6 on the simulator, said openly.

---

## 6. Risks and fallbacks

| Risk | Fallback |
| --- | --- |
| Camera calibration doesn't line up | Floor scenarios with the wreck's measured coordinates (`floor_*`, lines marked `WRECK`) |
| Riding Crazyflie loses tracking under the arm | Anchor from the scout behind the rover before each run; keep drives short |
| Colour detection confused by lighting | Tune the HSV table in the arena file; tape bigger patches; mask to the carpet |
| Gripper misses | Demo inspect + "marked for the disposal team"; show the grip on the simulator |
| EP Wi-Fi drops | Phone hotspot close to the net, `ROVER_VIDEO=0`, full battery |
| Gemini out of quota | Nothing breaks after W2: colour vision + scripted plans |

---

## 7. After the hackathon: from demo to product

1. **Real eye in the sky:** a drone with a camera (AI deck or a companion computer), so detection moves from the fixed camera to the scout.
2. **Outdoors:** Lighthouse → RTK-GPS for both robots; the map code doesn't care which.
3. **Detection:** a model trained on images of downed drones and debris (YOLO-class, on-device), replacing colour codes.
4. **Grasping:** object pose from the arm camera (depth or stereo) and grasp-point selection, instead of the grip-line heuristic.
5. **Integration:** ROS 2 bridge (the teammate's track) for SLAM and RViz; multiple rovers; 5G for range.
6. **Safety case:** the human approval gate, geofences and the audit report become the product's compliance story.
