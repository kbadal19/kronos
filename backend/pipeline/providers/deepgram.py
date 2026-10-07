"""
Deepgram Nova-3, batch (pre-recorded) mode.

Batch rather than streaming, deliberately: it's roughly half the price,
it's more accurate because the model sees the whole recording rather than
a rolling window, and it doesn't fall over when the network does — which
is the entire point of the outbox in the phone app.

Called over plain HTTP rather than through the vendor SDK so that adding a
second provider for the bake-off is a 60-line file, not a dependency
negotiation.
"""
import logging

import httpx
from django.conf import settings

from .base import (
    EmptyTranscriptError,
    TranscriptionError,
    TranscriptionRejected,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.deepgram.com/v1/listen"

# Generous: transcription is a background task, and a slow response is
# always better than a retry that pays for the same audio twice.
TIMEOUT = httpx.Timeout(300.0, connect=15.0)


class DeepgramProvider:
    name = "deepgram"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or settings.DEEPGRAM_API_KEY
        self.model = model or settings.DEEPGRAM_MODEL
        if not self.api_key:
            raise TranscriptionError("DEEPGRAM_API_KEY is not set")

    def transcribe(self, audio_path, *, keyterms=None, language=None):
        params = {
            "model": self.model,
            # Per user (TranscriptionProfile). "multi" follows Hindi <->
            # English switches mid-sentence; each word in the response
            # carries its own detected language.
            "language": language or settings.DEEPGRAM_LANGUAGE,
            # Punctuation and paragraphing make the transcript readable on
            # its own, which matters because with audio deleted the
            # transcript is all you will ever have.
            "punctuate": "true",
            "paragraphs": "true",
            "smart_format": "true",
            # No diarization: it's a single speaker talking to himself, and
            # it costs meaningfully more per hour.
            "diarize": "false",
        }

        # The accuracy lever. Nova-3 takes repeated `keyterm` params and
        # weights those phrases during decoding; this is what keeps
        # "Project CB" from becoming "project sea bee".
        keyterms = list(keyterms or [])[: settings.TRANSCRIPTION_KEYTERM_LIMIT]

        with open(audio_path, "rb") as fh:
            audio = fh.read()

        # Biasing is a nice-to-have; the transcript is not. If Deepgram
        # rejects the keyterm list as too long (its token count is not
        # ours), halve it and go again — down to none at all.
        while True:
            attempt = {**params, "keyterm": keyterms} if keyterms else params
            response = self._post(attempt, audio)
            if response.status_code == 400 and keyterms and "keyterm" in response.text.lower():
                logger.warning(
                    "deepgram: keyterm list rejected at %d terms, retrying with %d",
                    len(keyterms), len(keyterms) // 2,
                )
                keyterms = keyterms[: len(keyterms) // 2]
                continue
            break

        if response.status_code == 429 or response.status_code >= 500:
            raise TranscriptionError(
                f"Deepgram returned {response.status_code}: {response.text[:500]}"
            )
        if response.status_code >= 400:
            raise TranscriptionRejected(
                f"Deepgram rejected the request ({response.status_code}): {response.text[:500]}"
            )

        payload = response.json()
        return self._parse(payload)

    def _post(self, params, audio):
        try:
            response = httpx.post(
                API_URL,
                params=params,
                content=audio,
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "application/octet-stream",
                },
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"Deepgram request failed: {exc}") from exc
        return response

    def _parse(self, payload):

        try:
            channel = payload["results"]["channels"][0]
            alternative = channel["alternatives"][0]
        except (KeyError, IndexError) as exc:
            raise TranscriptionError(
                f"Unexpected Deepgram response shape: {str(payload)[:500]}"
            ) from exc

        text = (alternative.get("transcript") or "").strip()
        if not text:
            raise EmptyTranscriptError(
                "Deepgram returned an empty transcript — likely a muted mic "
                "or a recording that never captured audio."
            )

        return TranscriptionResult(
            text=text,
            provider=self.name,
            model=self.model,
            confidence=alternative.get("confidence"),
            duration_seconds=payload.get("metadata", {}).get("duration"),
            words=alternative.get("words"),
        )
