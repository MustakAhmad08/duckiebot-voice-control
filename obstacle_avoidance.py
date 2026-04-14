#!/usr/bin/env python3
"""
obstacle_avoidance.py — Time-of-Flight sensor obstacle avoidance
Uses VL53L0X (common Duckiebot ToF) to stop before hitting obstacles.
Runs as a safety layer on top of any other motor commands.
"""

import threading
import time
import logging

logging.basicConfig(level=logging.INFO, format="[TOF] %(asctime)s %(message)s")
log = logging.getLogger(__name__)

STOP_DISTANCE_MM  = 200   # Stop if obstacle closer than 20 cm
SLOW_DISTANCE_MM  = 400   # Slow down if obstacle closer than 40 cm
POLL_INTERVAL     = 0.05  # 20 Hz

try:
    import VL53L0X
    TOF_AVAILABLE = True
except ImportError:
    TOF_AVAILABLE = False
    log.warning("VL53L0X not found — obstacle avoidance in simulation mode")


class ObstacleAvoider:
    """
    Wraps a MotorDriver and intercepts set() calls to enforce safety distances.
    """

    def __init__(self, motor_driver):
        self.driver   = motor_driver
        self._tof     = None
        self._blocked = threading.Event()
        self._running = True
        self._distance_mm = 9999

    def start(self):
        if TOF_AVAILABLE:
            self._tof = VL53L0X.VL53L0X()
            self._tof.start_ranging(VL53L0X.VL53L0X_BETTER_ACCURACY_MODE)
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        log.info("Obstacle avoider started")

    def _poll_loop(self):
        while self._running:
            if TOF_AVAILABLE and self._tof:
                try:
                    self._distance_mm = self._tof.get_distance()
                except Exception:
                    self._distance_mm = 9999
            else:
                # Simulation: pretend nothing is close
                self._distance_mm = 9999

            # FIX: use <= so that a reading exactly at STOP_DISTANCE_MM
            # also triggers the blocked state, closing the gap where the
            # robot could inch forward right at the threshold.
            if self._distance_mm <= STOP_DISTANCE_MM:
                if not self._blocked.is_set():
                    log.warning(f"OBSTACLE at {self._distance_mm}mm — STOPPING")
                    self.driver.stop()
                    self._blocked.set()
            else:
                self._blocked.clear()

            time.sleep(POLL_INTERVAL)

    def safe_set(self, left_speed: int, right_speed: int):
        """Call this instead of driver.set() to get obstacle protection."""
        if self._blocked.is_set():
            # Allow backward movement to escape
            if left_speed < 0 and right_speed < 0:
                self.driver.set(left_speed, right_speed)
            else:
                self.driver.stop()
        elif self._distance_mm < SLOW_DISTANCE_MM:
            # Reduce speed proportionally.
            # factor is guaranteed > 0 here because _blocked cleared means
            # distance > STOP_DISTANCE_MM, so numerator > 0.
            factor = max(0.3, (self._distance_mm - STOP_DISTANCE_MM) /
                         (SLOW_DISTANCE_MM - STOP_DISTANCE_MM))
            self.driver.set(int(left_speed * factor), int(right_speed * factor))
        else:
            self.driver.set(left_speed, right_speed)

    @property
    def distance_mm(self):
        return self._distance_mm

    def stop(self):
        self._running = False
        if TOF_AVAILABLE and self._tof:
            self._tof.stop_ranging()