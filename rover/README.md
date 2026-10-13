# RoboMaster EP rover control

Two ways to drive the DJI RoboMaster EP, both over the robot's plaintext SDK (no `robomaster` package):

- **`robot_server.py`**: the rover **HTTP API** on port 8000. The mission manager, curl and a browser teleop page all use it.
- **`control1.py`**: the original stand-alone browser teleop (EP in AP mode). See [Stand-alone teleop](#stand-alone-teleop-control1py) below.

The EP accepts **one** SDK controller at a time, so run only one of them.

## Rover API server (`robot_server.py`)

### Set up

1. Put the EP in **STA mode** so it joins the team hotspot (RoboMaster app → connection → router mode, scan the QR code). Note the IP the hotspot gives it.
2. Join the laptop that will run the server (Mac or Linux) to the same hotspot, then check `ping <ep-ip>`.
3. Install: `pip install -r rover/requirements.txt` (PyAV + Pillow, Python 3.8+).

### Run

```bash
ROBOT_HOST=<ep-ip> python rover/robot_server.py     # real robot
MOCK=1 python rover/robot_server.py                  # fake EP, no hardware (no PyAV needed)
```

It prints the URL to use, e.g. `http://10.0.0.12:8000`. Open that in a browser for the keyboard/camera page (W A S D Q E drive, Space stop, I drives 1 m, O/C gripper, ↑/↓ arm). Point the mothership at it with `ROVER=http://<that-ip>:8000`. **Ctrl-C** (or `kill`) stops the robot and quits.

| Env var | Default | Meaning |
| --- | --- | --- |
| `ROBOT_HOST` | `192.168.2.1` | EP IP address on the hotspot |
| `PORT` | `8000` | HTTP port (`HTTP_HOST`, default `0.0.0.0`, sets the interface) |
| `MOCK` | off | `1` = fake EP and a grey camera image |
| `SDK_Y_SIGN` / `SDK_Z_SIGN` | `-1` / `-1` | Converts API left/CCW to DJI's documented right/clockwise. Flip one if the floor check fails |
| `GRIP_POWER` | `1` | Gripper force 1–4 |

### Test with curl

Every endpoint is a `GET` that returns JSON `{"ok": true|false, ...}`. API frame: **x forward, y left, z / yaw counter-clockwise**, in metres and degrees.

```bash
R=http://localhost:8000
curl $R/health                          # battery, camera, last action
curl "$R/drive?x=0.3"                   # blocking: 0.3 m forward, returns what it moved
curl "$R/drive?y=0.3"                   # 0.3 m LEFT
curl "$R/drive?z=45"                    # turn 45° LEFT (counter-clockwise from above)
curl "$R/speed?x=0.2&t=1"               # 0.2 m/s forward, stops by itself after 1 s
curl $R/stop                            # stop now (also interrupts a /drive)
curl $R/pose                            # odometry since start; /pose?reset=1 zeroes it
curl "$R/grip?action=open"              # or close
curl "$R/arm?preset=pick"               # pick | carry | home
curl "$R/arm?x=10&y=-10"                # relative nudge, mm (x forward, y up)
curl $R/arm                             # read the arm position
curl -o shot.jpg $R/snapshot            # full-resolution camera JPEG
```

Limits enforced by the server: `/speed` ≤ 0.5 m/s and 90 °/s, `t` ≤ 3 s; `/drive` ≤ 2 m per axis and 360°; arm nudges ≤ 100 mm. HTTP `503` means the server is up but the EP didn't answer; a refused command (e.g. arm out of reach) is `200` with `"ok": false` and the robot's reply in `error`.

### First run on the real EP (floor checks)

1. **Signs:** `curl "$R/drive?y=0.3"` must move **left** seen from behind; `curl "$R/drive?z=45"` must turn **counter-clockwise** seen from above. If one is wrong, restart with `SDK_Y_SIGN=1` or `SDK_Z_SIGN=1`, then write the result in the docstring of `robot_server.py`.
2. **Stop during a drive:** `curl "$R/drive?x=1"` and `curl $R/stop` from a second terminal. The robot should stop at once. (`/stop` sends zero speed; that this cancels a `chassis move` on the real EP is not verified yet.)
3. **Arm presets:** nudge the arm with `/arm?x=..&y=..` to good pick / carry / home poses, read each with `curl $R/arm`, and put the numbers in `ARM_PRESETS` in `robot_server.py`. The current values are placeholders.
4. **Odometry:** drive 1 m and compare `/pose` with a tape measure.

## Stand-alone teleop (`control1.py`)

Browser teleop for a DJI RoboMaster EP: drive, camera, gripper, and arm. The script talks the robot’s plaintext SDK over Wi‑Fi (no official `robomaster` Python package required).

## What you need

- A DJI RoboMaster EP (chassis + camera; gripper/arm optional)
- Python **3.8+** on Mac, Windows, or Linux
- The PC joined to the robot’s Wi‑Fi (direct AP mode)

## 1. Connect the PC to the robot

1. Power on the EP.
2. On the smart controller / robot, use **Wi‑Fi direct (AP)** mode so the robot creates a network.
3. On the PC, join that Wi‑Fi network (SSID looks like the robot’s).
4. Confirm the robot IP is the default used by this script: **`192.168.2.1`**.

Quick check from a terminal:

```bash
ping 192.168.2.1
```

You should get replies. If not, you are not on the robot’s network (or the IP is different).

> **Note:** While joined to the robot AP, the PC usually has **no internet**. That is expected. USB control is not used by this script.

If your robot is on another IP (router / STA mode), edit `HOST` at the top of `control1.py`.

## 2. Install Python packages

From this folder (`marc/rover`):

```bash
cd marc/rover
python3 -m venv .venv
```

**Mac / Linux**

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**Windows** (Command Prompt or PowerShell)

```bat
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Or without a venv:

```bash
python3 -m pip install -r requirements.txt
```

Dependencies:

| Package | Why |
| --- | --- |
| `av` (PyAV) | Decode the robot’s H.264 camera stream |
| `Pillow` | Encode JPEG frames for the browser |

System libraries: PyAV needs FFmpeg. On most Pips wheels it is bundled; if `import av` fails, install FFmpeg for your OS ([ffmpeg.org](https://ffmpeg.org/)) and try again.

## 3. Run

With the venv active and the PC on the robot Wi‑Fi:

```bash
python control1.py
```

Or:

```bash
python3 control1.py
```

You should see something like:

```text
Connecting to DJI RoboMaster...
command -> ok;
Successfully connected!
...
Camera window: http://127.0.0.1:8765
```

A browser tab opens for the camera and controls. Click the page so keyboard focus is there. **Ctrl‑C** in the terminal stops the script.

After code changes, stop the script, start it again, and refresh the browser page.

## Controls

| Key / button | Action |
| --- | --- |
| **W** / **S** | Forward / back |
| **A** / **D** | Strafe left / right |
| **Q** / **E** | Rotate left / right (yaw) |
| **I** | Drive **1 m** forward (one-shot) |
| **Space** | Stop |
| **O** / Open | Open gripper |
| **C** / Close | Close gripper |
| **↑** / Arm up | Arm relative move forward + up (mm) |
| **↓** / Arm down | Arm relative move back + down (mm) |

Arm moves use millimetres (1 mm accuracy), matching the [mechanical arm API](https://robomaster-dev.readthedocs.io/en/latest/python/robotic_arm.html). Gripper force is set to the lowest level (1).

## Ports used

| Port | Protocol | Role |
| --- | --- | --- |
| `192.168.2.1:40923` | TCP | SDK commands |
| `192.168.2.1:40921` | TCP | H.264 video after `stream on` |
| `127.0.0.1:8765` | HTTP | Local browser UI (tries 8765–8784 if busy) |

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| `Connection refused` / timeout on connect | Join the robot Wi‑Fi; `ping 192.168.2.1`; battery on; SDK not already held by another app |
| Camera stuck / no video | Check terminal for `stream on -> ok`; refresh the browser; only one script instance |
| Gripper / arm `fail` | Arm may be at end of reach; try the other arrow; keep fingers clear; charge the battery |
| `ModuleNotFoundError: av` or `PIL` | Activate the venv and run `pip install -r requirements.txt` again |
| Browser does not open | Open `http://127.0.0.1:8765` manually |
| Ctrl‑C crashes with segfault | Known quirk on some Macs; robot should already be stopped; power‑cycle if needed |

## Files

```text
marc/rover/
├── robot_server.py   # Rover HTTP API (:8000) + teleop page; MOCK=1 fake EP
├── teleop.html       # Keyboard/camera page served by robot_server.py at /
├── control1.py       # Stand-alone browser teleop
├── requirements.txt  # pip dependencies
└── README.md         # This file
```
