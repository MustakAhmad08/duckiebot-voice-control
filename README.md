# Duckiebot Voice Controller — ECE49595NL HW2

Drive a Duckiebot robot using spoken English. Speech → Azure STT → GPT parser → TCP → ROS.

```
┌──────────────────────────────────────────────────────────────────┐
│  YOUR LAPTOP                                                      │
│                                                                   │
│  Microphone → Azure STT → NLP Parser (GPT / Rules)               │
│                                  │                                │
│                             RobotClient (TCP)                     │
└──────────────────────────────┬───────────────────────────────────┘
                               │  WiFi  port 9010
┌──────────────────────────────┴───────────────────────────────────┐
│  DUCKIEBOT (Jetson Nano)                                          │
│                                                                   │
│  RobotServer → joy_mapper_node/car_cmd                            │
│             ↕                                                     │
│  Duckietown ROS nodes (kinematics, wheels, lane following)        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Set up the Laptop

```bash
cd files
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Edit config.py with your Azure keys:
#   AZURE_SPEECH_KEY, AZURE_SPEECH_REGION
#   AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT
```

### 2. Set up the Robot

Use the Docker-based robot procedure in [RUNBOOK.md](RUNBOOK.md).

### 3. Run the Laptop Controller

```bash
cd files
./.venv/bin/python main_laptop.py --robot-ip <ROBOT_IP> --robot-port 9010
```

### 4. Speak!

| Say this…                          | Does this…              |
|------------------------------------|-------------------------|
| "go forward"                       | Drive forward until stopped |
| "turn left"                        | Arc left until stopped  |
| "turn right"                       | Arc right until stopped |
| "stop" / "halt" / "brake"          | Stop immediately        |
| "reverse" / "go back"              | Reverse until stopped   |
| "curve left" / "veer right"        | Soft curve until stopped |
| "spin left" / "rotate right"       | Spin until stopped      |
| "follow the lane" / "autonomous"   | Enable lane-following   |
| "manual" / "take control"          | Disable lane-following  |
| "full speed"                       | Set speed to maximum    |
| "slow down"                        | Set speed to 50%        |

---

## File Structure

```
files/
├── main_laptop.py      ← Entry point on the laptop
├── speech_input.py     ← Azure STT / fallback mic listener
├── nlp_parser.py       ← GPT + rule-based command parser
├── robot_client.py     ← TCP client to the robot
├── config.py           ← Local API keys (ignored by git)
├── main_robot.py       ← Entry point inside the robot ROS container
├── motor_controller.py ← TCP → ROS bridge
├── motor_test.py       ← Direct motor calibration test
├── RUNBOOK.md          ← Docker-based operating procedure
└── requirements.txt    ← Laptop Python dependencies
```

---

## Architecture Details

### Speech Pipeline (Laptop)
- **Azure Cognitive Services Speech SDK** for continuous low-latency recognition
- Falls back to **SpeechRecognition + Google Web API** if Azure not configured

### NLP Parser (Laptop)
- **Fast path**: regex rule-based for common commands — zero latency, no API call
- **GPT path**: Azure OpenAI for complex/compound sentences like *"turn left at the stop sign then go straight"*
- Returns a list of command dicts (supports compound commands)

### Robot Server (Duckiebot)
- Lightweight TCP server on port `9010`
- Runs inside the Duckietown `ros-interface` Docker container
- Publishes manual commands to `/$VEHICLE_NAME/joy_mapper_node/car_cmd`
- Publishes lane-follow toggles to `/$VEHICLE_NAME/lane_following_node/switch`
- Uses a watchdog to send a hard stop if commands stop arriving

---

## Tips for Race Day

1. **Say "stop" first** if anything goes wrong — it's the highest priority command.
2. **Lane following mode** only works if the corresponding Duckietown lane-following node is running.
3. **"Curve left/right"** is gentler than "left/right" — use for slight corrections.
4. Speak clearly and at a normal pace — Azure STT handles accents well.
5. Test your WiFi connection before the race. A ping < 50ms is ideal.
6. Manual drive commands continue until you say `stop`; adjust `KEEPALIVE_INTERVAL` in `main_laptop.py` if you need a different resend cadence.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Robot not connecting | Check robot IP, ensure `main_robot.py` is running inside `ros-interface`, check firewall |
| STT not working | Check Azure keys in `config.py`, or install `SpeechRecognition` fallback |
| Lane mode does nothing | Confirm the Duckietown lane-following node is running and subscribed to `/$VEHICLE_NAME/lane_following_node/switch` |
| Motors not moving | Confirm `car_cmd` messages are reaching Duckietown ROS and the wheel driver stack is healthy |
| Robot turns too sharply or too slowly | Tune `DUCKIE_BASE_V`, `DUCKIE_TURN_V`, `DUCKIE_CURVE_OMEGA`, `DUCKIE_TURN_OMEGA`, and `DUCKIE_SPIN_OMEGA` in `motor_controller.py` |
