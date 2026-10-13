# Running MARC on the robots

## What you need

| Item | Role |
| --- | --- |
| DJI RoboMaster EP with the robotic arm and gripper | The rover: drives, photographs, picks up |
| 2 × Crazyflie 2.x, each with a Lighthouse deck | One flies (the scout); one rides on the rover with its motors off and gives the rover's position |
| Lighthouse base stations (2 or more) | The tracking system that defines the shared map |
| Crazyradio (PA or 2.0) | The laptop's radio link to both Crazyflies |
| One laptop (macOS or Linux, Python 3.10+) | Runs all three servers and the dashboard. On macOS the radio needs `brew install libusb` |

## One-time setup

1. **Install:** `python -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. **Lighthouse:** set up the base stations and write their geometry to each Crazyflie with `cfclient`
   (Lighthouse → Manage geometry). The geometry used on our floor is in [drone/calibration.yaml](../drone/calibration.yaml).
3. **Crazyflie addresses:** `python drone/scan_drones.py` lists the radios in range. Set `SCOUT_URI` (the flying one)
   and `TARGET_URI` (the riding one) if they differ from the defaults in `drone/drone_server.py`.
4. **Measure the field** in the Lighthouse frame and enter it in [manager/arena.floor.json](../manager/arena.floor.json):
   the field bounds, the flight cage (where tracking is good), the zones and the scout's launch pad. Put a landed
   Crazyflie on each spot and read `curl "localhost:8001/pose"`. The flight cage must also go into `DEFAULT_CAGE` in
   `drone/drone_server.py`; a test fails if the two differ.
5. **Mount the riding Crazyflie** on the rover's back-right corner, deck facing up with a clear view of the base
   stations, nose roughly towards the rover's front.

## Start

The EP is most reliable in **direct Wi-Fi mode**: the laptop joins the EP's own network, and the EP is always at
192.168.2.1.

```bash
.venv/bin/python start.py floor --rover 192.168.2.1            # add --no-ai to run without Gemini
```

The startup summary shows the EP, the scout and the AI status; the dashboard is at http://localhost:8002/.

The laptop is then off any other network, so run everything on it with the Crazyradio plugged in. A second network
(phone USB tethering or USB Ethernet) brings back internet access and other laptops. If the Crazyflies' radio drops
while the EP streams video, switch the EP's Wi-Fi to 5.8 GHz in the RoboMaster app (the Crazyradio uses 2.48 GHz).

Other options: `--rover auto` finds an EP on the same network (router mode), `--drone mock` or `--rover mock`
simulates one robot, and `start.py sim` runs both robot servers in mock mode on the floor map.

## Calibrate on the floor

1. **Rider check.** The rover panel should say *Lighthouse, from the Crazyflie it carries*. Push the rover by hand: its
   marker follows without jumping. A faded rover or a growing glitch count means the rider cannot see the base
   stations.
2. **Calibrate rider.** Hands-on control → **Calibrate rider**, with 40 cm free around the rover. It drives 25 cm
   forward, turns half a circle and back, and stores the measured mount in `runs/rider_mount.json`. Afterwards
   **Fwd 0.3 m** should move the marker nose first, and **Turn left 45°** should turn it in place.
3. **Gripper reach.** Measure from the centre of the four wheels to where the gripper holds an object with the arm in
   *pick*, and set `rover.grip_reach_m` in `manager/arena.floor.json`.

## Floor checklist

Run these in order; each one depends on the one before.

| Step | Do | Expect |
| --- | --- | --- |
| 1 | Switch **Safety pilot** on, then scout **Take off**, **Mark x,y**, **Back to pad** | It hovers over the spot and lands on its pad; the drone server prints how far off it landed |
| 2 | Rover **Go to x,y** | It stops within about 10 cm |
| 3 | Scenario **Search field A** | The scout searches, marks the find and returns to its pad |
| 4 | Scenario **Mark and drive** | The rover drives to the marked spot |
| 5 | Scenario **Mark and pick** | The rover grips the object and brings it back to where it started |
| 6 | Scenario **Hostile drone in field A** | Inspect, your authorization, pick-up, delivery to the disposal zone |

The floor scenarios place the object at a fixed spot (x −0.2, y 0.0); change it in the scenario file. To place the
object, hover the scout over the spot with **Mark x,y** and put the object under it.

## When something goes wrong

| Symptom | Likely cause and fix |
| --- | --- |
| "EP not connected", or commands time out | Laptop not on the EP's Wi-Fi, EP battery low, or the RoboMaster app still holds the SDK: close it |
| "Lighthouse tracking lost" on a Crazyflie | Base stations off or blocked, deck not detected at boot (power-cycle), or no geometry written |
| The rover is drawn faded and stops | The riding Crazyflie lost tracking; the rover waits up to 4 s for it to come back |
| Drone actions are refused | The **Safety pilot** switch is off, the battery is below 3.3 V, or the target is outside the flight cage |
| The rover drives in a curve to its goal | The mount's heading is off: run **Calibrate rider** again |

## Optional: overhead camera

A webcam above the field can find objects and track a marker on the rover
([manager/overhead.py](../manager/overhead.py)). Print the floor tags with
`python -m manager.overhead tags overhead_tags/`, tape them at measured spots, enter those spots under `overhead` in
the arena file, and start with `--camera 0` (or run `camera/stream_webcam.py` on another laptop and pass its URL).
`python -m manager.overhead check <camera> --save check.jpg` shows whether the calibration fits.
