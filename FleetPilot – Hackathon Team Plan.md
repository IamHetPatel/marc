# FleetPilot – Hackathon Team Plan

Oct 10, 2026 · @Het Patel

## Update Sat 11:00: what we got and how we win

We have a **RoboMaster EP** (wheels, camera, gripper arm) and **one Crazyflie drone**, and 10 people. So we build the full version: the drone finds the object, the EP drives there, checks it with its camera and **picks it up**. No ROS on the robots: the EP uses its Python SDK, the drone uses `cflib`.

**How the jury scores** (5 minutes per team, 4 criteria, equal weight, "biggest progress wins"):

| Criterion | What they want | What we do |
| --- | --- | --- |
| Progress | Where we started Sat 11:00, where we ended | A photo or short clip every 2 hours in our Discord #progress channel |
| Working demo | Live or on video; a robot that moves beats a slide | Hardware goes back to the kiosk **Sun 16:00**, pitch is 17:00, so **our demo is a video**: final cut Sun 15:30 |
| Technical depth | What was hard, how we solved it, what we learned | Hard part = drone and rover on one shared map. Write down every problem and fix |
| Pitch | Problem, approach, result, next step | 5 minutes, rehearsed 3 times |

Jury: Prof. Frank Fitzek (ComNets / CeTI), Lisa Martha Hecker (SKR network), Stefan Queißer (OSTEC / KI-Netzwerk Dresden).

**The hard part: one shared map.** The track 2 mentor slides say robots must share one coordinate frame. Our options, best first:

1. Get a 2nd Crazyflie or a spare Lighthouse deck from the kiosk and tape it on the EP (motors off). The EP then knows its exact position in the drone's map.
2. Start the EP on a tape cross at a known spot, facing a known direction, and track it with its wheel odometry (drifts a little over a few metres).
3. ArUco markers + an overhead camera ("external tracking").

**Ask the mentors now:** Is Lighthouse set up in the drone cage? Can the EP drive inside the cage with the drone? Does our Crazyflie have a Flow deck or Multi-ranger? Can we borrow a 2nd Crazyflie or Lighthouse deck?

**Rules that affect us:** drones fly only in the cage / test zone, with a mentor present for every flight and field run. Wi-Fi: eduroam, guest login from Thomas. All hardware back at the kiosk by Sun 16:00. Lunch poll at 12:30: vote for the Crazyflie workshop.

**How the code talks**, so 10 people can work in parallel. Everything is HTTP + JSON, every position in metres in the drone's Lighthouse frame:

- **Rover server** (Windows laptop, port 8000): `/drive` `/speed` `/grip` `/arm` `/snapshot` `/stop` `/pose`
- **Drone server** (port 8001): `/takeoff` `/goto?x=&y=&z=` `/scan?x0=&y0=&x1=&y1=` `/land` `/pose`
- **AI manager** (port 8002): `/mission` `/approve` `/stop` `/state` (the dashboard reads `/state` every 0.5 s)

Everyone builds against fake robots first (`MOCK=1`), then switches to the real ones.

## The idea

We type or say one sentence, an AI splits the job between our robots, a human approves the plan on a live screen, and the robots do it and check their own work.

- **Example mission:** "Check the test zone for anything blocking the exit."
- **Who does what:** a small drone searches fast from above. A ground robot drives to the spot and its camera looks closely. The AI says what it found, where, with a photo.
- **Real-world use:** inspection in warehouses and factories, where different robots from different brands must work together.
- **Tracks:** Track 2 (Multi-Robot Collaboration), with Track 3 (Computer Vision & AI) mixed in.
- **Why the jury should care:** a human stays in control (approve, change, stop), which matches the hosts' research (CeTI, 5G Lab). Our dashboard runs over the venue's 5G.

## The demo (3 minutes, Sunday 17:00)

1. **Plan:** we say the mission. The AI shows its plan on screen ("drone searches, ground robot checks"). A teammate presses **Approve**.
2. **Run:** the drone flies over the area and flags a spot. The ground robot drives there. The camera + AI confirm: "cardboard box blocking the exit".
3. **Human changes the plan:** halfway through, we say "skip the left side, check the charging station instead". The plan updates live and the robots follow.

We also show 2 numbers: how many runs out of 10 worked, and how fast the robot reacts over 5G vs Wi-Fi.

The ground robot (Turtlebot / ROSbot) has no gripper, so it **inspects** and does not pick things up. If we get a RoboMaster EP, it can also grab.

## How it works

&#91;embedded content: FleetPilot · 4 layers: dashboard, AI manager, adapters, robots\]

The AI never needs to know which robot is real. If we lose a robot, we swap its adapter and the demo keeps running.

## Robots we need (plan from Friday night)

120 people in about 12–20 teams share the robots, so we plan for **1 ground robot** and add more only if we get them. Each level below is a complete demo on its own.

| Level | Robots we get | What we show |
| --- | --- | --- |
| 1 (must have) | 1 ground robot with camera | Mission → approve → robot drives and inspects → AI report |
| 2 (goal) | + 1 drone | Drone finds the spot, ground robot checks it |
| 3 (backup) | Only 1 real robot | 1 real robot + 1–2 robots in simulation on the same map, run by the same AI (we say this openly) |
| 4 (bonus) | 2 ground robots and/or more drones | Robots split the area between them |

**What to ask the mentors for, in this order:**

1. A ground robot (Turtlebot or ROSbot XL) that already has a map, navigation (Nav2) and **rosbridge** running.
2. 1–2 Crazyflie drones, and: "Is the Lighthouse tracking system set up in the test zone?"
3. "Can we run Gazebo simulation on one of your NUC PCs?" (for level 3)

## Who does what

Everyone uses AI (Claude etc.) to write code. Each person owns one piece and says "done" when the test in the last column works.

| Squad | People | Owns | Done when | Deadline |
| --- | --- | --- | --- | --- |
| Rover | 2 (one Windows laptop, Python 3.8) | `robot_server.py`: drive, gripper, arm, camera, odometry `/pose` | A laptop drives the EP 1 m, grabs a cup and returns a photo | Sat 14:00 |
| Drone | 2 | Drone server with `cflib`: Lighthouse position, takeoff / goto / land, scan pattern, "object below" detection | The drone flies a scan in the cage and reports where the object is | Sat 18:00 |
| Shared map + digital twin | 2 (incl. our RViz teammate) | Put EP and drone in one frame; show both live in RViz or on the dashboard map | The EP drives to a point the drone reported, within 20 cm | Sat 22:00 |
| AI manager | 2 (Het + 1) | Plan → human approves → assign tasks → check with camera → re-plan on failure (Gemini Robotics-ER 2) | A full mission runs on fake robots, then on real ones | Fake Sat 15:00, real Sun 11:00 |
| Dashboard | 1 | Mission box, Approve / Change / Stop, AI messages, camera feed, map | Shows the fake robots live | Sat 18:00 |
| Story & video | 1 | #progress log, test counts, problems + fixes list, slides, video edit, pitch script | Video final, slides done | Sun 15:30 |

If someone finishes early, they help the shared map squad: getting both robots on one map is the hardest part.

## Timeline and stop rules

| When | Goal | If it fails |
| --- | --- | --- |
| Sat 12:00 | Mentor answers (Lighthouse, cage, decks); interface list agreed; one shared GitHub repo | Pick shared-map option 2 |
| Sat 14:00 | Laptop drives the EP and grabs; drone hovers in the cage | Ask a mentor; free hands help the stuck squad |
| Sat 18:00 | Each squad's part works on its own; dashboard shows fake robots | Cut extras, keep the basics |
| Sat 22:00 | EP drives to a point the drone reported (shared map works) | Switch to option 2 (tape cross + odometry) |
| Sun 11:00 | Full mission with real robots, run by the AI | Show the AI on fake robots + the real robots step by step |
| Sun 13:30 | No new features. Record 5+ full runs on video | Use the best run |
| Sun 15:30 | Video cut final, slides done | — |
| Sun 16:00 | Return all hardware to the kiosk | — |
| Sun 17:00 | 5-minute pitch | — |

Sleep a few hours on Saturday night. A rested team debugs faster.

## Setup per laptop

Nobody needs to install ROS on their laptop. The robot runs ROS; our laptops talk to it over Wi-Fi with **rosbridge** (`pip install roslibpy`).

- **Everyone:** Python 3.10+, `pip install google-genai requests roslibpy`, a Gemini API key, one shared GitHub repo.
- **Drone person (Mac):** `brew install libusb` then `pip install cflib`. On Windows, the Crazyradio needs the Zadig driver.
- **ROS teammate:** ROS 2 on Ubuntu or a VM. Ask the mentors which ROS 2 version the robot uses.
- **If we switch to the RoboMaster EP:** a Windows laptop with Python 3.8 and `pip install robomaster` (it does not work well on new Macs).

Starter code from Het: `fleet_agent.py` (AI + rosbridge), `fleet.py` (drones + ground robot), `robot_server.py` and `brain.py` (RoboMaster backup). Each has a `MOCK=1` mode for working without a robot.

**Links:** [Event page](https://mobile-robotics.eu/) · [cflib install](https://bitcraze.io/documentation/repository/crazyflie-lib-python/master/installation/install) · [Gemini Robotics-ER 2](https://ai.google.dev/gemini-api/docs/models/gemini-robotics-er-2-preview) · [RoboMaster SDK install](https://robomaster-dev.readthedocs.io/en/latest/python_sdk/installs.html)
