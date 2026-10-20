"""
Short spoken clips that need an answer now: a question for Ask, a fact to
remember for Knowledge.

A capture goes through the outbox and Celery because nobody is waiting on
it. These are the opposite — you're looking at the screen — so it's one
synchronous listen, briefed the way normalize is (your vocabulary, your
corrections, the script rule) but with no draft to correct: a question is
a few seconds, and one Gemini call that hears it with context beats a cold
Deepgram pass plus a second call.

Deepgram is the fallback when Gemini is down or rate limited, romanized
afterwards so a Hinglish question still reads in Roman script.

The audio is never kept. It's held in memory for the request, and written
to a temp file only if the fallback needs one, deleted straight after.
"""
import logging
import os
import tempfile
from dataclasses import dataclass

import httpx
from django.conf import settings
from pydantic import BaseModel

from .normalize import MIME_TYPES, SCRIPT_RULES

logger = logging.getLogger(__name__)

# A question is seconds; this is ~20 minutes at the app's 32 kbps, and well
# under Gemini's inline request cap.
MAX_CLIP_BYTES = 5 * 1024 * 1024
VOCAB_NAMES = 300
CORRECTION_LIMIT = 60

PURPOSES = {
    "question": "asking their personal assistant a question about their own notes, people and clients",
    "teach": "telling their personal assistant something to remember about a person, company, place or project",
}

CLIP_PROMPT = """\
Transcribe this short voice clip. The speaker is {purpose}. They talk in
{languages} and may switch between them mid-sentence.
{speaker_notes}
Rules:
1. Verbatim: never rephrase, translate, summarize or answer it. Drop only
   fillers like "um" and "uh".
2. Script: {script_rule}
3. When a word sounds like a name in the vocabulary, use the vocabulary's
   spelling. Never force a vocabulary match onto something not said.
4. Apply the known corrections wherever the wrong form is heard.
5. Punctuate. A question ends with "?".
6. If nothing intelligible is said, return an empty "text".

Return JSON: {{"text": the transcript}}.
"""


class ClipError(RuntimeError):
    """Neither recognizer could be reached. Worth trying again."""


class EmptyClip(ClipError):
    """Heard, but nothing was said — a muted mic or a tap too soon."""


class _Clip(BaseModel):
    text: str


@dataclass
class ClipResult:
    text: str
    via: str  # the model that heard it


def mime_type(filename):
    return MIME_TYPES.get(os.path.splitext(filename or "")[1].lower(), "audio/mp4")


def _context(user_id):
    from graph.models import Node

    from .models import CorrectionPair

    names = []
    rows = (
        Node.objects.filter(
            user_id=user_id, kind__in=Node.SEMANTIC_KINDS,
            state=Node.State.ACTIVE, merged_into__isnull=True,
        )
        .order_by("-mention_count", "title")
        .values_list("title", "aliases")[:VOCAB_NAMES]
    )
    for title, aliases in rows:
        names.extend(n for n in (title, *(aliases or [])) if n)
    sections = []
    if names:
        sections.append("Vocabulary (names the speaker uses):\n" + ", ".join(dict.fromkeys(names)))
    pairs = CorrectionPair.objects.filter(user_id=user_id).order_by("-hits").values_list("wrong", "right")[
        :CORRECTION_LIMIT
    ]
    if pairs:
        sections.append("Known corrections (wrong -> right):\n" + "\n".join(f"- {w} -> {r}" for w, r in pairs))
    return "\n\n".join(sections)


def _gemini(user_id, audio, mime, purpose, profile):
    from .gemini import client as shared_client
    from google.genai import errors, types

    notes = profile.speaker_notes.strip()
    system = CLIP_PROMPT.format(
        purpose=PURPOSES[purpose],
        languages=profile.languages or "Hindi and English",
        speaker_notes=f"\nAbout the speaker: {notes}\n" if notes else "",
        script_rule=SCRIPT_RULES.get(profile.script, SCRIPT_RULES["roman"]),
    )
    contents = [types.Part.from_bytes(data=audio, mime_type=mime)]
    context = _context(user_id)
    if context:
        contents.append(context)

    client = shared_client()
    models = list(dict.fromkeys(m for m in (settings.GEMINI_FAST_MODEL, settings.GEMINI_SMART_MODEL) if m))
    failures = []
    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=_Clip,
                ),
            )
        except (errors.APIError, httpx.HTTPError) as exc:
            failures.append(f"{model}: {exc}")
            continue
        parsed = response.parsed
        if parsed is None:
            try:
                parsed = _Clip.model_validate_json(response.text or "")
            except ValueError:
                failures.append(f"{model}: unparseable output")
                continue
        return ClipResult(text=parsed.text.strip(), via=model)
    raise ClipError("; ".join(failures))


def _deepgram(user_id, audio, suffix, profile):
    from .keyterms import get_keyterms
    from .providers.base import EmptyTranscriptError, TranscriptionError, get_provider
    from .retrieval import romanize

    settings.AUDIO_SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="clip-", suffix=suffix, dir=settings.AUDIO_SPOOL_DIR)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)
        result = get_provider().transcribe(
            path, keyterms=get_keyterms(user_id), language=profile.deepgram_language,
        )
    except EmptyTranscriptError:
        return ClipResult(text="", via="deepgram")
    except (TranscriptionError, ValueError) as exc:
        raise ClipError(str(exc)) from exc
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    text = result.text
    if profile.script != profile.Script.NATIVE:
        text = romanize(text)
    return ClipResult(text=text.strip(), via=f"{result.provider}/{result.model}")


def transcribe_clip(user_id, audio, filename, purpose):
    """The words in a short clip, as text. Raises EmptyClip when nothing
    was said, ClipError when nothing could listen."""
    from .models import TranscriptionProfile

    if not audio:
        raise EmptyClip("No audio.")
    profile = TranscriptionProfile.for_user(user_id)

    result = None
    if settings.GEMINI_API_KEY:
        try:
            result = _gemini(user_id, audio, mime_type(filename), purpose, profile)
        except ClipError as exc:
            logger.warning("clip: gemini failed, falling back to deepgram: %s", exc)
    if result is None:
        suffix = os.path.splitext(filename or "")[1].lower()
        result = _deepgram(user_id, audio, suffix if suffix in MIME_TYPES else ".m4a", profile)

    if not result.text or result.text.lower().strip("[]. ") == "inaudible":
        raise EmptyClip("Didn't catch that.")
    return result
