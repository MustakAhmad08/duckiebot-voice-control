#!/usr/bin/env python3
"""
motor_controller.py — TCP → ROS 1 bridge for Duckiebot
Receives JSON command packets from the laptop over TCP and publishes
to the official Duckietown ROS topics.

ROS topics used:
  /ROBOT_NAME/wheels_driver_node/wheels_cmd  (duckietown_msgs/WheelsCmdStamped)
  /ROBOT_NAME/lane_following_node/switch     (duckietown_msgs/BoolStamped)
  /ROBOT_NAME/joy_mapper_node/joystick_override (duckietown_msgs/BoolStamped)

Run with:
  rosrun <your_package> motor_controller.py
  or standalone:
  python3 motor_controller.py
"""

import socket
import json
import threading
import time
import logging
import os

logging.basicConfig(level=logging.INFO, format="[BRIDGE] %(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ─── ROS imports ──────────────────────────────────────────────────────────────
try:
    import rospy
    from duckietown_msgs.msg import WheelsCmdStamped, BoolStamped
    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False
    log.warning("rospy / duckietown_msgs not found — simulation mode")

# ─── Constants ────────────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 9010
WATCHDOG_TIMEOUT = 5.0

ROBOT_NAME = os.environ.get("VEHICLE_NAME", "duckie")

# Normalised wheel speed (0.0 – 1.0) sent to WheelsCmdStamped
MAX_WHEEL = 1.0
BASE_WHEEL = 0.4
ARC_RATIO = 0.2   # inner wheel fraction during arc turn (0 = tighter, 0.5 = gentle)

TURN_90_DURATION = float(os.environ.get("DUCKIE_TURN_90_DURATION", "0.5"))
TURN_IN_PLACE_WHEEL = float(os.environ.get("DUCKIE_TURN_IN_PLACE_WHEEL", "0.5"))


# ─── ROS Publisher wrapper ────────────────────────────────────────────────────

class ROSDriver:
    """
    Publishes wheel and lane commands to the official Duckietown ROS topics.
    Falls back to simulation logging when ROS is unavailable.
    """

    def __init__(self):
        if ROS_AVAILABLE:
            rospy.init_node("tcp_bridge", anonymous=False, disable_signals=True)
            ns = f"/{ROBOT_NAME}"
            self._wheels_pub = rospy.Publisher(
                f"{ns}/wheels_driver_node/wheels_cmd",
                WheelsCmdStamped, queue_size=1
            )
            self._lane_pub = rospy.Publisher(
                f"{ns}/lane_following_node/switch",
                BoolStamped, queue_size=1
            )
            self._joy_override_pub = rospy.Publisher(
                f"{ns}/joy_mapper_node/joystick_override",
                BoolStamped, queue_size=1
            )
            log.info(f"ROS node initialised — robot namespace: {ns}")
        else:
            self._wheels_pub = None
            self._lane_pub = None
            self._joy_override_pub = None

    def set_wheels(self, left: float, right: float):
        """Publish normalised wheel speeds in [-1.0, 1.0]."""
        left = max(-MAX_WHEEL, min(MAX_WHEEL, float(left)))
        right = max(-MAX_WHEEL, min(MAX_WHEEL, float(right)))

        if ROS_AVAILABLE:
            msg = WheelsCmdStamped()
            msg.header.stamp = rospy.Time.now()
            msg.vel_left = left
            msg.vel_right = right
            self._wheels_pub.publish(msg)
        else:
            log.info(f"[SIM] wheels L={left:+.2f}  R={right:+.2f}")

    def set_lane(self, enabled: bool):
        """
        Toggle lane following through the official JoyMapper override path.
        In Duckietown, joystick_override=False enables autonomy and
        joystick_override=True returns control to manual driving.
        """
        if ROS_AVAILABLE:
            stamp = rospy.Time.now()

            joy_msg = BoolStamped()
            joy_msg.header.stamp = stamp
            joy_msg.data = not enabled
            self._joy_override_pub.publish(joy_msg)

            # Legacy compatibility
            lane_msg = BoolStamped()
            lane_msg.header.stamp = stamp
            lane_msg.data = enabled
            self._lane_pub.publish(lane_msg)
        else:
            log.info(
                f"[SIM] lane_following={'ON' if enabled else 'OFF'} "
                f"joystick_override={'OFF' if enabled else 'ON'}"
            )

    def stop(self):
        self.set_wheels(0.0, 0.0)


# ─── RobotServer ──────────────────────────────────────────────────────────────

class RobotServer:
    """TCP server that translates JSON command packets into ROS topic publishes."""

    def __init__(self, driver: ROSDriver = None, host: str = HOST, port: int = PORT):
        self.driver = driver or ROSDriver()
        self.host = host
        self.port = port

        self.last_cmd_time = time.time()
        self._lock = threading.Lock()
        self._running = True
        self._watchdog_stopped = False

        self._turn_timer = None
        self._busy_turning = False

    # ── Client handler ────────────────────────────────────────────────────────

    def handle_client(self, conn, addr):
        log.info(f"Connected: {addr}")
        conn.settimeout(1.0)
        buf = ""

        try:
            while self._running:
                try:
                    data = conn.recv(1024).decode()
                except socket.timeout:
                    continue

                if not data:
                    break

                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._dispatch(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning(f"Bad JSON: {line!r} — {e}")
                    except Exception:
                        log.exception("Dispatch error")
        except Exception:
            log.exception(f"Client handler crashed for {addr}")
        finally:
            # Stop robot if controlling client disconnects
            self._cancel_turn_timer()
            self.driver.stop()
            conn.close()
            log.info(f"Disconnected: {addr}")

    # ── Timed turn helpers ────────────────────────────────────────────────────

    def _cancel_turn_timer(self):
        """
        Cancel any active timed turn and clear turning state.
        This is the critical fix: timer state and busy state must stay consistent.
        """
        with self._lock:
            timer = self._turn_timer
            self._turn_timer = None
            self._busy_turning = False

        if timer is not None:
            timer.cancel()

    def _finish_timed_turn(self):
        with self._lock:
            self._turn_timer = None
            self._busy_turning = False

        log.info("Timed turn complete — stopping")
        self.driver.stop()

    def _timed_turn(self, left: float, right: float):
        """Rotate for a fixed duration, then stop."""
        with self._lock:
            if self._busy_turning:
                log.info("Already turning — ignoring repeated turn command")
                return

            self._busy_turning = True

        self.driver.set_wheels(left, right)

        timer = threading.Timer(TURN_90_DURATION, self._finish_timed_turn)
        timer.daemon = True

        with self._lock:
            self._turn_timer = timer

        timer.start()

    # ── Command execution ─────────────────────────────────────────────────────

    def _execute(self, cmd: str, param: float):
        try:
            param = float(param)
        except (TypeError, ValueError):
            param = 1.0

        param = max(-1.0, min(1.0, param))
        arc = BASE_WHEEL * ARC_RATIO
        speed = param * MAX_WHEEL

        # Only cancel timed turn if the incoming command is intended to interrupt it
        if cmd in {"stop", "forward", "backward", "curve_left", "curve_right",
                   "spin_left", "spin_right", "speed", "lane_on", "lane_off"}:
            self._cancel_turn_timer()

        commands = {
            "forward":     lambda: self.driver.set_wheels(BASE_WHEEL, BASE_WHEEL),
            "backward":    lambda: self.driver.set_wheels(-BASE_WHEEL, -BASE_WHEEL),
            "left":        lambda: self._timed_turn(-TURN_IN_PLACE_WHEEL, TURN_IN_PLACE_WHEEL),
            "right":       lambda: self._timed_turn(TURN_IN_PLACE_WHEEL, -TURN_IN_PLACE_WHEEL),
            "curve_left":  lambda: self.driver.set_wheels(arc, BASE_WHEEL),
            "curve_right": lambda: self.driver.set_wheels(BASE_WHEEL, arc),
            "spin_left":   lambda: self.driver.set_wheels(-MAX_WHEEL, MAX_WHEEL),
            "spin_right":  lambda: self.driver.set_wheels(MAX_WHEEL, -MAX_WHEEL),
            "stop":        lambda: self.driver.stop(),
            "lane_on":     lambda: self.driver.set_lane(True),
            "lane_off":    lambda: self.driver.set_lane(False),
            "speed":       lambda: self.driver.set_wheels(speed, speed),
        }

        fn = commands.get(cmd)
        if fn:
            fn()
        else:
            log.warning(f"Unknown command: {cmd!r}")

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, pkt: dict):
        cmd = str(pkt.get("cmd", "")).lower().strip()
        param = pkt.get("param", 1.0)

        with self._lock:
            self.last_cmd_time = time.time()
            self._watchdog_stopped = False
            busy_turning = self._busy_turning

        # Ignore everything during a timed turn except stop
        if busy_turning and cmd != "stop":
            log.info(f"Ignoring command during timed turn: {cmd}")
            return

        log.info(f"CMD: {cmd}  param={param}")
        self._execute(cmd, param)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def pet_watchdog(self):
        with self._lock:
            self.last_cmd_time = time.time()
            self._watchdog_stopped = False

    def watchdog(self):
        while self._running:
            time.sleep(0.5)

            with self._lock:
                elapsed = time.time() - self.last_cmd_time
                already_stopped = self._watchdog_stopped

            if elapsed > WATCHDOG_TIMEOUT and not already_stopped:
                log.warning("Watchdog timeout — stopping motors")
                self._cancel_turn_timer()
                self.driver.stop()
                with self._lock:
                    self._watchdog_stopped = True

    # ── Server loop ───────────────────────────────────────────────────────────

    def run(self):
        threading.Thread(target=self.watchdog, daemon=True).start()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                srv.bind((self.host, self.port))
            except OSError as e:
                raise RuntimeError(
                    f"Failed to bind to {self.host}:{self.port} — port may already be in use."
                ) from e

            srv.listen(5)
            log.info(f"TCP bridge listening on {self.host}:{self.port}")

            while self._running:
                try:
                    conn, addr = srv.accept()
                    threading.Thread(
                        target=self.handle_client,
                        args=(conn, addr),
                        daemon=True,
                    ).start()

                except KeyboardInterrupt:
                    log.info("Shutting down…")
                    self._running = False
                    self._cancel_turn_timer()
                    self.driver.stop()

                except Exception:
                    log.exception("Server loop error")


if __name__ == "__main__":
    RobotServer().run()