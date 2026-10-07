"""
Feeding your own vocabulary back into the recognizer.

This is the flywheel at the centre of the accuracy story: every semantic
node you accumulate becomes a keyterm on the next capture, which makes the
next transcript better, which makes the next extraction better. It costs
one indexed query per capture.

Ordering is by mention_count because the list is capped — when you have
900 entities and room for 200, the ones you talk about constantly are the
ones worth biasing for.
"""
import re

from django.conf import settings
from django.core.cache import cache

# Per user: one client's entity names must never bias another's recognizer.
CACHE_KEY = "pipeline:keyterms:v2:{user_id}"
# How many of the keyterm slots go to recently mentioned entities.
RECENT_SLOTS = 25

# Short enough that a newly-created entity helps within the hour, long
# enough that a burst of captures doesn't hammer the same query. Busted
# explicitly whenever the link stage creates a semantic node.
CACHE_TTL = 3600


def _estimated_tokens(term):
    # Deepgram doesn't publish its tokenizer, and it splits Indian names
    # ("Matrichhaya", "Ashturkar") and punctuation ("ma'am") far finer than
    # English words: a len/3 estimate put 92 names at 449 tokens when
    # Deepgram counted over 500 and failed the note. So: ~2 chars per
    # token per word, plus one for each word boundary and symbol. Erring
    # high only costs a few keyterms; erring low costs the transcription
    # (the provider then retries with fewer — see deepgram.py).
    words = re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9\s]", term)
    return sum(-(-len(w) // 2) for w in words) + len(words)


def _within_budget(terms, budget):
    kept, used = [], 0
    for term in terms:
        cost = _estimated_tokens(term)
        if used + cost > budget:
            break
        kept.append(term)
        used += cost
    return kept


def get_keyterms(user_id, limit=None):
    from graph.models import Node

    limit = limit or settings.TRANSCRIPTION_KEYTERM_LIMIT

    cache_key = CACHE_KEY.format(user_id=user_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return _within_budget(cached[:limit], settings.TRANSCRIPTION_KEYTERM_TOKEN_BUDGET)

    from .retrieval import recent_ids

    base = Node.objects.filter(
        user_id=user_id,
        kind__in=Node.SEMANTIC_KINDS,
        state=Node.State.ACTIVE,
        merged_into__isnull=True,
    )
    # Deepgram runs before any draft exists, so it can't get the per-note
    # candidates normalize gets. Next best: what you've been talking about
    # lately first, then the names you use most. The normalize pass, which
    # does see a draft, recovers whatever this misses.
    recent = recent_ids(user_id, RECENT_SLOTS)
    by_id = {str(i): (t, a) for i, t, a in base.filter(id__in=recent).values_list("id", "title", "aliases")}
    rows = [by_id[i] for i in recent if i in by_id] + list(
        base.exclude(id__in=recent).order_by("-mention_count").values_list("title", "aliases")[: limit * 2]
    )

    terms = []
    seen = set()
    for title, aliases in rows:
        for term in (title, *(aliases or [])):
            term = (term or "").strip()
            # Single characters and very long phrases both hurt: one
            # matches everything, the other never matches at all.
            if not (2 <= len(term) <= 60):
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)

    cache.set(cache_key, terms, CACHE_TTL)
    return _within_budget(terms[:limit], settings.TRANSCRIPTION_KEYTERM_TOKEN_BUDGET)


def invalidate_keyterms(user_id):
    """Call after any change to a user's semantic nodes. Also drops the
    retrieval index built from the same names."""
    from .retrieval import invalidate_index

    cache.delete(CACHE_KEY.format(user_id=user_id))
    invalidate_index(user_id)
