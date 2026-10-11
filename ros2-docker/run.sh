#!/usr/bin/env bash
# Opens a shell in the ROS 2 container. The repo is mounted at /work.
# Host networking so DDS discovery and rosbridge (:9090) reach robots on the LAN.
set -e
cd "$(dirname "$0")"
docker build -t marc-ros2 .
xhost +local:root >/dev/null 2>&1 || true   # let RViz open a window (via XWayland)
exec docker run -it --rm --name marc-ros2 \
  --network host --ipc host \
  -e DISPLAY="${DISPLAY:-:0}" -e QT_QPA_PLATFORM=xcb \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$(cd .. && pwd)":/work \
  marc-ros2 bash
