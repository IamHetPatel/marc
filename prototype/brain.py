"""
brain.py - language -> robot actions with a VLM that sees the robot camera and checks its own work.
Runs on any Mac/Windows laptop with Python 3.10+. Talks to robot_server.py over HTTP.

  pip install google-genai requests
  export GEMINI_API_KEY=...  ROBOT=http://<windows-laptop-ip>:8000
  python brain.py "Drive to the red cup, grab it, and turn around"

Swap MODEL to gemini-robotics-er-1.5-preview (or any Gemini model) if the ER 2 preview errors.
"""
import os, sys
import requests
from google import genai
from google.genai import types

ROBOT = os.getenv("ROBOT", "http://localhost:8000")
MODEL = os.getenv("MODEL", "gemini-robotics-er-2-preview")


def _call(path, **params):
    return requests.get(ROBOT + path, params=params, timeout=30).json()


def move(forward_m: float, left_m: float, turn_left_deg: float) -> dict:
    """Move the robot relative to its current pose. Keep each step <= 1 m. Turn is in degrees, positive = left."""
    return _call("/drive", x=forward_m, y=left_m, z=turn_left_deg)


def gripper(action: str) -> dict:
    """Open or close the gripper. action is 'open' or 'close'."""
    return _call("/grip", action=action)


def arm(forward_mm: float, up_mm: float) -> dict:
    """Move the arm relative to its current position, in millimetres."""
    return _call("/arm", x=forward_mm, y=up_mm)


def look() -> str:
    """Take a fresh camera picture. Call this after every move to check progress."""
    r = requests.get(ROBOT + "/snapshot", timeout=10)
    if r.status_code != 200:
        return "camera unavailable"
    _latest["jpg"] = r.content
    return "image captured; it is attached to the next message"


_latest = {"jpg": None}

SYSTEM = (
    "You control a small mecanum-wheel robot with a gripper and a front camera. "
    "Work in small steps: look, decide, act, look again. Never move more than 1 m per step. "
    "If the target is not visible, rotate 45 degrees and look again. Report clearly when done or stuck."
)


def run(task, max_turns=12):
    client = genai.Client()
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=[move, gripper, arm, look],  # auto function calling
            temperature=0.2,
        ),
    )
    look()
    msg = [task]
    for _ in range(max_turns):
        if _latest["jpg"]:
            msg.append(types.Part.from_bytes(data=_latest["jpg"], mime_type="image/jpeg"))
            _latest["jpg"] = None
        resp = chat.send_message(msg)
        print("robot:", resp.text)
        if _latest["jpg"] is None:  # model stopped calling look(), so it is done
            break
        msg = ["Here is the latest camera image. Continue."]


if __name__ == "__main__":
    run(" ".join(sys.argv[1:]) or "Look around and describe what you see.")
