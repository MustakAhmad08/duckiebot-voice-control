#!/usr/bin/env python3
"""
motor_controller.py — TCP → ROS 1 bridge for Duckiebot
Receives JSON command packets from the laptop over TCP and publishes
to the official Duckietown ROS topics.

ROS topics used:
  /ROBOT_NAME/wheels_driver_node/wheels_cmd  (duckietown_msgs/WheelsCmdStamped)
  /ROBOT_NAME/lane_following_node/switch     (duckietown_msgs/BoolStamped)
  /ROBOT_NAME/joy_mapper_node/car_cmd        (duckietown_msgs/Twist2DStamped)

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
    from duckietown_msgs.msg import WheelsCmdStamped, BoolStamped, Twist2DStamped
    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False
    log.warning("rospy / duckietown_msgs not found — simulation mode")

# ─── Constants ────────────────────────────────────────────────────────────────
HOST             = "0.0.0.0"
PORT             = 9010               # Must match robot_client.DEFAULT_PORT
WATCHDOG_TIMEOUT = 5.0

ROBOT_NAME = os.environ.get("VEHICLE_NAME", "duckie")

# Tunables for manual driving via Twist2DStamped.
MAX_WHEEL  = 1.0
BASE_V     = float(os.environ.get("DUCKIE_BASE_V", "0.25"))
TURN_V     = float(os.environ.get("DUCKIE_TURN_V", "0.20"))
CURVE_OMEGA = float(os.environ.get("DUCKIE_CURVE_OMEGA", "2.5"))
TURN_OMEGA  = float(os.environ.get("DUCKIE_TURN_OMEGA", "4.0"))
SPIN_OMEGA  = float(os.environ.get("DUCKIE_SPIN_OMEGA", "6.0"))


# ─── ROS Publisher wrapper ────────────────────────────────────────────────────

class ROSDriver:
    """
    Publishes wheel and lane commands to the official Duckietown ROS topics.
    Falls back to simulation logging when ROS is unavailable.
    """

    def __init__(self):
        self._state_lock = threading.Lock()
        self._lane_enabled = False
        if ROS_AVAILABLE:
            rospy.init_node("tcp_bridge", anonymous=False, disable_signals=True)
            ns = f"/{ROBOT_NAME}"
            self._wheels_pub = rospy.Publisher(
                f"{ns}/wheels_driver_node/wheels_cmd",
                WheelsCmdStamped, queue_size=1)
            self._lane_pub = rospy.Publisher(
                f"{ns}/lane_following_node/switch",
                BoolStamped, queue_size=1)
            self._car_cmd_pub = rospy.Publisher(
                f"{ns}/joy_mapper_node/car_cmd",
                Twist2DStamped, queue_size=1)
            log.info(f"ROS node initialised — robot namespace: {ns}")
        else:
            self._wheels_pub = self._lane_pub = self._car_cmd_pub = None

    def set_wheels(self, left: float, right: float):
        """Publish normalised wheel speeds in [-1.0, 1.0]."""
        left  = max(-MAX_WHEEL, min(MAX_WHEEL, left))
        right = max(-MAX_WHEEL, min(MAX_WHEEL, right))
        if ROS_AVAILABLE:
            msg = WheelsCmdStamped()
            msg.header.stamp = rospy.Time.now()
            msg.vel_left  = left
            msg.vel_right = right
            self._wheels_pub.publish(msg)
        else:
            log.info(f"[SIM] wheels L={left:+.2f}  R={right:+.2f}")

    def set_car_cmd(self, v: float, omega: float):
        """Publish a car command to Duckietown's normal kinematics path."""
        if ROS_AVAILABLE:
            msg = Twist2DStamped()
            msg.header.stamp = rospy.Time.now()
            msg.v = v
            msg.omega = omega
            self._car_cmd_pub.publish(msg)
        else:
            log.info(f"[SIM] car_cmd v={v:+.2f} omega={omega:+.2f}")

    def set_lane(self, enabled: bool):
        """Enable or disable the Duckietown lane-following node."""
        with self._state_lock:
            self._lane_enabled = enabled
        if ROS_AVAILABLE:
            msg = BoolStamped()
            msg.header.stamp = rospy.Time.now()
            msg.data = enabled
            self._lane_pub.publish(msg)
        else:
            log.info(f"[SIM] lane_following={'ON' if enabled else 'OFF'}")

    def drive(self, v: float, omega: float):
        """Manual driving always takes control away from lane following."""
        if self.lane_enabled:
            self.set_lane(False)
        self.set_car_cmd(v, omega)

    @property
    def lane_enabled(self) -> bool:
        with self._state_lock:
            return self._lane_enabled

    def stop(self):
        # Disable autonomy first so it cannot immediately overwrite the stop.
        if self.lane_enabled:
            self.set_lane(False)
        self.set_car_cmd(0.0, 0.0)
        self.set_wheels(0.0, 0.0)


# ─── RobotServer ──────────────────────────────────────────────────────────────

class RobotServer:
    """TCP server that translates JSON command packets into ROS topic publishes."""

    def __init__(self, driver: ROSDriver = None, host: str = HOST, port: int = PORT):
        self.driver            = driver or ROSDriver()
        self.host              = host
        self.port              = port
        self.last_cmd_time     = time.time()
        self._client_count     = 0
        self._lock             = threading.Lock()
        self._running          = True
        self._watchdog_stopped = False

    # ── Client handler ────────────────────────────────────────────────────────

    def handle_client(self, conn, addr):
        log.info(f"Connected: {addr}")
        conn.settimeout(1.0)
        buf = ""
        with self._lock:
            self._client_count += 1
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
            should_stop = False
            with self._lock:
                self._client_count = max(0, self._client_count - 1)
                should_stop = self._client_count == 0
            if should_stop:
                self.driver.stop()
            conn.close()
            log.info(f"Disconnected: {addr}")

    # ── Command execution ─────────────────────────────────────────────────────

    def _execute(self, cmd: str, param: float):
        clamped = max(min(float(param), 1.0), -1.0)
        commands = {
            "forward":     lambda: self.driver.drive( BASE_V, 0.0),
            "backward":    lambda: self.driver.drive(-BASE_V, 0.0),
            "left":        lambda: self.driver.drive( TURN_V,  TURN_OMEGA),
            "right":       lambda: self.driver.drive( TURN_V, -TURN_OMEGA),
            "curve_left":  lambda: self.driver.drive( BASE_V,  CURVE_OMEGA),
            "curve_right": lambda: self.driver.drive( BASE_V, -CURVE_OMEGA),
            "spin_left":   lambda: self.driver.drive( 0.0,  SPIN_OMEGA),
            "spin_right":  lambda: self.driver.drive( 0.0, -SPIN_OMEGA),
            "stop":        lambda: self.driver.stop(),
            "lane_on":     lambda: self.driver.set_lane(True),
            "lane_off":    lambda: self.driver.set_lane(False),
            "speed":       lambda: self.driver.drive(clamped * BASE_V, 0.0),
        }
        fn = commands.get(cmd)
        if fn:
            fn()
        else:
            log.warning(f"Unknown command: {cmd!r}")

    # ── Dispatch ──────────────────────────────────────────────────────────────

    def _dispatch(self, pkt: dict):
        cmd   = pkt.get("cmd",   "").lower()
        param = pkt.get("param", 1.0)
        with self._lock:
            self.last_cmd_time     = time.time()
            self._watchdog_stopped = False
        log.info(f"CMD: {cmd}  param={param}")
        self._execute(cmd, param)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def pet_watchdog(self):
        with self._lock:
            self.last_cmd_time     = time.time()
            self._watchdog_stopped = False

    def watchdog(self):
        while self._running:
            time.sleep(0.5)
            with self._lock:
                elapsed         = time.time() - self.last_cmd_time
                already_stopped = self._watchdog_stopped
            if elapsed > WATCHDOG_TIMEOUT and not already_stopped:
                log.warning("Watchdog timeout — stopping motors")
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
                    f"Failed to bind to {self.host}:{self.port} — "
                    f"port may already be in use."
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
                    self.driver.stop()


if __name__ == "__main__":
    RobotServer().run()
