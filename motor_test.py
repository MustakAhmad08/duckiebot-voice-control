#!/usr/bin/env python3

import argparse
import os
import time
import logging
import socket
from pathlib import Path
from typing import List, Tuple

try:
    from Adafruit_MotorHAT import Adafruit_MotorHAT
except ImportError:
    raise RuntimeError("Adafruit_MotorHAT not installed")

logging.basicConfig(level=logging.INFO, format="[MOTOR TEST] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────
I2C_BUS = int(os.getenv("DUCKIE_I2C_BUS", "1"))
MOTOR_HAT_ADDR = 0x60

LEFT_MOTOR_ID = int(os.getenv("DUCKIE_LEFT_MOTOR_ID", "1"))
RIGHT_MOTOR_ID = int(os.getenv("DUCKIE_RIGHT_MOTOR_ID", "2"))

LEFT_MOTOR_SIGN = int(os.getenv("DUCKIE_LEFT_MOTOR_SIGN", "1"))
RIGHT_MOTOR_SIGN = int(os.getenv("DUCKIE_RIGHT_MOTOR_SIGN", "1"))

# Apply the same calibration defaults as the runtime controller.
LEFT_SPEED_SCALE = float(os.getenv("DUCKIE_LEFT_SPEED_SCALE", "1.0"))
RIGHT_SPEED_SCALE = float(os.getenv("DUCKIE_RIGHT_SPEED_SCALE", "1.0"))
LEFT_SPEED_TRIM = int(os.getenv("DUCKIE_LEFT_SPEED_TRIM", "0"))
RIGHT_SPEED_TRIM = int(os.getenv("DUCKIE_RIGHT_SPEED_TRIM", "0"))

# ─────────────────────────────────────────────────────────────

def clamp(val, lo=-255, hi=255):
    return max(lo, min(hi, val))


def apply_trim(speed: int, trim: int) -> int:
    if speed == 0 or trim == 0:
        return speed
    if speed > 0:
        return speed + trim
    return speed - trim


def _candidate_kinematics_files() -> List[Path]:
    configured = os.getenv("DUCKIE_KINEMATICS_FILE")
    if configured:
        return [Path(configured)]

    robot_name = (
        os.getenv("VEHICLE_NAME")
        or os.getenv("DUCKIEBOT_NAME")
        or socket.gethostname().split(".")[0]
    )
    base = Path("/data/config/calibrations/kinematics")
    return [
        base / f"{robot_name}.yaml",
        base / "default.yaml",
    ]


def load_kinematics_calibration() -> Tuple[float, float]:
    gain = 1.0
    trim = 0.0

    for path in _candidate_kinematics_files():
        if not path.exists():
            continue
        values = {}
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, raw = line.split(":", 1)
            values[key.strip()] = raw.strip()
        gain = float(values.get("gain", gain))
        trim = float(values.get("trim", trim))
        log.info(f"Using kinematics calibration from {path} (gain={gain:.3f}, trim={trim:.3f})")
        return gain, trim

    return gain, trim


def calibrate(motor_id: int, speed: int) -> int:
    """
    Convert logical speed -> calibrated motor command.
    """
    speed = clamp(speed)
    gain, trim = load_kinematics_calibration()
    left_factor = LEFT_SPEED_SCALE * max(0.0, gain - trim)
    right_factor = RIGHT_SPEED_SCALE * max(0.0, gain + trim)

    if motor_id == LEFT_MOTOR_ID:
        physical = int(speed * left_factor)
        physical = apply_trim(physical, LEFT_SPEED_TRIM)
        return physical * LEFT_MOTOR_SIGN

    if motor_id == RIGHT_MOTOR_ID:
        physical = int(speed * right_factor)
        physical = apply_trim(physical, RIGHT_SPEED_TRIM)
        return physical * RIGHT_MOTOR_SIGN

    return speed


def apply_speed(motor, speed: int):
    """
    Apply speed to motor (handles direction here ONLY)
    """
    speed = clamp(speed)

    if speed > 0:
        motor.setSpeed(min(abs(speed), 255))
        motor.run(Adafruit_MotorHAT.FORWARD)

    elif speed < 0:
        motor.setSpeed(min(abs(speed), 255))
        motor.run(Adafruit_MotorHAT.BACKWARD)

    else:
        motor.setSpeed(0)
        motor.run(Adafruit_MotorHAT.RELEASE)


# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Duckiebot motor test")

    parser.add_argument("--motor", type=int,
                        help="Motor channel (1–4)")
    parser.add_argument("--both", action="store_true",
                        help="Test left & right motors together")
    parser.add_argument("--speed", type=int, default=180,
                        help="Speed (-255 to 255)")
    parser.add_argument("--duration", type=float, default=2.0,
                        help="Run time (seconds)")

    args = parser.parse_args()

    if not args.both and args.motor is None:
        parser.error("Use --motor N or --both")

    args.speed = clamp(args.speed)

    # ─── Init Motor HAT ──────────────────────────────────────
    try:
        hat = Adafruit_MotorHAT(addr=MOTOR_HAT_ADDR, i2c_bus=I2C_BUS)
    except Exception as e:
        log.error(f"I2C init failed: {e}")
        return

    # ─── Select motors ───────────────────────────────────────
    if args.both:
        motor_ids = [LEFT_MOTOR_ID, RIGHT_MOTOR_ID]
    else:
        motor_ids = [args.motor]

    motors = [hat.getMotor(mid) for mid in motor_ids]

    # ─── Run test ────────────────────────────────────────────
    try:
        for motor, mid in zip(motors, motor_ids):
            physical = calibrate(mid, args.speed)

            log.info(
                f"Motor {mid}: logical={args.speed:+d} → calibrated={physical:+d}"
            )

            apply_speed(motor, physical)

        time.sleep(args.duration)

    finally:
        log.info("Stopping motors...")
        for motor in motors:
            apply_speed(motor, 0)


# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
