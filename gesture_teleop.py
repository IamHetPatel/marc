"""
gesture_teleop.py - drive the robot with your hand in front of the laptop webcam (no gloves, no XR).
  pip install mediapipe opencv-python requests
  ROBOT=http://<windows-laptop-ip>:8000 python gesture_teleop.py

Open palm = stop | index finger up = forward | fist = backward
Hand left/right of centre = turn. Press q to quit.
Uses the classic mediapipe.solutions.hands API; if your mediapipe version has removed it,
pin an older one: pip install "mediapipe==0.10.14".
"""
import os, time
import cv2, requests
import mediapipe as mp

ROBOT = os.getenv("ROBOT", "http://localhost:8000")
hands = mp.solutions.hands.Hands(max_num_hands=1, min_detection_confidence=0.6)


def fingers_up(lm):
    tips, pips = [8, 12, 16, 20], [6, 10, 14, 18]
    return [lm[t].y < lm[p].y for t, p in zip(tips, pips)]  # image y grows downward


def classify(lm):
    up = fingers_up(lm)
    if all(up):
        return "stop"
    if up[0] and not any(up[1:]):
        return "forward"
    if not any(up):
        return "backward"
    return "stop"


cap = cv2.VideoCapture(0)
last = 0.0
while cap.isOpened():
    ok, frame = cap.read()
    if not ok:
        break
    frame = cv2.flip(frame, 1)
    res = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cmd, x, z = "none", 0.0, 0.0
    if res.multi_hand_landmarks:
        lm = res.multi_hand_landmarks[0].landmark
        cmd = classify(lm)
        x = {"forward": 0.2, "backward": -0.15}.get(cmd, 0.0)
        cx = lm[9].x  # palm centre, 0..1
        z = 0.0 if abs(cx - 0.5) < 0.15 else (-45.0 if cx > 0.5 else 45.0)
        if cmd == "stop":
            z = 0.0
    if time.time() - last > 0.2:  # 5 Hz command rate is plenty and keeps Wi-Fi/5G calm
        try:
            requests.get(ROBOT + "/speed", params={"x": x, "y": 0, "z": z, "t": 0.4}, timeout=0.5)
        except requests.RequestException:
            pass
        last = time.time()
    cv2.putText(frame, f"{cmd}  turn={z:+.0f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.imshow("gesture teleop", frame)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break
cap.release()
cv2.destroyAllWindows()
