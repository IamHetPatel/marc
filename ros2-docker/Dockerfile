# ROS 2 dev container. Change the distro with: --build-arg ROS_DISTRO=jazzy (match the robot's distro if needed)
ARG ROS_DISTRO=lyrical
FROM ros:${ROS_DISTRO}-ros-base
ARG ROS_DISTRO
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-${ROS_DISTRO}-rviz2 \
      ros-${ROS_DISTRO}-rosbridge-server \
      ros-${ROS_DISTRO}-demo-nodes-cpp \
      ros-${ROS_DISTRO}-demo-nodes-py \
      python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> /root/.bashrc
WORKDIR /work
