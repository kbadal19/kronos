"""
One Gemini client per process, reused by every call.

A fresh `genai.Client` per call opens a fresh HTTPS connection: DNS, TCP
and TLS to Google before the first byte of the prompt, ~250ms measured
from here. Ask makes three or four calls in a row, so that was most of a
second spent on handshakes. The client's connection pool is thread-safe,
so gunicorn's threads share it.
"""
import threading

from django.conf import settings

_lock = threading.Lock()
_cached = (None, None, None)  # (Client class, api key, client)


def client():
    from google import genai

    global _cached
    cls, key = genai.Client, settings.GEMINI_API_KEY
    # Keyed on the class too, so a test that patches genai.Client gets
    # its mock rather than a client cached by an earlier test.
    if _cached[0] is not cls or _cached[1] != key:
        with _lock:
            if _cached[0] is not cls or _cached[1] != key:
                _cached = (cls, key, cls(api_key=key))
    return _cached[2]
