#!/usr/bin/env python3
"""
robot_client.py — Laptop-side TCP client that sends commands to the Duckiebot.
Handles connection, reconnection, and command queuing.
"""

import socket
import json
import threading
import queue
import time
import logging

log = logging.getLogger(__name__)

RECONNECT_DELAY = 2.0   # seconds between reconnection attempts
SEND_TIMEOUT    = 1.0


class RobotClient:
    """
    Thread-safe TCP client.
    send_command() is non-blocking — commands are queued and sent by a worker thread.
    """

    def __init__(self, host: str, port: int = 9000):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._q: queue.Queue = queue.Queue(maxsize=20)
        self._connected = threading.Event()
        self._running = True
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._send_loop, daemon=True)
        self._connector = threading.Thread(target=self._connect_loop, daemon=True)

    def start(self):
        self._connector.start()
        self._worker.start()
        log.info(f"RobotClient started — connecting to {self.host}:{self.port}")

    def send_command(self, cmd: dict):
        """Queue a command dict for sending. Drops oldest if queue is full."""
        # Always let stop through immediately
        if cmd.get("cmd") == "stop":
            try:
                self._q.put_nowait(cmd)
            except queue.Full:
                # Drain one and retry
                try: self._q.get_nowait()
                except queue.Empty: pass
                self._q.put_nowait(cmd)
        else:
            try:
                self._q.put_nowait(cmd)
            except queue.Full:
                log.debug("Queue full — command dropped")

    def send_commands(self, cmds: list[dict]):
        for c in cmds:
            self.send_command(c)

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def stop(self):
        self._running = False
        self.send_command({"cmd": "stop"})
        self._connected.clear()
        with self._lock:
            if self._sock:
                try: self._sock.close()
                except OSError: pass

    # ── Internal ─────────────────────────────────────────────────────────────

    def _connect_loop(self):
        while self._running:
            try:
                with self._lock:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5.0)
                    sock.connect((self.host, self.port))
                    sock.settimeout(SEND_TIMEOUT)
                    self._sock = sock
                self._connected.set()
                log.info(f"Connected to robot at {self.host}:{self.port}")
                # Wait until disconnected
                while self._running and self._connected.is_set():
                    time.sleep(0.5)
            except (ConnectionRefusedError, OSError, TimeoutError) as e:
                self._connected.clear()
                log.warning(f"Connection failed: {e}. Retrying in {RECONNECT_DELAY}s…")
                time.sleep(RECONNECT_DELAY)

    def _send_loop(self):
        while self._running:
            try:
                cmd = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            if not self._connected.is_set():
                log.debug(f"Not connected — dropping {cmd}")
                continue

            line = json.dumps(cmd) + "\n"
            try:
                with self._lock:
                    if self._sock:
                        self._sock.sendall(line.encode("utf-8"))
                log.debug(f"Sent: {cmd}")
            except OSError as e:
                log.warning(f"Send failed: {e}")
                self._connected.clear()
                with self._lock:
                    if self._sock:
                        try: self._sock.close()
                        except OSError: pass
                        self._sock = None
                if self._running:
                    self._retry_after_reconnect(cmd)

    def _retry_after_reconnect(self, cmd: dict):
        """Wait briefly for reconnect and retry one dropped command once."""
        deadline = time.time() + RECONNECT_DELAY + 3.0
        while self._running and time.time() < deadline:
            if self._connected.wait(timeout=0.25):
                line = json.dumps(cmd) + "\n"
                try:
                    with self._lock:
                        if self._sock:
                            self._sock.sendall(line.encode("utf-8"))
                    log.info(f"Retried after reconnect: {cmd}")
                    return
                except OSError as e:
                    log.warning(f"Retry send failed: {e}")
                    self._connected.clear()
                    with self._lock:
                        if self._sock:
                            try: self._sock.close()
                            except OSError: pass
                            self._sock = None
        log.warning(f"Dropped command after reconnect attempts: {cmd}")
