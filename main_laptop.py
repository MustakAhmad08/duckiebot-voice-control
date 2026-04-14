#!/usr/bin/env python3
"""
main_laptop.py — Laptop-side master controller
Pipeline: Microphone → Azure STT → GPT/Rule Parser → TCP → Duckiebot

Usage:
    python3 main_laptop.py --robot-ip 192.168.1.100

Environment variables (or set in config.py):
    AZURE_SPEECH_KEY        — Azure Cognitive Services Speech key
    AZURE_SPEECH_REGION     — e.g. "eastus"
    AZURE_OPENAI_KEY        — Azure OpenAI key
    AZURE_OPENAI_ENDPOINT   — Azure OpenAI endpoint URL
    AZURE_OPENAI_DEPLOYMENT — Deployment name, e.g. "gpt-4o"
"""

import argparse
import logging
import sys
import time
import threading
import os

# Try to load from local config.py first (put your keys there during dev)
try:
    import config  # noqa: F401  — sets os.environ inside
except ImportError:
    pass

from speech_input  import create_listener
from nlp_parser    import parse_command
from robot_client  import RobotClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("main")

MOTION_COMMANDS = {
    "forward", "backward", "left", "right",
    "spin_left", "spin_right", "curve_left", "curve_right", "speed"
}
KEEPALIVE_INTERVAL = 0.5   # resend active manual motion before robot watchdog expires


class DuckiebotController:
    """
    Orchestrates the full speech → command → robot pipeline.
    """

    def __init__(self, robot_ip: str, robot_port: int = 9000):
        self.client   = RobotClient(robot_ip, robot_port)
        self.listener = None
        self._running = True
        self._lock = threading.Lock()
        self._active_motion_cmd: dict | None = None
        self._keepalive_thread = threading.Thread(
            target=self._motion_keepalive_loop, daemon=True
        )

    def start(self):
        log.info("Starting robot client…")
        self.client.start()
        self._keepalive_thread.start()

        log.info("Starting speech listener…")
        self.listener = create_listener(self._on_speech)
        self.listener.start()

        log.info("=" * 60)
        log.info("  Duckiebot Voice Controller READY")
        log.info("  Speak commands in English.")
        log.info("  Examples: 'go forward', 'turn left', 'stop',")
        log.info("            'follow the lane', 'curve right', 'full speed'")
        log.info("=" * 60)

    def _on_speech(self, text: str):
        """Callback fired by the speech listener for each utterance."""
        log.info(f"  Voice: {text!r}")
        commands = parse_command(text)
        for cmd in commands:
            cmd_name = cmd.get("cmd", "unknown")
            if cmd_name == "unknown":
                log.info(f"  → Not understood: {text!r}")
                continue
            log.info(f"  → Command: {cmd}")
            self.client.send_command(cmd)
            self._update_active_motion(cmd)

    def _update_active_motion(self, cmd: dict):
        """Track which manual motion command should be kept alive."""
        cmd_name = cmd.get("cmd", "unknown")
        with self._lock:
            if cmd_name in MOTION_COMMANDS:
                self._active_motion_cmd = dict(cmd)
            elif cmd_name in {"stop", "lane_on", "lane_off"}:
                self._active_motion_cmd = None

    def _motion_keepalive_loop(self):
        """Re-send the active manual motion command so the robot watchdog stays satisfied."""
        while self._running:
            time.sleep(KEEPALIVE_INTERVAL)
            with self._lock:
                active_cmd = dict(self._active_motion_cmd) if self._active_motion_cmd else None
            if active_cmd:
                self.client.send_command(active_cmd)

    def run_forever(self):
        self.start()
        try:
            while self._running:
                time.sleep(1)
                status = "CONNECTED" if self.client.is_connected else "DISCONNECTED"
                log.debug(f"Robot status: {status}")
        except KeyboardInterrupt:
            log.info("Ctrl+C — shutting down…")
        finally:
            self.shutdown()

    def shutdown(self):
        self._running = False
        if self.listener:
            self.listener.stop()
        with self._lock:
            self._active_motion_cmd = None
        self.client.stop()
        log.info("Shutdown complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Duckiebot Voice Controller — drive by spoken English"
    )
    parser.add_argument(
        "--robot-ip",
        required=True,
        help="IP address of the Duckiebot (e.g. 192.168.1.100)",
    )
    parser.add_argument(
        "--robot-port",
        type=int,
        default=9000,
        help="TCP port on the robot (default: 9000)",
    )
    args = parser.parse_args()

    controller = DuckiebotController(args.robot_ip, args.robot_port)
    controller.run_forever()


if __name__ == "__main__":
    main()
