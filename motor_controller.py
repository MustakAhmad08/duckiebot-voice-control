#!/usr/bin/env python3
"""
motor_controller.py — TCP → ROS 1 bridge for Duckiebot
Receives JSON command packets from the laptop over TCP and publishes
to the official Duckietown ROS topics.

ROS topics used:
  /ROBOT_NAME/wheels_driver_node/wheels_cmd       (duckietown_msgs/WheelsCmdStamped)
  /ROBOT_NAME/lane_following_node/switch          (duckietown_msgs/BoolStamped)
  /ROBOT_NAME/joy_mapper_node/joystick_override   (duckietown_msgs/BoolStamped)
  /ROBOT_NAME/fsm_node/mode                       (duckietown_msgs/FSMState)
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
HOST             = "0.0.0.0"
PORT             = 9010
WATCHDOG_TIMEOUT = 5.0

ROBOT_NAME = os.environ.get("VEHICLE_NAME", "duckie")

MAX_WHEEL          = 1.0
BASE_WHEEL         = float(os.environ.get("DUCKIE_BASE_WHEEL",         "0.3"))
ARC_RATIO          = float(os.environ.get("DUCKIE_ARC_RATIO",          "0.5"))
TURN_IN_PLACE_WHEEL = float(os.environ.get("DUCKIE_TURN_IN_PLACE_WHEEL", "0.3"))
BASE_SPEED_SCALE   = float(os.environ.get("DUCKIE_BASE_SPEED_SCALE",   "1.0"))
LEFT_WHEEL_SCALE   = float(os.environ.get("DUCKIE_LEFT_WHEEL_SCALE",   "1.0"))
RIGHT_WHEEL_SCALE  = float(os.environ.get("DUCKIE_RIGHT_WHEEL_SCALE",  "1.0"))

# At TURN_IN_PLACE_WHEEL=0.3 a Duckiebot turns ~180°/s → 90° ≈ 0.5s.
# Tune with DUCKIE_TURN_90_DURATION if your robot over/undershoots.
TURN_90_DURATION = float(os.environ.get("DUCKIE_TURN_90_DURATION", "0.50"))


# ─── ROSDriver ────────────────────────────────────────────────────────────────

class ROSDriver:
    """Publishes to official Duckietown ROS topics. Sim-logs when ROS unavailable."""

    def __init__(self):
        if ROS_AVAILABLE:
            rospy.init_node("tcp_bridge", anonymous=False, disable_signals=True)
            ns = f"/{ROBOT_NAME}"
            self._wheels_pub = rospy.Publisher(
                f"{ns}/wheels_driver_node/wheels_cmd",
                WheelsCmdStamped, queue_size=1)
            self._lane_pub   = rospy.Publisher(
                f"{ns}/lane_following_node/switch",
                BoolStamped, queue_size=1)
            self._joy_pub    = rospy.Publisher(
                f"{ns}/joy_mapper_node/joystick_override",
                BoolStamped, queue_size=1)
            log.info(f"ROS node initialised — namespace: {ns}")
        else:
            self._wheels_pub = self._lane_pub = self._joy_pub = None

    def _calibrate(self, left: float, right: float):
        left  = max(-MAX_WHEEL, min(MAX_WHEEL, left  * BASE_SPEED_SCALE * LEFT_WHEEL_SCALE))
        right = max(-MAX_WHEEL, min(MAX_WHEEL, right * BASE_SPEED_SCALE * RIGHT_WHEEL_SCALE))
        return left, right

    def set_wheels(self, left: float, right: float):
        left, right = self._calibrate(float(left), float(right))
        if ROS_AVAILABLE:
            msg = WheelsCmdStamped()
            msg.header.stamp = rospy.Time.now()
            msg.vel_left  = left
            msg.vel_right = right
            self._wheels_pub.publish(msg)
        else:
            log.info(f"[SIM] wheels L={left:+.2f} R={right:+.2f}")

    def set_lane(self, enabled: bool):
        """
        Enable/disable lane following via the two topics dt-core actually listens to:

        1. joy_mapper_node/joystick_override
           - False → autonomous mode active (lane following can run)
           - True  → manual joystick control (blocks autonomy)

        2. lane_following_node/switch
           - True  → lane following node switches on
           - False → lane following node switches off

        Both must be published together. joystick_override alone does not
        start lane following; lane switch alone does not release manual control.
        FSMState is NOT published here — fsm_node/mode is read-only on most
        dt-core builds and writing to it has no effect.
        """
        if ROS_AVAILABLE:
            stamp = rospy.Time.now()
            # Step 1: release / restore joystick override
            joy = BoolStamped()
            joy.header.stamp = stamp
            joy.data = not enabled   # False = autonomous, True = manual
            self._joy_pub.publish(joy)
            # Step 2: switch lane following node on/off
            lane = BoolStamped()
            lane.header.stamp = stamp
            lane.data = enabled
            self._lane_pub.publish(lane)
        else:
            log.info(f"[SIM] lane={'ON' if enabled else 'OFF'}")

    def stop(self):
        self.set_wheels(0.0, 0.0)


# ─── RobotServer ──────────────────────────────────────────────────────────────

class RobotServer:
    """TCP server translating JSON command packets into ROS topic publishes."""

    def __init__(self, driver: ROSDriver = None, host: str = HOST, port: int = PORT):
        self.driver            = driver or ROSDriver()
        self.host              = host
        self.port              = port
        self.last_cmd_time     = time.time()
        self._running          = True
        self._watchdog_stopped = False
        # Single lock protects all turn state + timing fields
        self._lock             = threading.Lock()
        self._turn_timer       = None   # threading.Timer | None
        self._busy_turning     = False

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
            self._cancel_turn()
            self.driver.stop()
            conn.close()
            log.info(f"Disconnected: {addr}")

    # ── Timed turn helpers ────────────────────────────────────────────────────

    def _cancel_turn(self):
        """Cancel any running timed turn. Safe to call from any thread."""
        with self._lock:
            timer, self._turn_timer = self._turn_timer, None
            self._busy_turning = False
        if timer is not None:
            timer.cancel()

    def _finish_turn(self):
        """Called by threading.Timer when turn duration elapses."""
        with self._lock:
            self._turn_timer   = None
            self._busy_turning = False
        log.info("Timed turn complete — stopping")
        self.driver.stop()

    def _timed_turn(self, left: float, right: float):
        """
        FIX: acquire lock once for the entire setup sequence so there is no
        race window between setting _busy_turning and assigning _turn_timer.
        A concurrent _cancel_turn() will block until setup is complete,
        guaranteeing the timer reference is always captured before any cancel.
        """
        with self._lock:
            if self._busy_turning:
                log.info("Already turning — ignoring repeated turn command")
                return
            # Cancel any stale previous timer (shouldn't exist, but be safe)
            if self._turn_timer is not None:
                self._turn_timer.cancel()
                self._turn_timer = None

            # Set flag AND create+store timer atomically under the same lock
            self._busy_turning = True
            timer = threading.Timer(TURN_90_DURATION, self._finish_turn)
            timer.daemon = True
            self._turn_timer = timer

        # Drive wheels and start timer outside the lock (no deadlock risk)
        self.driver.set_wheels(left, right)
        timer.start()

    # ── Command execution ─────────────────────────────────────────────────────

    def _execute(self, cmd: str, param: float):
        arc = BASE_WHEEL * ARC_RATIO

        # Non-turn commands cancel any running timed turn first
        if cmd not in {"left", "right"}:
            self._cancel_turn()

        commands = {
            "forward":     lambda: self.driver.set_wheels( BASE_WHEEL,          BASE_WHEEL),
            "backward":    lambda: self.driver.set_wheels(-BASE_WHEEL,         -BASE_WHEEL),
            "left":        lambda: self._timed_turn(-TURN_IN_PLACE_WHEEL,  TURN_IN_PLACE_WHEEL),
            "right":       lambda: self._timed_turn( TURN_IN_PLACE_WHEEL, -TURN_IN_PLACE_WHEEL),
            "curve_left":  lambda: self.driver.set_wheels( arc,            BASE_WHEEL),
            "curve_right": lambda: self.driver.set_wheels( BASE_WHEEL,     arc),
            "spin_left":   lambda: self.driver.set_wheels(-BASE_WHEEL,     BASE_WHEEL),
            "spin_right":  lambda: self.driver.set_wheels( BASE_WHEEL,    -BASE_WHEEL),
            "stop":        lambda: self.driver.stop(),
            "lane_on":     lambda: self.driver.set_lane(True),
            "lane_off":    lambda: self.driver.set_lane(False),
            "speed":       lambda: self.driver.set_wheels(
                               max(-1.0, min(1.0, param)) * MAX_WHEEL,
                               max(-1.0, min(1.0, param)) * MAX_WHEEL),
        }
        fn = commands.get(cmd)
        if fn:
            fn()
        else:
            log.warning(f"Unknown command: {cmd!r}")

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, pkt: dict):
        cmd   = str(pkt.get("cmd", "")).lower().strip()
        param = pkt.get("param", 1.0)
        try:
            param = float(param)
        except (TypeError, ValueError):
            param = 1.0

        with self._lock:
            self.last_cmd_time     = time.time()
            self._watchdog_stopped = False
            busy = self._busy_turning

        # During a timed turn, only "stop" is allowed through
        if busy and cmd != "stop":
            log.info(f"Ignoring '{cmd}' — timed turn in progress")
            return

        log.info(f"CMD: {cmd}  param={param}")
        self._execute(cmd, param)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def pet_watchdog(self):
        with self._lock:
            self.last_cmd_time     = time.time()
            # FIX: also clear _watchdog_stopped so the watchdog can fire again
            # after a timed-turn timeout resets it mid-session.
            self._watchdog_stopped = False

    def watchdog(self):
        while self._running:
            time.sleep(0.5)
            with self._lock:
                elapsed         = time.time() - self.last_cmd_time
                already_stopped = self._watchdog_stopped
            if elapsed > WATCHDOG_TIMEOUT and not already_stopped:
                log.warning("Watchdog timeout — stopping motors")
                self._cancel_turn()
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
                    threading.Thread(target=self.handle_client,
                                     args=(conn, addr), daemon=True).start()
                except KeyboardInterrupt:
                    log.info("Shutting down…")
                    self._running = False
                    self._cancel_turn()
                    self.driver.stop()
                except Exception:
                    log.exception("Server loop error")


if __name__ == "__main__":
    RobotServer().run()