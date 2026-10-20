"""
The normalize pass: the second, context-aware listen.

Deepgram is fast and good at timing, but it hears every recording cold. It
doesn't know that "CB" is a project, that Navjyoti is a person, or that
the last five notes were all about log ingestion. In Hindi-English
code-switching that gap matters most at the joins, where the recognizer has
to guess which language it's hearing and a proper noun is easiest to lose.

So Gemini gets a second listen, this time with context:

    the audio itself         ground truth, while it still exists
    Deepgram's draft         a strong hint for timing and English
    your vocabulary          entity names and aliases from the graph
    your corrections         every wrong -> right you've fixed by hand
    your recent notes        what you've been talking about lately

and writes the NORMALIZED transcript. RAW is never touched, so a bad
normalization is always one re-run away from being replaced.

This is one well-briefed call rather than a tool-using loop: everything
worth knowing is known before the call, so a loop would only add latency
and cost.
"""
import hashlib
import logging
import os
from dataclasses import dataclass, field

import httpx
from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel

logger = logging.getLogger(__name__)

SUMMARY_CHARS = 160
CORRECTION_LIMIT = 100
RECENT_NOTE_CHARS = 600

# Gemini's inline request cap is 20 MB in total. Phone captures at 32 kbps
# m4a are ~2.4 MB per ten minutes, so the Files API path is the rare one.
INLINE_AUDIO_LIMIT = 15 * 1024 * 1024

MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".flac": "audio/flac",
}

# An output this far from the draft's length isn't a correction, it's a
# hallucination or a summary. Wide on purpose: Deepgram can drop a whole
# Hindi stretch, and Gemini legitimately recovering it makes text longer.
MIN_LENGTH_RATIO = 0.4
MAX_LENGTH_RATIO = 3.0
GUARD_MIN_CHARS = 40

SYSTEM_PROMPT = """\
You are the transcription editor for one person's private voice notes.

The speaker talks in {languages} and may switch between them freely, often
mid-sentence. You get the audio, a draft transcript from a speech
recognizer, and context about the speaker. Your job is the most accurate
verbatim transcript of the audio.
{speaker_notes}
Rules:
1. The audio is the ground truth. The draft is a hint: often wrong on
   non-English words, on proper nouns, and where the language switches —
   but its English is usually exactly right. Change a draft word only
   when the audio clearly says something different. When unsure, keep the
   draft's word.
2. Verbatim. Never rephrase, not even slightly: if the speaker says "let's
   see what happens", writing "let's see how that works" is a serious
   error. Do not translate, summarize, reorder, or improve the wording.
   Keep the speaker's grammar, repetitions, and self-corrections. Drop only
   pure fillers like "um" and "uh".
3. Script: {script_rule}
4. Proper nouns: when a word sounds like an entry in the vocabulary, use
   the vocabulary's canonical spelling. Never force a vocabulary match
   onto something the speaker didn't say.
5. Apply the known corrections wherever the wrong form appears.
6. Add punctuation, and start a new paragraph when the topic changes.
7. If a stretch is genuinely unintelligible, write [inaudible]. Never
   invent words to fill a gap.
8. The recent notes are context for what the speaker tends to talk about.
   Never copy text from them into the transcript.

Return JSON: "text" is the transcript; "corrections" lists each place you
changed a word or name relative to the draft, as {{"heard": draft form,
"written": your form}}. Skip pure punctuation and script changes.
"""

SCRIPT_RULES = {
    "roman": (
        "write English words in English. Write every other language in\n"
        "   Roman script the way it's commonly typed in chat (Hindi: \"kal meeting\n"
        "   hai\", \"abhi bhi pending hai\"). Never use a native script such as\n"
        "   Devanagari, even where the draft does. Transliterate, never\n"
        "   translate: a Hindi word stays Hindi even when it sounds like an\n"
        "   English one — और is \"aur\" (never \"or\"), तो is \"to\", में is\n"
        "   \"mein\", वो is \"wo\"."
    ),
    "native": (
        "write each word in the native script of its language (Hindi in\n"
        "   Devanagari, English in Latin), even where the draft romanized it."
    ),
}


def render_system_prompt(profile):
    notes = profile.speaker_notes.strip()
    return SYSTEM_PROMPT.format(
        languages=profile.languages or "Hindi and English",
        speaker_notes=f"\nAbout the speaker: {notes}\n" if notes else "",
        script_rule=SCRIPT_RULES.get(profile.script, SCRIPT_RULES["roman"]),
    )


TEXT_ONLY_NOTE = """\
The audio for this note is no longer available. Work from the draft alone:
fix proper nouns against the vocabulary, apply known corrections, apply the
script rule, and add punctuation. Change nothing you
can't justify from the draft and the context.
"""


TRANSIENT_PREFIX = "transient: "


class NormalizationError(RuntimeError):
    """Transient: the provider hiccupped. Retry.

    The message always starts with TRANSIENT_PREFIX, so a failed
    ExtractionRun's error says whether trying again later could help.
    """

    def __init__(self, message):
        super().__init__(TRANSIENT_PREFIX + message)


class NormalizationRejected(RuntimeError):
    """Permanent for this input: a bad request, or output that failed the
    sanity guard. Another attempt won't help; the raw transcript stands."""


class _Correction(BaseModel):
    heard: str
    written: str


class _Output(BaseModel):
    text: str
    corrections: list[_Correction]


@dataclass
class NormalizationResult:
    text: str
    corrections: list[dict]
    model: str
    prompt_hash: str
    raw_output: dict
    input_tokens: int | None = None
    output_tokens: int | None = None
    used_audio: bool = False
    # Entities retrieval offered for this note, best first.
    candidate_ids: list = field(default_factory=list)


def build_context(capture, draft):
    """What the editor should know for *this* note, as (text, retrieval).

    Never the whole knowledge base: the draft is searched for the entities
    it plausibly names (see retrieval.py), and only those — plus their
    graph neighbours and what's been mentioned lately — are described. The
    prompt stays the same size at 70 entities or 70,000.
    """
    from capture.models import Capture, Transcript
    from graph.models import Node

    from .models import CorrectionPair
    from .retrieval import _clean, retrieve, romanize, sound_key

    sections = []

    found = retrieve(capture.user_id, draft)
    nodes = {
        str(n.id): n
        for n in Node.objects.filter(pk__in=found.ids()).only("id", "title", "aliases", "kind", "summary")
    }
    vocab = []
    for hit in found.candidates:
        node = nodes.get(hit.node_id)
        if node is None:
            continue
        line = f"- {node.title}"
        if node.aliases:
            line += f" (also said as: {', '.join(node.aliases)})"
        line += f" [{node.kind}]"
        # Who or what it is — what lets the editor tell two Ayushes apart
        # or know "Kumar sir" is a person. Short by convention; never the
        # node body, which holds notes that don't belong in a prompt.
        if node.summary:
            line += f": {node.summary[:SUMMARY_CHARS]}"
        vocab.append(line)
    if vocab:
        sections.append(
            "Vocabulary likely relevant to this note (people, places, projects the "
            "speaker knows):\n" + "\n".join(vocab)
        )

    # Corrections are filtered the same way: only those whose wrong form
    # (or its sound) is in the draft, or whose right form is a candidate.
    draft_words = f" {_clean(romanize(draft))} "
    draft_keys = {sound_key(w) for w in draft_words.split()}
    titles = {n.title for n in nodes.values()}
    pairs = []
    for wrong, right in CorrectionPair.objects.filter(user_id=capture.user_id).order_by("-hits").values_list(
        "wrong", "right"
    ):
        hit = f" {_clean(wrong)} " in draft_words or (
            " " not in wrong.strip() and len(sound_key(wrong)) > 3 and sound_key(wrong) in draft_keys
        )
        if hit or right in titles:
            pairs.append((wrong, right))
        if len(pairs) >= CORRECTION_LIMIT:
            break
    if pairs:
        sections.append(
            "Known corrections (wrong -> right):\n"
            + "\n".join(f"- {wrong} -> {right}" for wrong, right in pairs)
        )

    recent = (
        Capture.objects.filter(user_id=capture.user_id, captured_at__lt=capture.captured_at)
        .exclude(pk=capture.pk)
        .prefetch_related("transcripts")
        .order_by("-captured_at")[: settings.NORMALIZE_RECENT_CONTEXT]
    )
    notes = []
    for previous in reversed(list(recent)):
        by_kind = {t.kind: t.text for t in previous.transcripts.all()}
        text = by_kind.get(Transcript.Kind.NORMALIZED) or by_kind.get(Transcript.Kind.RAW)
        if text:
            when = timezone.localtime(previous.captured_at)
            notes.append(f"[{when:%a %d %b %H:%M}] {text[:RECENT_NOTE_CHARS]}")
    if notes:
        sections.append(
            "Recent notes (topic context only; do not copy from them):\n" + "\n\n".join(notes)
        )

    when = timezone.localtime(capture.captured_at)
    sections.append(f"This note was recorded {when:%A %d %B %Y, %H:%M}.")
    return "\n\n".join(sections), found


def _mime_type(path):
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "audio/mp4")


def _guard(raw_text, text):
    if len(raw_text) < GUARD_MIN_CHARS:
        return
    ratio = len(text) / len(raw_text)
    if not MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO:
        raise NormalizationRejected(
            f"Normalized length is {ratio:.2f}x the draft; looks like a "
            "summary or a hallucination rather than a correction."
        )


def normalize_capture(capture, raw_text):
    """One Gemini call. Raises NormalizationError (retry) or
    NormalizationRejected (give up, keep raw)."""
    from .gemini import client as shared_client
    from google.genai import errors, types

    from .models import TranscriptionProfile

    if not settings.GEMINI_API_KEY:
        raise NormalizationRejected("GEMINI_API_KEY is not set")

    system_prompt = render_system_prompt(TranscriptionProfile.for_user(capture.user_id))
    context, found = build_context(capture, raw_text)
    has_audio = bool(capture.audio_path) and os.path.exists(capture.audio_path)

    prompt = f"{context}\n\nDraft transcript from the recognizer:\n{raw_text}"
    if not has_audio:
        prompt = f"{TEXT_ONLY_NOTE}\n{prompt}"

    # Overload (503) and quota (429) are per model, and both are common on
    # Gemini's newest models, so one bad minute on the primary falls
    # through to the fallback rather than costing a whole retry cycle.
    models = list(dict.fromkeys(
        m for m in (settings.NORMALIZE_MODEL, settings.NORMALIZE_FALLBACK_MODEL) if m
    ))

    client = shared_client()
    uploaded = None
    contents = []
    response = None
    failures = []

    try:
        if has_audio:
            mime = _mime_type(capture.audio_path)
            if os.path.getsize(capture.audio_path) <= INLINE_AUDIO_LIMIT:
                with open(capture.audio_path, "rb") as fh:
                    contents.append(types.Part.from_bytes(data=fh.read(), mime_type=mime))
            else:
                uploaded = client.files.upload(
                    file=capture.audio_path,
                    config=types.UploadFileConfig(mime_type=mime),
                )
                contents.append(uploaded)
        contents.append(prompt)

        for model in models:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=_Output,
                    ),
                )
                break
            except errors.ServerError as exc:
                failures.append(f"{model}: server error {exc.code}")
            except errors.ClientError as exc:
                if exc.code != 429:
                    raise NormalizationRejected(
                        f"Gemini rejected the request: {exc}"
                    ) from exc
                failures.append(f"{model}: rate limited")
            if len(failures) < len(models):
                logger.warning("normalize: %s, falling back", failures[-1])
    except errors.APIError as exc:
        raise NormalizationError(f"Gemini file upload failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise NormalizationError(f"Gemini request failed: {exc}") from exc
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                logger.warning("normalize: could not delete uploaded file %s", uploaded.name)

    if response is None:
        raise NormalizationError("; ".join(failures))

    prompt_hash = hashlib.sha256(
        f"{model}\n{system_prompt}\n{prompt}".encode()
    ).hexdigest()

    parsed = response.parsed
    if parsed is None:
        try:
            parsed = _Output.model_validate_json(response.text or "")
        except ValueError as exc:
            raise NormalizationRejected(
                f"Gemini returned unparseable output: {(response.text or '')[:300]}"
            ) from exc

    text = parsed.text.strip()
    if not text:
        raise NormalizationRejected("Gemini returned an empty transcript")
    _guard(raw_text, text)

    usage = response.usage_metadata
    output = parsed.model_dump()
    # What retrieval offered and why — the record that tells a retrieval
    # miss (right entity never offered) from a model miss (offered, ignored).
    output["retrieval"] = [
        {"node": h.node_id, "why": h.reason, "heard": h.heard, "score": round(h.score, 3)}
        for h in found.candidates
    ]
    return NormalizationResult(
        text=text,
        corrections=output["corrections"],
        model=model,
        prompt_hash=prompt_hash,
        raw_output=output,
        input_tokens=getattr(usage, "prompt_token_count", None),
        output_tokens=getattr(usage, "candidates_token_count", None),
        used_audio=has_audio,
        candidate_ids=found.ids(),
    )
