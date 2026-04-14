#!/usr/bin/env python3
"""
main_robot.py — Duckiebot main entry point
Starts all robot-side subsystems:
  1. Motor controller TCP server (receives commands from laptop)
  2. Lane follower (camera-based autonomy)
  3. Obstacle avoider (ToF safety layer)

Run on the robot with:
    python3 main_robot.py
"""

import threading
import logging
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))
from motor_controller  import RobotServer, MotorDriver, PORT
from obstacle_avoidance import ObstacleAvoider

logging.basicConfig(
    level=logging.INFO,
    format="[ROBOT-MAIN] %(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


class SafetyDriver:
    """Expose a MotorDriver-like interface while routing motion through ToF safety."""

    def __init__(self, raw_driver: MotorDriver, avoider: ObstacleAvoider):
        self._raw_driver = raw_driver
        self._avoider = avoider

    def set(self, left_speed: int, right_speed: int):
        self._avoider.safe_set(left_speed, right_speed)

    def stop(self):
        # FIX: was calling _raw_driver.stop() directly, bypassing ObstacleAvoider.
        # Route through safe_set(0,0) so there is exactly one stop code path.
        self._avoider.safe_set(0, 0)


def main():
    parser = argparse.ArgumentParser(
        description="Duckiebot robot-side server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=PORT,
        help="TCP port to listen on (default: 9000)",
    )
    parser.add_argument(
        "--lane-status-port",
        type=int,
        default=9001,
        help="TCP port for lane follower status updates (default: 9001)",
    )
    args = parser.parse_args()

    if args.lane_status_port == args.port:
        parser.error("--lane-status-port must be different from --port")

    log.info("=== Duckiebot System Starting ===")

    # Shared motor driver for every subsystem
    raw_driver = MotorDriver()

    # Safety wrapper (ToF obstacle avoidance)
    avoider = ObstacleAvoider(raw_driver)
    avoider.start()
    driver = SafetyDriver(raw_driver, avoider)
    server = RobotServer(driver=driver, port=args.port)

    follower = None
    lane_available = False
    try:
        from lane_follower import LaneFollower
        follower = LaneFollower(driver, status_port=args.lane_status_port,
                                watchdog_pet=server.pet_watchdog)   # FIX #2: wire watchdog pet
        follower.start_status_server()
        lane_thread = threading.Thread(target=follower.run, daemon=True)
        lane_thread.start()
        lane_available = True
        log.info("Lane follower started")
    except ModuleNotFoundError as e:
        log.warning(f"Lane follower disabled: missing dependency ({e})")
    except Exception as e:
        log.warning(f"Lane follower disabled: startup failed ({e})")

    # Monkey-patch server dispatch to also handle lane commands.
    # FIX: lane_on / lane_off bypassed _dispatch entirely, so
    # last_cmd_time was never updated → watchdog fired ~2 s after any
    # lane command and killed the motors mid-autonomous-drive.
    # Now we explicitly reset last_cmd_time inside the patch.
    original_dispatch = server._dispatch

    def extended_dispatch(pkt):
        cmd = pkt.get("cmd", "").lower()
        if cmd in ("lane_on", "lane_off"):
            # FIX: was duplicating pet_watchdog() logic inline — just call it directly.
            server.pet_watchdog()
            if cmd == "lane_on":
                if follower and lane_available:
                    follower.enable()
                    log.info("Lane following enabled via voice command")
                else:
                    log.warning("Lane following requested but unavailable on this robot")
            else:
                if follower and lane_available:
                    follower.disable()
                    log.info("Lane following disabled via voice command")
        else:
            original_dispatch(pkt)

    server._dispatch = extended_dispatch

    log.info(f"All subsystems running. Waiting for laptop connection on port {args.port}…")
    try:
        server.run()
    except KeyboardInterrupt:
        log.info("Shutting down…")
        if follower:
            follower.stop()
        avoider.stop()
        raw_driver.stop()


if __name__ == "__main__":
    main()
