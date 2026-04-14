# Duckiebot Voice Controller — ECE49595NL HW2

Drive a Duckiebot robot using spoken English. Speech → Azure STT → GPT parser → TCP → motors.

```
┌──────────────────────────────────────────────────────────────────┐
│  YOUR LAPTOP                                                      │
│                                                                   │
│  Microphone → Azure STT → NLP Parser (GPT / Rules)               │
│                                  │                                │
│                             RobotClient (TCP)                     │
└──────────────────────────────┬───────────────────────────────────┘
                               │  WiFi  port 9000
┌──────────────────────────────┴───────────────────────────────────┐
│  DUCKIEBOT (Jetson Nano)                                          │
│                                                                   │
│  RobotServer → MotorDriver                                        │
│             ↕                                                     │
│  LaneFollower (camera / OpenCV)                                   │
│  ObstacleAvoider (ToF sensor)                                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Set up the Laptop

```bash
cd laptop/
pip install -r requirements.txt

# Edit config.py with your Azure keys:
#   AZURE_SPEECH_KEY, AZURE_SPEECH_REGION
#   AZURE_OPENAI_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT
```

### 2. Set up the Robot

SSH into the Duckiebot:
```bash
ssh duckie@<ROBOT_IP>

cd robot/
pip install -r requirements.txt
python3 main_robot.py
```

### 3. Run the Laptop Controller

```bash
python3 main_laptop.py --robot-ip <ROBOT_IP>
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
duckiebot/
├── laptop/
│   ├── main_laptop.py      ← Entry point (run this on your laptop)
│   ├── speech_input.py     ← Azure STT / fallback mic listener
│   ├── nlp_parser.py       ← GPT + rule-based command parser
│   ├── robot_client.py     ← TCP client to robot
│   ├── config.py           ← Your API keys (DO NOT commit)
│   └── requirements.txt
│
└── robot/
    ├── main_robot.py       ← Entry point (run this on the Duckiebot)
    ├── motor_controller.py ← TCP server + motor driver
    ├── lane_follower.py    ← Camera-based lane following (OpenCV)
    ├── obstacle_avoidance.py ← ToF sensor safety layer
    └── requirements.txt
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
- Lightweight TCP server on port 9000
- **Watchdog**: robot auto-stops motors if no command for 2 seconds; manual drive commands are re-sent from the laptop to keep motion active until `stop`
- Parses newline-delimited JSON packets

### Lane Follower (Duckiebot)
- Detects **yellow** (left boundary) and **white** (right boundary) tape via HSV masking
- Proportional steering correction
- Toggle on/off via voice (`"follow the lane"` / `"manual"`)

### Obstacle Avoider (Duckiebot)
- Polls VL53L0X ToF sensor at 20 Hz
- Slows down at 40cm, stops at 20cm
- Allows backward escape movement

---

## Tips for Race Day

1. **Say "stop" first** if anything goes wrong — it's the highest priority command.
2. **Lane following mode** is great for straight sections; switch to manual for tight turns.
3. **"Curve left/right"** is gentler than "turn left/right" — use for slight corrections.
4. Speak clearly and at a normal pace — Azure STT handles accents well.
5. Test your WiFi connection before the race. A ping < 50ms is ideal.
6. Manual drive commands continue until you say `stop`; adjust `KEEPALIVE_INTERVAL` in `main_laptop.py` if you need a different resend cadence.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Robot not connecting | Check robot IP, ensure `main_robot.py` is running, check firewall |
| STT not working | Check Azure keys in `config.py`, or install `SpeechRecognition` fallback |
| Lane follower veers off | Retune `YELLOW_LOW/HIGH`, `WHITE_LOW/HIGH` HSV values in `lane_follower.py` |
| Motors not moving | Confirm motor IDs (`LEFT_MOTOR_ID`, `RIGHT_MOTOR_ID`) match your wiring |
| Too slow/fast | Adjust `BASE_SPEED`, `TURN_SPEED` in `motor_controller.py` |
