#!/usr/bin/env python3
"""
nlp_parser.py — Laptop-side natural language command interpreter
Uses Azure OpenAI GPT to parse free-form spoken English into robot commands.
Falls back to a fast rule-based parser for low-latency simple commands.
"""

import json
import re
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ─── Try to import Azure OpenAI ───────────────────────────────────────────────
try:
    from openai import AzureOpenAI
    AZURE_AVAILABLE = True
except ImportError:
    AZURE_AVAILABLE = False
    log.warning("openai package not found — using rule-based parser only")

# ─── Azure configuration (set via environment or config.py) ──────────────────
AZURE_ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_API_KEY     = os.environ.get("AZURE_OPENAI_KEY", "")
AZURE_DEPLOYMENT  = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
AZURE_API_VERSION = "2024-02-01"

SYSTEM_PROMPT = """You are a robot driving assistant. Convert spoken English commands into 
JSON robot control commands. Respond ONLY with valid JSON, no explanation.

Valid commands and their JSON:
- "go forward" / "move ahead" / "drive" → {"cmd": "forward"}
- "go back" / "reverse" / "backward" → {"cmd": "backward"}
- "turn left" → {"cmd": "left"}
- "turn right" → {"cmd": "right"}
- "stop" / "halt" / "freeze" / "brake" → {"cmd": "stop"}
- "spin left" / "rotate left" → {"cmd": "spin_left"}
- "spin right" / "rotate right" → {"cmd": "spin_right"}
- "curve left" / "veer left" / "bear left" → {"cmd": "curve_left"}
- "curve right" / "veer right" / "bear right" → {"cmd": "curve_right"}
- "speed up" / "faster" / "full speed" → {"cmd": "speed", "param": 1.0}
- "slow down" / "slower" / "half speed" → {"cmd": "speed", "param": 0.5}
- "follow the lane" / "auto drive" / "autonomous" → {"cmd": "lane_on"}
- "manual" / "take control" / "manual mode" → {"cmd": "lane_off"}
- "how far" / "distance" → {"cmd": "query_distance"}

If multiple commands in one sentence, return an array: [{"cmd":...}, {"cmd":...}]
If unclear or unrelated to driving, return: {"cmd": "unknown"}
"""


# ─── Rule-based fast parser (no API call needed) ─────────────────────────────

# FIX: Reordered rules so more-specific multi-word patterns (spin, curve, veer,
# bear, rotate) are checked BEFORE the generic bare "left"/"right" patterns.
# Previously "spin left" matched \b(turn\s+)?left\b and returned "left" instead
# of "spin_left", because "left" appears in the string and the pattern is optional
# on "turn\s+".
RULES = [
    # FIX: removed redundant "stop" entry — rule_parse() already handles stop
    # via the dedicated early-return check above, so this entry was dead code.
    # ↓ specific compound-direction patterns first
    (r"\bgo\s+back\b",                                         "backward"),
    (r"\b(spin|rotate)\s+left\b",                              "spin_left"),
    (r"\b(spin|rotate)\s+right\b",                             "spin_right"),
    (r"\b(curve|veer|bear|drift)\s+left\b",                    "curve_left"),
    (r"\b(curve|veer|bear|drift)\s+right\b",                   "curve_right"),
    # ↓ generic single-word directions after
    (r"\b(forward|ahead|go|drive|straight|proceed)\b",         "forward"),
    (r"\b(back(ward)?|reverse|behind)\b",                      "backward"),
    (r"\b(turn\s+)?left\b",                                    "left"),
    (r"\b(turn\s+)?right\b",                                   "right"),
    (r"\b(faster|speed up|full speed|floor it|punch it)\b",    "speed"),
    (r"\b(slower|slow down|half speed|easy|careful)\b",        "speed_half"),
    (r"\b(auto|autonomous|lane follow|follow the lane)\b",     "lane_on"),
    (r"\b(manual|take control|i.ll drive|override)\b",         "lane_off"),
]

SPEED_MAP = {
    "speed":      {"cmd": "speed", "param": 1.0},
    "speed_half": {"cmd": "speed", "param": 0.5},
}


def rule_parse(text: str) -> Optional[dict]:
    """Fast regex-based parsing. Returns command dict or None if no match."""
    text = text.lower().strip()
    # Priority: stop always wins
    if re.search(r"\b(stop|halt|freeze|brake|emergency)\b", text):
        return {"cmd": "stop"}
    for pattern, cmd in RULES[1:]:
        if re.search(pattern, text):
            return SPEED_MAP.get(cmd, {"cmd": cmd})
    return None


# ─── GPT-based parser ─────────────────────────────────────────────────────────

class GPTParser:
    def __init__(self):
        if AZURE_AVAILABLE and AZURE_ENDPOINT and AZURE_API_KEY:
            self.client = AzureOpenAI(
                azure_endpoint=AZURE_ENDPOINT,
                api_key=AZURE_API_KEY,
                api_version=AZURE_API_VERSION,
            )
            self.enabled = True
            log.info("Azure OpenAI GPT parser initialised")
        else:
            self.client  = None
            self.enabled = False
            log.info("GPT parser disabled — rule-based only")

    def parse(self, text: str) -> list[dict]:
        """
        Returns a list of command dicts parsed from text.
        Tries rule-based first, falls back to GPT for complex sentences.
        """
        # Fast path
        simple = rule_parse(text)
        if simple:
            return [simple]

        # GPT path
        if self.enabled:
            try:
                resp = self.client.chat.completions.create(
                    model=AZURE_DEPLOYMENT,
                    messages=[
                        {"role": "system",  "content": SYSTEM_PROMPT},
                        {"role": "user",    "content": text},
                    ],
                    max_tokens=150,
                    temperature=0.0,
                )
                raw = resp.choices[0].message.content.strip()
                log.debug(f"GPT raw: {raw}")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return [parsed]
                if isinstance(parsed, list):
                    return parsed
            except Exception as e:
                log.warning(f"GPT parse error: {e}")

        log.info(f"No command understood: {text!r}")
        return [{"cmd": "unknown"}]


# ─── Module-level singleton ───────────────────────────────────────────────────
_parser: GPTParser | None = None

def parse_command(text: str) -> list[dict]:
    """Convenience function: parse text → list of command dicts.
    Owns the singleton directly — get_parser() indirection removed as redundant.
    """
    global _parser
    if _parser is None:
        _parser = GPTParser()
    return _parser.parse(text)


# ─── Quick self-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        "go forward",
        "stop the robot now",
        "curve to the right a bit",
        "turn left at the intersection",
        "spin left",
        "rotate right",
        "activate autonomous lane following mode",
        "reverse slowly",
        "go fast",
        "what is the capital of France?",
    ]
    for t in tests:
        result = parse_command(t)
        print(f"  {t!r:50s} → {result}")