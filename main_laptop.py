#!/usr/bin/env python3

import argparse
import logging
import sys
import time
import threading

try:
    import config  # noqa: F401  - loads local env vars during development
except ImportError:
    pass

from speech_input import create_listener
from nlp_parser import parse_command
from robot_client import RobotClient

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

KEEPALIVE_INTERVAL = 0.2
RESEND_INTERVAL = 0.6   # ✅ NEW: slower resend for same command


class DuckiebotController:

    def __init__(self, robot_ip: str, robot_port: int = 9010):
        self.client = RobotClient(robot_ip, robot_port)
        self.listener = None

        self._running = True
        self._lock = threading.Lock()

        self._active_motion_cmd = None
        self._last_sent_cmd = None
        self._last_send_time = 0

        self._keepalive_thread = threading.Thread(
            target=self._motion_keepalive_loop, daemon=True
        )

    # ─────────────────────────────────────────────

    def start(self):
        log.info("Starting robot client…")
        self.client.start()

        log.info("Starting speech listener…")
        self.listener = create_listener(self._on_speech)
        self.listener.start()

        self._keepalive_thread.start()

        log.info("=" * 60)
        log.info("Duckiebot Voice Controller READY")
        log.info("=" * 60)

    # ─────────────────────────────────────────────

    def _on_speech(self, text: str):
        if not self._running:
            return

        log.info(f"Voice: {text!r}")

        try:
            commands = parse_command(text)
        except Exception as e:
            log.error(f"NLP parse failed: {e}")
            return

        for cmd in commands:
            cmd_name = cmd.get("cmd", "unknown")

            if cmd_name == "unknown":
                continue

            log.info(f"Command: {cmd}")

            if self.client.is_connected:
                self.client.send_command(cmd)

            self._update_active_motion(cmd)

    # ─────────────────────────────────────────────

    def _update_active_motion(self, cmd: dict):
        cmd_name = cmd.get("cmd", "unknown")

        with self._lock:
            if cmd_name in MOTION_COMMANDS:
                self._active_motion_cmd = dict(cmd)
                self._last_sent_cmd = None
                self._last_send_time = 0
            elif cmd_name in {"stop", "lane_on", "lane_off"}:
                self._active_motion_cmd = None
                self._last_sent_cmd = None

    # ─────────────────────────────────────────────

    def _motion_keepalive_loop(self):
        while self._running:
            time.sleep(KEEPALIVE_INTERVAL)

            with self._lock:
                cmd = self._active_motion_cmd

            if not cmd or not self.client.is_connected:
                continue

            now = time.time()

            # ✅ send immediately if new command
            if cmd != self._last_sent_cmd:
                self.client.send_command(cmd)
                self._last_sent_cmd = cmd
                self._last_send_time = now
                continue

            # ✅ resend only after interval
            if now - self._last_send_time > RESEND_INTERVAL:
                self.client.send_command(cmd)
                self._last_send_time = now

    # ─────────────────────────────────────────────

    def run_forever(self):
        self.start()

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Ctrl+C — shutting down…")
        finally:
            self.shutdown()

    # ─────────────────────────────────────────────

    def shutdown(self):
        log.info("Shutting down...")

        self._running = False

        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass

        if self.client.is_connected:
            self.client.send_command({"cmd": "stop"})

        self.client.stop()

        # ✅ ensure thread stops
        if self._keepalive_thread.is_alive():
            self._keepalive_thread.join(timeout=1)

        log.info("Shutdown complete.")


# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Duckiebot Voice Controller")
    parser.add_argument("--robot-ip", required=True)
    parser.add_argument("--robot-port", type=int, default=9010)

    args = parser.parse_args()

    controller = DuckiebotController(args.robot_ip, args.robot_port)
    controller.run_forever()


if __name__ == "__main__":
    main()
