# Duckiebot Voice Control Runbook

This runbook describes the standard operating procedure for running the current voice-control system.

## Paths

- Laptop code: your local clone of this repository
- Robot host code: `/home/duckie/robot/files`
- Robot Docker container code: `/root/robot/files`
- Robot IP: `10.0.0.185`
- ROS container: `ros-interface`
- Robot bridge port: `9010`

## Operating Rules

- Run `main_laptop.py` only on the Mac laptop.
- Run `main_robot.py` only inside the `ros-interface` Docker container.
- Do not run `main_robot.py` on the robot host shell.
- When robot-side files change, copy them to the robot host and then into the container.

## Terminal Roles

### Laptop Terminal

Used for:
- copying updated files to the robot
- running `main_laptop.py`

### Robot Host Terminal

Used for:
- copying files into Docker
- opening a shell in `ros-interface`

Prompt looks like:

```bash
duckie@duckiebot14:...
```

### Robot Container Terminal

Used for:
- running `main_robot.py`
- inspecting ROS topics

Prompt looks like:

```bash
root@duckiebot14:...
```

## 1. Update Robot Files

Run these commands on the laptop from the local repository directory when `main_robot.py` or `motor_controller.py` changes:

```bash
ssh duckie@10.0.0.185 "mkdir -p /home/duckie/robot/files"
scp main_robot.py duckie@10.0.0.185:/home/duckie/robot/files/
scp motor_controller.py duckie@10.0.0.185:/home/duckie/robot/files/
```

## 2. Copy Files Into Docker

Run these commands on the robot host:

```bash
docker exec ros-interface mkdir -p /root/robot/files
docker cp /home/duckie/robot/files/main_robot.py ros-interface:/root/robot/files/
docker cp /home/duckie/robot/files/motor_controller.py ros-interface:/root/robot/files/
```

## 3. Start the Robot Bridge

Run these commands on the robot host:

```bash
pkill -f "main_robot.py --port 9010" || true
docker exec -it ros-interface bash
cd /root/robot/files
python3 main_robot.py --port 9010
```

Expected output includes lines like:

```text
ROS node initialised — robot namespace: /duckiebot14
TCP bridge listening on 0.0.0.0:9010
```

If you see:

```text
rospy / duckietown_msgs not found — simulation mode
```

you are running in the wrong shell. Exit and rerun inside the `ros-interface` container.

## 4. Start the Laptop Controller

Run these commands on the laptop from the local repository directory:

```bash
./.venv/bin/python main_laptop.py --robot-ip 10.0.0.185 --robot-port 9010
```

Expected output includes:

```text
Using Azure Speech SDK
Connected to robot at 10.0.0.185:9010
Duckiebot Voice Controller READY
```

If the virtual environment is missing, create it on the laptop:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install azure-cognitiveservices-speech openai SpeechRecognition
```

## 5. Verify ROS Traffic

Open another robot host terminal and run:

```bash
docker exec -it ros-interface bash
rostopic echo /$VEHICLE_NAME/joy_mapper_node/car_cmd
```

Optional lane-follow switch check:

```bash
rostopic echo /$VEHICLE_NAME/lane_following_node/switch
```

## 6. Test Sequence

After both sides are running, say these commands in order:

1. `stop`
2. `go forward`
3. `left`
4. `right`
5. `stop`

Expected result:

- the robot bridge terminal prints `CMD: ...`
- the `car_cmd` topic shows `Twist2DStamped` messages

## Shutdown

### Laptop

Press `Ctrl+C`

### Robot Bridge

Press `Ctrl+C` in the container terminal running `main_robot.py`

## Troubleshooting

### Simulation Mode

Symptom:

```text
rospy / duckietown_msgs not found — simulation mode
```

Cause:
- `main_robot.py` was run on the host shell instead of inside `ros-interface`

Fix:

```bash
docker exec -it ros-interface bash
cd /root/robot/files
python3 main_robot.py --port 9010
```

### Port Already In Use

Symptom:

```text
Failed to bind to 0.0.0.0:9010
```

Cause:
- another copy of `main_robot.py` is already running

Fix:

```bash
pkill -f "main_robot.py --port 9010"
```

Then restart the bridge.

### Laptop Speech Packages Missing

Symptom:

```text
No speech recognition library available
```

Fix:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install azure-cognitiveservices-speech openai SpeechRecognition
```

Then run:

```bash
./.venv/bin/python main_laptop.py --robot-ip 10.0.0.185 --robot-port 9010
```

### Microphone Permission on macOS

Symptom:
- laptop controller starts, but speech input does not react

Fix:
- allow microphone access for Terminal or iTerm in macOS

Path:
- `System Settings -> Privacy & Security -> Microphone`

## Quick Start Summary

### Laptop

```bash
./.venv/bin/python main_laptop.py --robot-ip 10.0.0.185 --robot-port 9010
```

### Robot Host

```bash
docker exec -it ros-interface bash
cd /root/robot/files
python3 main_robot.py --port 9010
```
