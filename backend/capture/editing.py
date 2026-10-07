"""
Turning a hand edit into something the pipeline learns from.

When you fix a transcript, the words you changed are exactly the words the
recognizer and the normalize pass keep getting wrong for you. This module
diffs what you were shown against what you saved and proposes each small
replacement as a CorrectionPair. The app asks before saving any of them:
an edit can also be a rewording you'd never want applied to every future
note, and only you know which is which.
"""
import difflib
import re

# A correction is a word or a short name, not a rewritten sentence. Longer
# replacements are edits about meaning, and would misfire as find-and-replace.
MAX_SPAN_WORDS = 4

_EDGE_PUNCT = re.compile(r"^[^\w]+|[^\w]+$")


def _words(text):
    return text.split()


def _clean(tokens):
    return " ".join(_EDGE_PUNCT.sub("", t) for t in tokens).strip()


def suggest_corrections(before, after):
    """[(wrong, right), ...] for each short replacement between two texts.

    Pure insertions and deletions are skipped — there's no "wrong" form to
    watch for. So are changes that only touch punctuation.
    """
    a, b = _words(before), _words(after)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    pairs = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op != "replace":
            continue
        if (i2 - i1) > MAX_SPAN_WORDS or (j2 - j1) > MAX_SPAN_WORDS:
            continue
        wrong, right = _clean(a[i1:i2]), _clean(b[j1:j2])
        if not wrong or not right or wrong == right:
            continue
        pairs.append((wrong, right))

    return list(dict.fromkeys(pairs))


def looks_like_a_name(right):
    """Pre-tick suggestions whose fix is capitalized — "Stracomm",
    "Credence" — and leave lowercase ones for you to decide: "or -> aur"
    is right in one sentence and wrong in the next."""
    return any(c.isupper() for c in right)
