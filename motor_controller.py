#!/usr/bin/env python3
"""
motor_controller.py — Robot-side motor controller for Duckiebot
Receives commands over a simple TCP socket from the laptop.
Controls left/right wheel motors via Adafruit MotorHAT or direct GPIO.
"""

import socket
import json
import threading
import time
import logging
import sys
import os

logging.basicConfig(level=logging.INFO, format="[ROBOT] %(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── Try to import real motor libraries; fall back to stubs for dev ───────────
try:
    from Adafruit_MotorHAT import Adafruit_MotorHAT
    HAT_AVAILABLE = True
except ImportError:
    HAT_AVAILABLE = False
    log.warning("Adafruit_MotorHAT not found — running in simulation mode")

# ─── Constants ────────────────────────────────────────────────────────────────
HOST = "0.0.0.0"          # Listen on all interfaces
PORT = 9000               # Must match laptop client
MAX_SPEED = 200           # 0-255 for MotorHAT
TURN_SPEED = 150
BASE_SPEED = 180
WATCHDOG_TIMEOUT = 2.0    # Stop if no command received within N seconds
I2C_BUS = int(os.environ.get("DUCKIE_I2C_BUS", "1"))

# Motor IDs on the HAT (adjust to your wiring)
LEFT_MOTOR_ID  = 1
RIGHT_MOTOR_ID = 2


class MotorDriver:
    """Thin wrapper around Adafruit_MotorHAT with a simulation fallback."""

    def __init__(self):
        if HAT_AVAILABLE:
            self.hat = Adafruit_MotorHAT(addr=0x60, i2c_bus=I2C_BUS)
            self.left  = self.hat.getMotor(LEFT_MOTOR_ID)
            self.right = self.hat.getMotor(RIGHT_MOTOR_ID)
        else:
            self.hat = self.left = self.right = None
        self._left_speed  = 0
        self._right_speed = 0

    def set(self, left_speed: int, right_speed: int):
        """
        Set individual wheel speeds.
        Positive = forward, negative = backward, 0 = stop.
        Speed magnitude: 0-255.
        """
        self._left_speed  = left_speed
        self._right_speed = right_speed
        if HAT_AVAILABLE:
            self._apply(self.left,  left_speed)
            self._apply(self.right, right_speed)
        else:
            log.info(f"[SIM] L={left_speed:+4d}  R={right_speed:+4d}")

    def _apply(self, motor, speed):
        if speed > 0:
            motor.setSpeed(min(speed, 255))
            motor.run(Adafruit_MotorHAT.FORWARD)
        elif speed < 0:
            motor.setSpeed(min(abs(speed), 255))
            motor.run(Adafruit_MotorHAT.BACKWARD)
        else:
            motor.run(Adafruit_MotorHAT.RELEASE)

    def stop(self):
        self.set(0, 0)


class RobotServer:
    """
    TCP server that receives JSON command packets from the laptop and drives motors.
    Packet schema: {"cmd": "<action>", "param": <optional float>}
    """

    # FIX: "left"/"right" changed from spin-in-place to forward-arcing turns.
    # The old implementation was identical to spin_left/spin_right, which is
    # unsuitable for a moving racetrack.  An arcing turn keeps the outer wheel
    # at BASE_SPEED and slows the inner wheel to ~20% to produce a tight but
    # still-forward arc.
    COMMANDS = {
        "forward":     lambda d, p: d.set( BASE_SPEED,  BASE_SPEED),
        "backward":    lambda d, p: d.set(-BASE_SPEED, -BASE_SPEED),
        "left":        lambda d, p: d.set( int(BASE_SPEED * 0.2),  BASE_SPEED),
        "right":       lambda d, p: d.set( BASE_SPEED, int(BASE_SPEED * 0.2)),
        "stop":        lambda d, p: d.stop(),
        "speed":       lambda d, p: d.set(int(p * MAX_SPEED), int(p * MAX_SPEED)),
        "spin_left":   lambda d, p: d.set(-MAX_SPEED,  MAX_SPEED),
        "spin_right":  lambda d, p: d.set( MAX_SPEED, -MAX_SPEED),
        "curve_left":  lambda d, p: d.set( int(BASE_SPEED * 0.4),  BASE_SPEED),
        "curve_right": lambda d, p: d.set( BASE_SPEED, int(BASE_SPEED * 0.4)),
    }

    def __init__(self, driver=None, host: str = HOST, port: int = PORT):
        self.driver = driver if driver is not None else MotorDriver()
        self.host = host
        self.port = port
        self.last_cmd_time = time.time()
        self._lock = threading.Lock()
        self._running = True
        self._watchdog_stopped = False

    def handle_client(self, conn, addr):
        log.info(f"Connected: {addr}")
        buffer = ""
        try:
            while self._running:
                data = conn.recv(1024).decode("utf-8")
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pkt = json.loads(line)
                        try:
                            self._dispatch(pkt)
                        except Exception:
                            log.exception(f"Command dispatch failed for packet: {pkt}")
                            raise
                    except json.JSONDecodeError as e:
                        log.warning(f"Bad JSON: {line!r} — {e}")
        except ConnectionResetError:
            log.warning(f"Client reset connection: {addr}")
        except Exception:
            log.exception(f"Robot client handler crashed for {addr}")
        finally:
            self.driver.stop()
            conn.close()
            log.info(f"Disconnected: {addr}")

    def _dispatch(self, pkt: dict):
        cmd   = pkt.get("cmd", "").lower()
        param = pkt.get("param", 1.0)
        with self._lock:
            self.last_cmd_time = time.time()
            self._watchdog_stopped = False
            if cmd in self.COMMANDS:
                log.info(f"CMD: {cmd}  param={param}")
                self.COMMANDS[cmd](self.driver, param)
            else:
                log.warning(f"Unknown command: {cmd!r}")

    def pet_watchdog(self):
        """FIX #2: allow external callers (e.g. LaneFollower) to reset the watchdog
        timer so autonomous driving doesn't get killed mid-lap."""
        with self._lock:
            self.last_cmd_time = time.time()
            self._watchdog_stopped = False

    def watchdog(self):
        """Stop motors if no command arrives within WATCHDOG_TIMEOUT seconds."""
        while self._running:
            time.sleep(0.25)
            with self._lock:
                if time.time() - self.last_cmd_time > WATCHDOG_TIMEOUT and not self._watchdog_stopped:
                    self.driver.stop()
                    self._watchdog_stopped = True

    def run(self):
        wd = threading.Thread(target=self.watchdog, daemon=True)
        wd.start()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((self.host, self.port))
            except OSError as e:
                raise RuntimeError(
                    f"Failed to bind robot server to {self.host}:{self.port}. "
                    f"Another process may already be using that port."
                ) from e
            srv.listen(5)   # FIX #3: raise backlog so rapid reconnects aren't refused
            log.info(f"Listening on {self.host}:{self.port}")
            try:
                while self._running:
                    conn, addr = srv.accept()
                    t = threading.Thread(target=self.handle_client,
                                        args=(conn, addr), daemon=True)
                    t.start()
            except KeyboardInterrupt:
                log.info("Shutting down…")
                self._running = False
                self.driver.stop()


if __name__ == "__main__":
    RobotServer().run()
