# RoboMaster EP rover control

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
├── control1.py       # Main script
├── requirements.txt  # pip dependencies
└── README.md         # This file
```
