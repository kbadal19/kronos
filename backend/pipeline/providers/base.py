"""
The transcription seam.

Provider choice is config-driven from day one because you should not pick
one from a benchmark table — you should record twenty minutes of your own
voice (the technical rambling, the mumbling, the 4am half-sentences) and
run it through two or three before committing. Swapping afterwards is then
an env var, not a refactor.

Every provider takes the same `keyterms` list: your own entity names and
aliases, pulled fresh from Postgres before each call. Within a language
that biasing matters far more than the choice of model — the words that
break are "Project CB", "Credence", "RCA", not ordinary vocabulary.
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TranscriptionResult:
    text: str
    provider: str
    model: str
    confidence: float | None = None
    duration_seconds: float | None = None
    # Word-level timings when the provider returns them. Useless in Phase 0
    # but free to keep — and with the audio deleted, unrecoverable later.
    words: list[dict] | None = field(default=None)


class TranscriptionError(RuntimeError):
    """Raised for anything the caller should retry.

    Deliberately distinct from a bad-audio failure: the pipeline retries
    this, but a capture that transcribes to nothing is a dead end that
    needs your attention rather than another three attempts.
    """


class TranscriptionRejected(TranscriptionError):
    """The provider refused the request itself (a 4xx other than rate
    limiting). Sending the same request again gets the same answer, so the
    pipeline fails it once, with this message, instead of retrying."""


class EmptyTranscriptError(TranscriptionError):
    """The provider succeeded but returned no words.

    Usually a muted mic or a recording that never started. Worth surfacing
    loudly while the audio retention window is still open, because that's
    the only period in which it can be retried at all.
    """


class TranscriptionProvider(Protocol):
    name: str

    def transcribe(
        self,
        audio_path: str,
        *,
        keyterms: list[str] | None = None,
        language: str | None = None,
    ) -> TranscriptionResult:
        ...


def get_provider(name: str | None = None) -> TranscriptionProvider:
    """Resolve the configured provider. Imports are local so an unused
    provider's dependencies never have to be installed."""
    from django.conf import settings

    name = name or settings.TRANSCRIPTION_PROVIDER

    if name == "deepgram":
        from .deepgram import DeepgramProvider

        return DeepgramProvider()

    raise ValueError(
        f"Unknown transcription provider {name!r}. "
        "Add it in pipeline/providers/ and wire it up here."
    )
