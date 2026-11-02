"""Name and address normalisation for Tier 2 scored matching.

Probabilistic by nature, so every score is stored on the edge it creates.
Policy decisions encoded here:
* A person name alone is never evidence (too many Rameshes in Karnataka).
  Tier 2 needs the name AND an address or contact signal.
* Company registered names are unique by law in India (Companies Act s.4(2)
  read with Incorporation Rules r.8), so an exact match of the NORMALISED
  registered name is treated as an identity key for companies only.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

HONORIFICS = {"sri", "shri", "smt", "mr", "mrs", "ms", "dr", "m/s", "ms.", "kumari", "late"}
BUSINESS_WORDS = {"traders", "trading", "enterprises", "enterprise", "services", "service", "works",
                  "agencies", "agency", "stores", "store", "consultancy", "consulting", "consultants",
                  "industries", "company", "co", "and", "sons", "brothers", "bros", "supplies",
                  "private", "limited", "llp", "the", "group", "associates"}

_SUFFIXES = [
    (r"\bpvt\b\.?", "private"), (r"\bprivate\s+ltd\b\.?", "private limited"),
    (r"\bltd\b\.?", "limited"), (r"&", " and "), (r"\bco\b\.?", "company"),
    (r"\bm/s\b\.?", " "),
]


def _basic(s: str) -> str:
    s = (s or "").lower()
    for pat, rep in _SUFFIXES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def company_key(name: str) -> str:
    """Normalised registered name, used as an identity key for companies."""
    return _basic(name)


def looks_like_company(name: str) -> bool:
    b = _basic(name)
    return b.endswith(" private limited") or b.endswith(" limited")


def person_tokens(name: str) -> list[str]:
    toks = [t for t in _basic(name).split() if t not in HONORIFICS and t not in BUSINESS_WORDS]
    return toks


def person_name_score(a: str, b: str) -> tuple[float, str]:
    """Score how well two person-ish names agree, allowing South Indian initials.

    'K Ramesh' vs 'Ramesh Kumar Krishnan': 'ramesh' matches fully, 'k' matches the
    first letter of 'krishnan'. Returns (score 0..1, note).
    """
    ta, tb = person_tokens(a), person_tokens(b)
    if not ta or not tb:
        return 0.0, "empty"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    used, got, initials = set(), 0.0, 0
    for t in short:
        best, best_j, kind = 0.0, None, ""
        for j, u in enumerate(long_):
            if j in used:
                continue
            if t == u:
                best, best_j, kind = 1.0, j, "full"
                break
            if len(t) == 1 and u.startswith(t):
                if best < 0.6:
                    best, best_j, kind = 0.6, j, "initial"
            else:
                r = SequenceMatcher(None, t, u).ratio()
                if r >= 0.85 and r > best:
                    best, best_j, kind = r, j, "spelling"
        if best_j is not None:
            used.add(best_j)
            got += best
            initials += kind == "initial"
    score = got / len(short)
    # a single shared token between long names is weak evidence
    if len(short) == 1 and len(long_) > 1:
        score *= 0.8
    return round(score, 3), f"{initials} initials"


def address_score(a: str, b: str) -> float:
    pa, pb = re.findall(r"\b\d{6}\b", a or ""), re.findall(r"\b\d{6}\b", b or "")
    pin = 1.0 if pa and pb and pa[-1] == pb[-1] else 0.0
    norm = lambda s: {t for t in _basic(s.replace("Mysore", "Mysuru")).split()
                      if len(t) > 1 and not re.fullmatch(r"\d{6}", t)}
    ta, tb = norm(a), norm(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return round(0.6 * pin + 0.4 * jac, 3)


def fuzzy_name_ratio(a: str, b: str) -> float:
    """Plain fuzzy ratio used ONLY by the baseline (sorted tokens, suffixes stripped)."""
    sa = " ".join(sorted(person_tokens(a)))
    sb = " ".join(sorted(person_tokens(b)))
    if not sa or not sb:
        return 0.0
    return round(SequenceMatcher(None, sa, sb).ratio(), 3)
