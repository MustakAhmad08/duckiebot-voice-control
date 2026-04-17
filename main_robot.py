#!/usr/bin/env python3
"""
main_robot.py — Duckiebot ROS 1 bridge entry point

Starts the TCP server that translates laptop voice commands into
official Duckietown ROS topic publishes.

Lane following and obstacle avoidance are handled by Duckietown's
own ROS nodes — no custom camera or ToF code needed.

Run on the robot:
    python3 main_robot.py
    python3 main_robot.py --port 9010
"""

import logging
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))
from motor_controller import RobotServer, ROSDriver, PORT

logging.basicConfig(
    level=logging.INFO,
    format="[ROBOT-MAIN] %(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Duckiebot TCP→ROS bridge")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"TCP port to listen on (default: {PORT})")
    args = parser.parse_args()

    log.info("=== Duckiebot TCP→ROS Bridge Starting ===")
    log.info(f"Robot name: {os.environ.get('VEHICLE_NAME', 'duckie')}")
    log.info("Lane following and obstacle avoidance: handled by Duckietown ROS nodes")

    driver = ROSDriver()
    server = RobotServer(driver=driver, port=args.port)

    try:
        server.run()
    except KeyboardInterrupt:
        log.info("Shutting down…")
    finally:
        driver.stop()


if __name__ == "__main__":
    main()