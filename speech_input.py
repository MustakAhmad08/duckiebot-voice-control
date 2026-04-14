#!/usr/bin/env python3
"""
speech_input.py — Laptop-side speech-to-text using Azure Cognitive Services
Continuously listens for voice, transcribes, and calls a callback with the text.
Falls back to SpeechRecognition + Google STT if Azure SDK not available.
"""

import os
import queue
import threading
import logging

log = logging.getLogger(__name__)

# ─── Azure Speech SDK (preferred) ────────────────────────────────────────────
try:
    import azure.cognitiveservices.speech as speechsdk
    AZURE_SPEECH_AVAILABLE = True
except ImportError:
    AZURE_SPEECH_AVAILABLE = False
    log.warning("azure-cognitiveservices-speech not found — trying SpeechRecognition fallback")

# ─── SpeechRecognition fallback ───────────────────────────────────────────────
try:
    import speech_recognition as sr
    SR_AVAILABLE = True
except ImportError:
    SR_AVAILABLE = False

AZURE_SPEECH_KEY    = os.environ.get("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.environ.get("AZURE_SPEECH_REGION", "eastus")


class AzureSpeechListener:
    """Continuous speech recognition via Azure Cognitive Services Speech SDK."""

    def __init__(self, callback, language="en-US"):
        if not AZURE_SPEECH_AVAILABLE:
            raise RuntimeError("azure-cognitiveservices-speech not installed")
        config = speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION,
        )
        config.speech_recognition_language = language
        audio_config = speechsdk.audio.AudioConfig(use_default_microphone=True)
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=config, audio_config=audio_config
        )
        self.callback = callback
        self._running = False

    def _on_recognized(self, evt):
        text = evt.result.text.strip()
        if text:
            log.info(f"[Azure STT] Recognised: {text!r}")
            self.callback(text)

    def _on_canceled(self, evt):
        log.warning(f"[Azure STT] Canceled: {evt.result.cancellation_details}")

    def start(self):
        self._running = True
        self.recognizer.recognized.connect(self._on_recognized)
        self.recognizer.canceled.connect(self._on_canceled)
        self.recognizer.start_continuous_recognition()
        log.info("[Azure STT] Continuous recognition started")

    def stop(self):
        self.recognizer.stop_continuous_recognition()
        self._running = False
        log.info("[Azure STT] Stopped")


class FallbackSpeechListener:
    """Fallback using SpeechRecognition library + Google Web Speech API (free)."""

    def __init__(self, callback, language="en-US"):
        if not SR_AVAILABLE:
            raise RuntimeError(
                "Neither azure-cognitiveservices-speech nor SpeechRecognition is installed.\n"
                "Install with: pip install azure-cognitiveservices-speech\n"
                "         or: pip install SpeechRecognition pyaudio"
            )
        self.recognizer = sr.Recognizer()
        self.mic = sr.Microphone()
        self.callback = callback
        self.language = language
        self._thread  = None
        self._running = False

        # Calibrate for ambient noise
        with self.mic as source:
            log.info("[Fallback STT] Calibrating microphone…")
            self.recognizer.adjust_for_ambient_noise(source, duration=1.5)
            log.info("[Fallback STT] Ready")

    def _listen_loop(self):
        with self.mic as source:
            while self._running:
                try:
                    log.info("[Fallback STT] Listening…")
                    audio = self.recognizer.listen(source, timeout=5,
                                                   phrase_time_limit=6)
                    text = self.recognizer.recognize_google(
                        audio, language=self.language
                    )
                    if text:
                        log.info(f"[Fallback STT] Recognised: {text!r}")
                        self.callback(text)
                except sr.WaitTimeoutError:
                    pass
                except sr.UnknownValueError:
                    log.debug("[Fallback STT] Could not understand audio")
                except sr.RequestError as e:
                    log.warning(f"[Fallback STT] API error: {e}")

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False


def create_listener(callback, language="en-US"):
    """
    Factory: returns the best available speech listener.
    Tries Azure first, then SpeechRecognition fallback.
    """
    if AZURE_SPEECH_AVAILABLE and AZURE_SPEECH_KEY:
        log.info("Using Azure Speech SDK")
        return AzureSpeechListener(callback, language)
    elif SR_AVAILABLE:
        log.info("Using SpeechRecognition fallback (Google Web API)")
        return FallbackSpeechListener(callback, language)
    else:
        raise RuntimeError(
            "No speech recognition library available.\n"
            "pip install azure-cognitiveservices-speech\n"
            "    or\n"
            "pip install SpeechRecognition pyaudio"
        )


# ─── Quick test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO)

    def on_text(text):
        print(f">>> {text}")

    listener = create_listener(on_text)
    listener.start()
    print("Speak now. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
