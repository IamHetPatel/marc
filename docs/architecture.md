# Architecture

MARC is three small HTTP servers and a browser dashboard. Only the mission manager talks to the robot servers,
and every position is in metres in one frame: the Lighthouse tracking frame the scout flies in.

## Who talks to whom

```mermaid
flowchart LR
  OP(["Operator"]) --> DASH["Dashboard<br/>plain JS, SVG map"]
  DASH -- "GET /state twice a second<br/>POST mission, approve, change, stop" --> API

  subgraph MS ["Mission manager · FastAPI · :8002"]
    direction TB
    API["REST API"] --> CTRL["Controller<br/>mission lifecycle, watchdog"]
    CTRL --> PLAN["Planner<br/>mission text to steps, validator"]
    CTRL --> EXE["Executor<br/>one step at a time, re-plan budget"]
    EXE --> SUP["Supervisor<br/>AI or rules"]
    EXE --> TOOLS["Robot actions<br/>run_action()"]
    TOOLS --> SAFE["Safety gates"]
    TOOLS --> LOC["Localization"]
    TOOLS --> PER["Perception"]
    TOOLS --> DET["Scout detector"]
    CTRL --> STORE[("Store: mission, plan,<br/>findings, photos")]
  end

  TOOLS -- "/speed /drive /grip /arm /snapshot /pose" --> RS["Rover server :8000"]
  TOOLS -- "/takeoff /goto /scan /dock /land /pose" --> DS["Drone server :8001"]
  LOC -- "/pose?cf=target, 10 Hz" --> DS
  RS -- "TCP 40923, plain-text SDK<br/>+ H.264 video" --> EP["RoboMaster EP"]
  DS -- "Crazyradio 2.4 GHz" --> SCOUT["Scout Crazyflie"]
  DS -- "Crazyradio" --> RIDER["Crazyflie riding on the rover"]
  LH{{"Lighthouse base stations"}} -. "IR sweeps" .-> SCOUT & RIDER
  PLAN & SUP & PER -. "optional" .-> GEM["Gemini API"]
```

| Piece | Code | Job |
| --- | --- | --- |
| Mission manager | [manager/](../manager/) | Plans, runs and supervises missions; enforces safety; serves the dashboard |
| Rover server | [rover/robot_server.py](../rover/robot_server.py) | Speaks the EP's plain-text SDK over TCP; keeps the link alive through Wi-Fi hiccups |
| Drone server | [drone/drone_server.py](../drone/drone_server.py) | Flies the scout with the Crazyflie high-level commander; reports both Crazyflies' Lighthouse poses |
| Camera streamer | [camera/stream_webcam.py](../camera/stream_webcam.py) | Serves an overhead webcam over HTTP for the optional overhead-camera module |

## A recovery mission, call by call

```mermaid
sequenceDiagram
  autonumber
  actor OP as Operator
  participant MS as Mission manager
  participant AI as Gemini (optional)
  participant DS as Drone server
  participant RS as Rover server

  OP->>MS: POST /mission "Recover the hostile drone in field A"
  alt API key set
    MS->>AI: mission + robot actions + field + rules
    AI-->>MS: steps
  else no key
    MS->>MS: built-in plan or scenario file
  end
  MS->>MS: validate the steps
  MS-->>OP: plan, waiting for approval
  OP->>MS: POST /approve

  Note over MS,DS: Scout
  MS->>DS: /takeoff, then /goto along the search lanes
  MS->>MS: detector reports finding f1
  MS->>MS: supervisor checkpoint
  MS->>DS: /goto over f1, then /dock back to the pad

  Note over MS,RS: Rover (position from the riding Crazyflie)
  loop until within 10 cm, then turn to face f1
    MS->>DS: /pose?cf=target
    MS->>RS: /speed
  end
  MS->>RS: /snapshot, then inspect: ours, hostile or not a drone
  MS-->>OP: f1 is hazardous. Authorize the pick-up?
  OP->>MS: POST /change "yes"
  MS->>RS: /arm pick, /grip open
  loop line the gripper up within 2 cm
    MS->>DS: /pose?cf=target
    MS->>RS: /speed
  end
  MS->>RS: /grip close, /arm carry, /snapshot to check the grip
  MS->>RS: /speed to the disposal zone, /grip open, /arm home
  MS->>MS: write runs/<mission>/report.yaml and report.html
  OP->>MS: POST /stop (any time)
  par
    MS->>RS: /stop
  and
    MS->>DS: /land
  end
```

## Mission states

```mermaid
stateDiagram-v2
  [*] --> idle
  idle --> planning: POST /mission
  idle --> awaiting_approval: POST /plan or a scenario
  planning --> awaiting_approval: plan validated
  planning --> failed: no valid plan
  awaiting_approval --> running: POST /approve
  running --> replanning: checkpoint, failed step or a human change
  replanning --> running: new remaining steps
  running --> done: last step succeeded
  running --> failed: abort, or the re-plan budget is spent
  running --> stopped: POST /stop or the watchdog
  done --> idle: POST /reset
  failed --> idle: POST /reset
  stopped --> idle: POST /reset
```

## How the plan adapts

After a checkpoint step (search, scan, inspect, a question to the operator) or any failure, the supervisor decides
what happens next: continue, re-plan the remaining steps, ask the operator, finish, or abort. With a Gemini key the
model decides and its answer is validated; without one, or when the answer is invalid, rules decide:

| Situation | Decision |
| --- | --- |
| The scout found nothing in field A | Ask the operator whether to search field B |
| The rover's camera says it is one of ours | Deliver it to base instead of the disposal zone |
| It is not a drone | Finish and leave it |
| It cannot be gripped | Drop the pick-up steps and report it |
| The operator does not authorize | Finish and leave it where it is |
| A step failed | Retry it once, then abort |
| A robot stopped answering | Abort |

## Where the rover is

```mermaid
flowchart LR
  A["Crazyflie on the rover<br/>Lighthouse position, 10 Hz"] --> B{"Plausible?<br/>on the map,<br/>no impossible jump"}
  B -- no --> X["Ignore and count the glitch<br/>(3 agreeing reads = it was really moved)"]
  B -- yes --> C["Mount offset<br/>Crazyflie pose to rover centre"]
  CAL["Calibrate rider:<br/>25 cm forward, half a turn"] -. "runs/rider_mount.json" .-> C
  C --> D{"Fresh,<br/>under 1 s?"}
  D -- no --> W["Rover stops and waits"]
  D -- yes --> G["Drive loop<br/>body-frame speed to the goal"]
  G -- "/speed" --> EP["EP mecanum chassis"]
  EP -. "moves" .-> A
```

The EP's wheel odometry drifts and the Crazyflie on top of it does not, so the riding Crazyflie is the rover's
only position source (`ROVER_RIDER=target`). `ROVER_RIDER_MODE=blend` switches back to the rider plus wheel
odometry. Without a rider, the rover starts from a taped start cross and counts wheel turns.

## Data model

```mermaid
erDiagram
  MISSION ||--|{ STEP : "plan (versioned)"
  MISSION ||--o{ FINDING : discovers
  MISSION ||--o{ MESSAGE : "chat log"
  MISSION ||--|| REPORT : "ends in"
  STEP }o--|| ACTION : calls
  STEP ||--o{ PHOTO : evidence
  FINDING |o--o| PHOTO : photo
  SCENARIO ||--o{ TARGET : places
  SCENARIO ||--o{ STEP : scripts
  SCENARIO }o--|| ARENA : "belongs to"
  ARENA ||--|{ ZONE : names
  ARENA ||--|| ROVER_BODY : footprint

  MISSION { string id string text string status int plan_version string source }
  STEP { string id string robot string action json args string status json result }
  ACTION { string name string robot bool checkpoint bool moves_rover }
  FINDING { string id float x float y string category bool hazardous bool authorized string status }
  ARENA { string kind rect bounds rect drone_cage pose home pose launch_pad }
  ZONE { string name rect area }
  ROVER_BODY { float length_m float width_m float grip_reach_m }
  SCENARIO { string name string title string arena }
  TARGET { string id string kind float x float y }
  PHOTO { string id bytes jpeg }
  MESSAGE { float t string role string text }
  REPORT { string yaml string html }
```

A finding is created by the scout and becomes the rover's goal. Plans refer to it as `$finding.f1` before it
exists, and the reference is resolved when the step runs. The same goes for `$zone.disposal`, `$home` and
`$start` (where the rover was when the mission began).

## Safety, in code

| Rule | Where |
| --- | --- |
| The drone is locked until the safety pilot switch is on | `Safety.mentor_gate`, every drone action |
| Flight targets stay inside the measured flight cage; the drone server refuses the rest | `Safety.drone_target`, `drone_server._outside_cage` |
| A lost Lighthouse position is never used, even with the geofence off | `drone_server._sane`, `RiderLocalization` |
| A low drone keeps 0.5 m from where the rover is going | `tools._separation_violation` |
| The gripper does not close on a hazardous finding without the operator's yes | `tools._authorization_gate` |
| Speeds and single moves are capped | `Safety.clamp_speed`, `Safety.clamp_move`, the rover server |
| A robot that stops answering stops the mission | `Monitor` watchdog |
| STOP halts the rover and lands the drone in parallel, without the AI | `Safety.stop_all` |
