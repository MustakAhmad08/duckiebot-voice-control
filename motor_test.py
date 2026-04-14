#!/usr/bin/env python3
"""
motor_test.py — direct Motor HAT channel test for Duckiebot

Examples:
    python3 motor_test.py --motor 1 --speed 180 --duration 2
    python3 motor_test.py --motor 2 --speed -180 --duration 2
    python3 motor_test.py --both --speed 160 --duration 2
"""

import argparse
import os
import time

from Adafruit_MotorHAT import Adafruit_MotorHAT

I2C_BUS = int(os.environ.get("DUCKIE_I2C_BUS", "1"))
MOTOR_HAT_ADDR = 0x60


def apply_speed(motor, speed: int):
    if speed > 0:
        motor.setSpeed(min(speed, 255))
        motor.run(Adafruit_MotorHAT.FORWARD)
    elif speed < 0:
        motor.setSpeed(min(abs(speed), 255))
        motor.run(Adafruit_MotorHAT.BACKWARD)
    else:
        motor.run(Adafruit_MotorHAT.RELEASE)


def main():
    parser = argparse.ArgumentParser(description="Direct motor channel test")
    parser.add_argument("--motor", type=int, choices=[1, 2, 3, 4],
                        help="single Motor HAT channel to test")
    parser.add_argument("--both", action="store_true",
                        help="test motor channels 1 and 2 together")
    parser.add_argument("--speed", type=int, default=180,
                        help="motor speed from -255 to 255")
    parser.add_argument("--duration", type=float, default=2.0,
                        help="seconds to run before stopping")
    args = parser.parse_args()

    if not args.both and args.motor is None:
        parser.error("choose --motor N or --both")

    hat = Adafruit_MotorHAT(addr=MOTOR_HAT_ADDR, i2c_bus=I2C_BUS)

    motors = []
    if args.both:
        motors = [hat.getMotor(1), hat.getMotor(2)]
    else:
        motors = [hat.getMotor(args.motor)]

    try:
        for motor in motors:
            apply_speed(motor, args.speed)
        time.sleep(args.duration)
    finally:
        for motor in motors:
            apply_speed(motor, 0)


if __name__ == "__main__":
    main()
