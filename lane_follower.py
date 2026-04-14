#!/usr/bin/env python3
"""
lane_follower.py — Autonomous lane-following for Duckiebot
Uses the onboard camera + OpenCV to detect yellow/white tape lines.
Can be toggled on/off by the laptop controller.
Sends motor commands internally (same MotorDriver).
"""

import cv2
import numpy as np
import threading
import time
import logging
import socket
import json

logging.basicConfig(level=logging.INFO, format="[LANE] %(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── Tunable Parameters ───────────────────────────────────────────────────────
CAMERA_ID    = 0
FRAME_W      = 640
FRAME_H      = 480
ROI_TOP_FRAC = 0.55   # only look at bottom 45% of frame

BASE_SPEED   = 160
STEER_GAIN   = 1.8    # how aggressively to correct steering
MAX_CORRECTION = 90   # max speed difference left vs right

STATUS_PORT  = 9001   # sends lane status JSON to laptop

# HSV ranges for tape colours (tune on your track!)
YELLOW_LOW  = np.array([ 18,  80,  80])
YELLOW_HIGH = np.array([ 35, 255, 255])
WHITE_LOW   = np.array([  0,   0, 180])
WHITE_HIGH  = np.array([180,  40, 255])


class LaneFollower:
    def __init__(self, motor_driver, status_port: int = STATUS_PORT, watchdog_pet=None):
        self.driver  = motor_driver
        self.status_port = status_port
        self.enabled = threading.Event()
        self.running = True
        self._cap    = None
        self._status_server_started = False
        self._status_clients = []
        self._lock   = threading.Lock()
        self._watchdog_pet = watchdog_pet   # FIX #2: callback to reset RobotServer watchdog

    # ── Camera helpers ────────────────────────────────────────────────────────

    def _open_camera(self):
        cap = cv2.VideoCapture(CAMERA_ID)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
        if not cap.isOpened():
            raise RuntimeError("Cannot open camera")
        return cap

    # ── Lane detection ────────────────────────────────────────────────────────

    def _detect_centroid(self, frame, hsv_low, hsv_high):
        """Return x-centroid of the largest blob matching the colour range."""
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, hsv_low, hsv_high)
        # ROI: bottom portion of frame
        roi_top = int(FRAME_H * ROI_TOP_FRAC)
        mask[:roi_top, :] = 0
        mask = cv2.erode(mask,  None, iterations=2)
        mask = cv2.dilate(mask, None, iterations=2)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return None
        c = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(c) < 500:
            return None
        M = cv2.moments(c)
        if M["m00"] == 0:
            return None
        return int(M["m10"] / M["m00"])

    def _compute_steering(self, frame):
        """
        Returns (left_speed, right_speed) based on lane lines.
        Strategy: keep yellow line on the left, white line on the right.

        FIX: single-line error signs were inverted — robot steered away from
        the visible line instead of tracking it.  The error must express
        "how far is the robot to the RIGHT of where it should be", so that a
        positive error → right-of-centre → increase left speed (turn left).
        """
        cx = FRAME_W // 2
        yellow_x = self._detect_centroid(frame, YELLOW_LOW, YELLOW_HIGH)
        white_x  = self._detect_centroid(frame, WHITE_LOW,  WHITE_HIGH)

        error = 0.0
        if yellow_x is not None and white_x is not None:
            lane_center = (yellow_x + white_x) // 2
            error = lane_center - cx
        elif yellow_x is not None:
            # Only yellow (left boundary) visible.
            # Desired: yellow should sit at cx - FRAME_W//6 (left of centre).
            # error > 0 means robot is too far right → steer left (correct).
            error = yellow_x - (cx - FRAME_W // 6)
        elif white_x is not None:
            # Only white (right boundary) visible.
            # Desired: white should sit at cx + FRAME_W//6 (right of centre).
            # error > 0 means robot is too far right → steer left (correct).
            error = (cx + FRAME_W // 6) - white_x
        else:
            # No lines — go straight and hope for the best
            return BASE_SPEED, BASE_SPEED

        correction = int(STEER_GAIN * error)
        correction = max(-MAX_CORRECTION, min(MAX_CORRECTION, correction))
        left_speed  = BASE_SPEED + correction
        right_speed = BASE_SPEED - correction
        return (max(0, min(255, left_speed)),
                max(0, min(255, right_speed)))

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        try:
            self._cap = self._open_camera()
        except Exception as e:
            log.warning(f"Lane follower disabled: camera startup failed ({e})")
            self.enabled.clear()
            self.running = False
            return
        log.info("Camera open. Lane follower ready.")
        try:
            while self.running:
                ret, frame = self._cap.read()
                if not ret:
                    log.warning("Camera read failed")
                    time.sleep(0.1)
                    continue

                if self.enabled.is_set():
                    l, r = self._compute_steering(frame)
                    self.driver.set(l, r)
                    if self._watchdog_pet:          # FIX #2: keep watchdog alive during autonomous drive
                        self._watchdog_pet()
                    self._broadcast_status({"lane": "active",
                                            "left": l, "right": r})
                else:
                    time.sleep(0.05)
        finally:
            if self._cap is not None:
                self._cap.release()

    def enable(self):
        log.info("Lane following ENABLED")
        self.enabled.set()

    def disable(self):
        log.info("Lane following DISABLED")
        self.enabled.clear()
        self.driver.stop()   # FIX #1: stop motors immediately on disable

    def toggle(self):
        if self.enabled.is_set():
            self.disable()
        else:
            self.enable()

    def stop(self):
        self.running = False

    # ── Status broadcast ──────────────────────────────────────────────────────

    def _broadcast_status(self, status: dict):
        """Send lane status as JSON to any connected laptop listeners.
        FIX #4: copy client list before releasing lock so blocking sendall()
        never holds _lock — prevents stalling the camera loop and accept thread.
        """
        msg = (json.dumps(status) + "\n").encode()
        with self._lock:
            clients = list(self._status_clients)
        dead = []
        for c in clients:
            try:
                c.sendall(msg)
            except OSError:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    self._status_clients.remove(c)

    def start_status_server(self):
        def _serve():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    srv.bind(("0.0.0.0", self.status_port))
                except OSError as e:
                    log.warning(f"Lane status server disabled on :{self.status_port} ({e})")
                    return
                srv.listen(5)
                self._status_server_started = True
                log.info(f"Status server on :{self.status_port}")
                while self.running:
                    try:
                        conn, addr = srv.accept()
                        log.info(f"Status client: {addr}")
                        with self._lock:
                            self._status_clients.append(conn)
                    except OSError:
                        break
        t = threading.Thread(target=_serve, daemon=True)
        t.start()


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from motor_controller import MotorDriver
    driver = MotorDriver()
    follower = LaneFollower(driver)
    follower.enable()
    follower.start_status_server()
    follower.run()
