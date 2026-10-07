"""
Finding the few entities a note is about, without reading all of them.

The knowledge base grows without bound — every person, client, place and
project in a life — so no stage may send all of it to a model. Instead
each note's Deepgram draft, mistakes and all, is used as a search query,
and only the entities it plausibly mentions go to the normalize pass:

    draft --romanize--> n-grams --+-- sound-alike match   ("Starcom" ~ Stracomm)
                                  +-- spelling match      ("Navjoti" ~ Navjyoti)
                                  |
                       direct hits --> 1-hop graph neighbours (Shubham -> Stracomm)
                                  +--> recently active set (what you've been on about)

Sound-alike is the one that matters for Hinglish. The recognizer doesn't
misspell names at random; it swaps them for English words that *sound*
similar, so names are matched on a phonetic key rather than on spelling.

Prompt size is then bounded by MAX_CANDIDATES however large the graph is.
Phase 1's resolve stage (mention -> candidates -> pick) reuses `match_text`.
"""
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

MAX_DIRECT = 40
MAX_NEIGHBOURS = 20
NEIGHBOURS_PER_NODE = 4
MAX_RECENT = 15
MAX_RELATED = 10
# Cosine distance above which "related" is noise. Measured on real notes
# with gemini-embedding-001 at 768 dims: an unrelated note's nearest
# entities all sit at 0.39-0.42 (no signal), while "Begusarai mein school
# ka shoot" put Doon Public School at 0.30 and Begusarai at 0.33. Re-measure
# if the embedding model or dimensions change.
RELATED_MAX_DISTANCE = 0.36
RELATED_TEXT_CHARS = 4000
MAX_CANDIDATES = 60
RECENT_DAYS = 14

MAX_NGRAM = 4
SPELLING_CUTOFF = 88  # rapidfuzz ratio, 0-100
SOUND_CUTOFF = 0.8  # normalized Levenshtein similarity on sound keys
# Keys this short collide constantly ("sbm" is half the dictionary), so
# they only count when the spelling is close too; two letters, only exact.
TINY_KEY = 2
SHORT_KEY = 3
SHORT_KEY_SPELLING = 80
# A sound key shared by more entities than this proves nothing — only an
# exact spelling counts for it.
AMBIGUOUS_OWNERS = 3
MIN_FUZZY_CHARS = 5
# Below this a match is more likely coincidence than mention.
MIN_SCORE = 0.6

INDEX_CACHE_KEY = "pipeline:vocab-index:v1:{user_id}"
INDEX_CACHE_TTL = 3600

# Words that are never names on their own. An n-gram made only of these is
# skipped, which keeps "ki to hai" from matching anything.
STOPWORDS = frozenset(
    """
    a an and are as at be but by do for from had has have he her his i if in is it its
    me my no not of on or our she so that the their them then there they this to was
    we were what when which who will with you your yes ok okay also just like about
    hai hain ka ki ke ko se me mein main mai tha thi the to bhi aur ek wo woh ye yeh
    kya kar karna karo kiya hoga hogi raha rahi rahe ho gaya gayi abhi phir fir toh
    uska uski uske usko unka unki unke apna apni apne mera meri mere tera hum tum
    nahi nahin haan bas sab kuch koi wala wali wale liye par pe tak sahi achha acha
    baar din raat kal aaj abhi
    """.split()
)


# --- Text normalisation -----------------------------------------------------

_DEVANAGARI = re.compile(r"[ऀ-ॿ]+")


def _romanize_word(word):
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate

    roman = transliterate(word, sanscript.DEVANAGARI, sanscript.ITRANS)
    # ITRANS marks long vowels in capitals; Hindi names are usually typed
    # with long u doubled (Doon, Khushboo) and long a/i single (Patna, Shri).
    roman = roman.replace("U", "oo").replace("A", "a").replace("I", "i").lower()
    roman = re.sub(r"[^a-z]", "", roman)
    # Hindi drops the final inherent vowel ("shubhama" is said "shubham").
    if len(roman) > 3 and roman.endswith("a") and roman[-2] not in "aeiou":
        roman = roman[:-1]
    return roman


def romanize(text):
    """Devanagari words to plain Latin, everything else untouched."""
    return _DEVANAGARI.sub(lambda m: _romanize_word(m.group(0)), text)


_SOUND_RULES = [
    ("ph", "f"), ("bh", "b"), ("dh", "d"), ("th", "t"), ("gh", "g"), ("kh", "k"),
    ("jh", "j"), ("sh", "s"), ("ch", "C"), ("ck", "k"), ("ce", "se"), ("ci", "si"),
    ("q", "k"), ("x", "ks"), ("z", "j"), ("w", "v"), ("c", "k"), ("C", "c"),
]


def sound_key(text):
    """A spelling-independent key for how a name sounds in Indian English.

    Aspirates fold into their plain consonant (bh -> b), w/v and z/j merge,
    vowels after the first letter go, and doubled letters collapse. So
    "Stracomm" and "Starcom" share "strkm", and "Shubham", "Subham" and
    "shubhama" all give "sbm".
    """
    s = re.sub(r"[^a-z]", "", text.lower())
    if not s:
        return ""
    for old, new in _SOUND_RULES:
        s = s.replace(old, new)
    head, tail = s[0], re.sub(r"[aeiouh]", "", s[1:])
    key = head + tail
    return re.sub(r"(.)\1+", r"\1", key)


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


# --- The per-user index ------------------------------------------------------

def _build_index(user_id):
    """Every title and alias of every active entity, with its sound key.
    Cached per user; dropped by keyterms.invalidate_keyterms whenever the
    vocabulary changes."""
    from graph.models import Node

    rows = Node.objects.filter(
        user_id=user_id,
        kind__in=Node.SEMANTIC_KINDS,
        state=Node.State.ACTIVE,
        merged_into__isnull=True,
    ).values_list("id", "title", "aliases")

    entries = []
    for node_id, title, aliases in rows:
        for name in (title, *(aliases or [])):
            # "Ayush (video editor)": the parenthetical disambiguates for
            # people reading the list; nobody says it out loud.
            spoken = re.sub(r"\(.*?\)", "", name)
            spelled = _clean(spoken)
            if len(spelled) < 2:
                continue
            entries.append((str(node_id), spelled, sound_key(spelled)))
    return entries


def get_index(user_id):
    key = INDEX_CACHE_KEY.format(user_id=user_id)
    entries = cache.get(key)
    if entries is None:
        entries = _build_index(user_id)
        cache.set(key, entries, INDEX_CACHE_TTL)
    return entries


def invalidate_index(user_id):
    cache.delete(INDEX_CACHE_KEY.format(user_id=user_id))


# --- Matching ----------------------------------------------------------------

@dataclass
class Hit:
    node_id: str
    score: float
    reason: str
    heard: str = ""


@dataclass
class Retrieval:
    candidates: list = field(default_factory=list)  # [Hit], best first

    def ids(self):
        return [h.node_id for h in self.candidates]


def _ngrams(text):
    tokens = _clean(romanize(text)).split()
    seen = set()
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(tokens) - n + 1):
            words = tokens[i : i + n]
            # Names don't start or end with "hai" or "the": a phrase that
            # does is a sentence fragment, not a mention.
            if words[0] in STOPWORDS or words[-1] in STOPWORDS:
                continue
            phrase = " ".join(words)
            if len(phrase) < 3 or phrase in seen:
                continue
            seen.add(phrase)
            yield phrase


def match_text(user_id, text):
    """Entities the text plausibly names: {node_id: Hit}. The reusable core —
    normalize feeds it a whole draft; Phase 1's resolver will feed it one
    mention at a time.

    Tuned for precision at scale. With tens of thousands of names, ordinary
    words in a draft ("run", "plan") share sound keys with dozens of short
    names ("Runi", "Runa"); a 20k-entity benchmark let 38 such collisions
    crowd out the real match. So, like IDF in search: a sound key shared by
    many entities is not evidence, very short keys need exact spelling, and
    every sound match is scored by how close the spelling also is.
    """
    index = get_index(user_id)
    if not index or not text:
        return {}

    spellings = [e[1] for e in index]
    keys = [e[2] for e in index]
    words = [len(s.split()) for s in spellings]
    by_key, owners = {}, {}
    for i, k in enumerate(keys):
        by_key.setdefault(k, []).append(i)
        owners.setdefault(k, set()).add(index[i][0])

    def informative(k):
        return len(owners.get(k, ())) <= AMBIGUOUS_OWNERS

    hits = {}

    def offer(i, score, reason, phrase):
        node_id = index[i][0]
        if score < MIN_SCORE:
            return
        if node_id not in hits or hits[node_id].score < score:
            hits[node_id] = Hit(node_id, score, reason, phrase)

    for phrase in _ngrams(text):
        key = sound_key(phrase)
        n = len(phrase.split())

        for i in by_key.get(key, ()):
            # "nav jyoti" may be "Navjyoti", but a four-word phrase is not
            # a one-word name.
            if abs(n - words[i]) > 1:
                continue
            if phrase == spellings[i]:
                offer(i, 1.0, "named", phrase)
                continue
            close = fuzz.ratio(phrase, spellings[i])
            if len(key) <= TINY_KEY or not informative(key):
                continue
            if len(key) <= SHORT_KEY and close < SHORT_KEY_SPELLING:
                continue
            offer(i, 0.55 + 0.4 * close / 100, "sounds like", phrase)

        # Near spelling ("Navjoti"). Short words excluded: "run" is 86%
        # like "Runa" and means nothing by it.
        if len(phrase) >= MIN_FUZZY_CHARS:
            for _, score, i in process.extract(
                phrase, spellings, scorer=fuzz.ratio, score_cutoff=SPELLING_CUTOFF, limit=3
            ):
                if abs(n - words[i]) <= 1:
                    offer(i, 0.8 * score / 100, "spelled like", phrase)

        # Near sound — only for keys long and rare enough to be distinctive.
        if len(key) > SHORT_KEY + 1:
            for _, score, i in process.extract(
                key, keys, scorer=Levenshtein.normalized_similarity,
                score_cutoff=SOUND_CUTOFF, limit=3,
            ):
                if not informative(keys[i]) or abs(n - words[i]) > 1:
                    continue
                close = fuzz.ratio(phrase, spellings[i])
                if close >= 60:
                    offer(i, 0.45 * score + 0.3 * close / 100, "sounds like", phrase)

    return hits


def retrieve(user_id, text):
    """The candidate set for one note, bounded by MAX_CANDIDATES."""
    from graph.models import Edge, Node

    direct = sorted(match_text(user_id, text).values(), key=lambda h: -h.score)[:MAX_DIRECT]
    chosen = {h.node_id: h for h in direct}

    # By meaning: what the draft is *about*, even when it names nothing —
    # "Begusarai shoot on teachers' day" never says Doon, but its vector
    # lands next to Doon Public School's. Kept after the name matches and
    # capped, so it adds what they missed rather than crowding them out.
    for node_id in related_ids(user_id, text):
        if node_id not in chosen:
            chosen[node_id] = Hit(node_id, 0.6, "related")
    direct = list(chosen.values())

    # One hop out: whoever comes attached to what was named.
    if direct:
        def live(prefix):
            return {
                f"{prefix}__state": Node.State.ACTIVE,
                f"{prefix}__merged_into__isnull": True,
                f"{prefix}__kind__in": Node.SEMANTIC_KINDS,
            }

        out = Edge.objects.filter(
            state=Node.State.ACTIVE, from_node_id__in=list(chosen), **live("to_node")
        ).select_related("to_node")
        back = Edge.objects.filter(
            state=Node.State.ACTIVE, to_node_id__in=list(chosen), **live("from_node")
        ).select_related("from_node")
        linked = [(e.from_node_id, e.to_node) for e in out] + [(e.to_node_id, e.from_node) for e in back]
        linked.sort(key=lambda pair: -pair[1].mention_count)

        per_node, added = {}, 0
        for anchor, other in linked:
            oid = str(other.id)
            if added >= MAX_NEIGHBOURS:
                break
            if oid in chosen or per_node.get(anchor, 0) >= NEIGHBOURS_PER_NODE:
                continue
            per_node[anchor] = per_node.get(anchor, 0) + 1
            chosen[oid] = Hit(oid, 0.5, "linked")
            added += 1

    # What you've been talking about lately, whether or not the draft
    # caught it — the recognizer may have mangled it beyond matching.
    for node_id in recent_ids(user_id, MAX_RECENT):
        if len(chosen) >= MAX_CANDIDATES:
            break
        chosen.setdefault(node_id, Hit(node_id, 0.3, "recent"))

    ranked = sorted(chosen.values(), key=lambda h: -h.score)[:MAX_CANDIDATES]
    return Retrieval(candidates=ranked)


def related_ids(user_id, text, limit=MAX_RELATED):
    """Entities whose embedding is close to the text's. Empty — never an
    error — when embeddings aren't available: names and graph still work."""
    from django.conf import settings
    from pgvector.django import CosineDistance

    from graph.models import Embedding, Node

    from .embeddings import EmbeddingError, embed_texts

    if not settings.GEMINI_API_KEY or not text.strip():
        return []
    try:
        [vector] = embed_texts([text[:RELATED_TEXT_CHARS]], task="RETRIEVAL_QUERY")
    except EmbeddingError:
        return []
    return [
        str(i)
        for i in Embedding.objects.filter(
            node__user_id=user_id,
            node__state=Node.State.ACTIVE,
            node__merged_into__isnull=True,
            model=settings.GEMINI_EMBEDDING_MODEL,
        )
        .annotate(distance=CosineDistance("vector", vector))
        .filter(distance__lt=RELATED_MAX_DISTANCE)
        .order_by("distance")
        .values_list("node_id", flat=True)[:limit]
    ]


def recent_ids(user_id, limit):
    """Most recently mentioned first; falls back on mention_count while
    there's no history yet."""
    from graph.models import Node

    base = Node.objects.filter(
        user_id=user_id,
        kind__in=Node.SEMANTIC_KINDS,
        state=Node.State.ACTIVE,
        merged_into__isnull=True,
    )
    since = timezone.now() - timedelta(days=RECENT_DAYS)
    ids = list(
        base.filter(last_mentioned_at__gte=since)
        .order_by("-last_mentioned_at")
        .values_list("id", flat=True)[:limit]
    )
    if len(ids) < limit:
        ids += list(
            base.exclude(id__in=ids).order_by("-mention_count").values_list("id", flat=True)[: limit - len(ids)]
        )
    return [str(i) for i in ids]


def record_mentions(user_id, node_ids, text, when):
    """After a note is final: the candidates it actually names get their
    mention_count and last_mentioned_at moved, which is what keeps the
    recent set — and Deepgram's keyterms — following what you talk about."""
    from django.db.models import F

    from graph.models import Node

    lowered = f" {_clean(text)} "
    named = []
    for node in Node.objects.filter(pk__in=node_ids).only("id", "title", "aliases"):
        names = (re.sub(r"\(.*?\)", "", n) for n in (node.title, *(node.aliases or [])))
        if any(f" {_clean(n)} " in lowered for n in names if _clean(n)):
            named.append(node.id)
    if named:
        Node.objects.filter(pk__in=named).update(
            mention_count=F("mention_count") + 1, last_mentioned_at=when,
        )
    return named
