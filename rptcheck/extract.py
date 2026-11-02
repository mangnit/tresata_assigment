"""S1: read director / KMP declarations (simulated OCR text of signed PDFs).

Two extractors share ONE output schema and ONE validator:
* offline: a deterministic parser for the tidy synthetic forms;
* llm: a model reads the page and returns JSON (see llm.py).
Real scans are messy, which is where the LLM earns its place; the synthetic forms
are tidy, so the offline parser is enough to exercise the rest of the pipeline.

Each extracted item carries `raw_quoted_span` (what the page says, OCR noise and
all) and `normalized_*` values produced by deterministic code. The validator
checks the span against the page with a similarity threshold, so a model that
'helpfully' fixes an OCR error in its quote is not rejected, while a quote that
is not on the page at all is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .ids import normalize_din, normalize_pan

SPAN_THRESHOLD = 0.90

INJECTION_PATTERNS = [
    r"ignore (all )?(prior|previous|above) instructions", r"\bai system\b", r"system prompt",
    r"mark (every|all) (vendor|vendors|party|parties) as", r"you are (now )?an? (ai|assistant)",
]

RULE4 = {"spouse", "father", "mother", "son", "daughter", "sons_wife", "daughters_husband",
         "brother", "sister", "huf_member"}
_REL_MAP = [  # compound relations first: "daughter's husband" must not match "husband"
    (r"son'?s wife|daughter[- ]in[- ]law", "sons_wife"),
    (r"daughter'?s husband|son[- ]in[- ]law", "daughters_husband"),
    (r"\b(wife|husband|spouse)\b", "spouse"), (r"\b(step[- ]?)?son\b", "son"),
    (r"\b(step[- ]?)?daughter\b", "daughter"), (r"\b(step[- ]?)?father\b", "father"),
    (r"\b(step[- ]?)?mother\b", "mother"), (r"\b(step[- ]?)?brother\b", "brother"),
    (r"\b(step[- ]?)?sister\b", "sister"), (r"\bhuf\b", "huf_member"),
]


def canonical_relation(text: str) -> str:
    """Map free text to Rule 4 terms. Anything not in the closed list becomes 'other:...',
    which can only ever feed the Ind AS 24 judgement rule, never a Confirmed s.2(76) hit."""
    t = re.sub(r"\s+", " ", text.lower().strip())
    m = re.fullmatch(r"(?:step[- ]?)?(\w+)'?s (\w+)", t)          # possessive: "X's Y"
    if m:
        base, rel = m.groups()
        if base == "son" and rel == "wife":
            return "sons_wife"
        if base == "daughter" and rel == "husband":
            return "daughters_husband"
        return "other:" + t                                     # wife's brother, sister's husband ...
    if re.search(r"in[- ]law", t):
        if t.startswith("daughter"):
            return "sons_wife"
        if t.startswith("son"):
            return "daughters_husband"
        return "other:" + t                                     # brother-, sister-, father-, mother-in-law
    for pat, rel in _REL_MAP:
        if re.search(pat, t):
            return rel
    return "other:" + t


def scan_injection(text: str) -> list[str]:
    return [m.group(0) for p in INJECTION_PATTERNS for m in re.finditer(p, text, flags=re.I)]


def pages(text: str) -> dict[int, str]:
    parts = re.split(r"--- page (\d+) ---\n", text)
    if len(parts) == 1:
        return {1: text}
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def validate_span(raw_span: str, page_text: str) -> tuple[bool, str, float]:
    if not raw_span:
        return False, "empty", 0.0
    if raw_span in page_text:
        return True, "exact", 1.0
    best = 0.0
    for line in page_text.splitlines():
        if not line.strip():
            continue
        # compare against the line and its pipe-separated cells
        for cand in [line] + [c.strip() for c in line.split("|")]:
            r = SequenceMatcher(None, raw_span.lower(), cand.lower()).ratio()
            best = max(best, r)
    return best >= SPAN_THRESHOLD, "fuzzy" if best >= SPAN_THRESHOLD else "rejected", round(best, 3)


@dataclass
class Interest:
    entity_name: str
    pan: str | None
    nature: str
    shareholding_pct: float | None
    raw_quoted_span: str
    page: int
    span_check: str = ""


@dataclass
class Relative:
    name: str
    relation: str          # canonical
    relation_text: str
    pan: str | None
    din: str | None
    raw_quoted_span: str
    page: int
    span_check: str = ""


@dataclass
class Declaration:
    file: str
    declarant: str
    din: str | None
    interests: list[Interest] = field(default_factory=list)
    relatives: list[Relative] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    injection_hits: list[str] = field(default_factory=list)
    extractor: str = "offline"


def _pct(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) if m else None


def _offline_items(page_no: int, text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) == 5 and re.fullmatch(r"\d+", cells[0]):
            m = re.search(r"\(PAN:\s*([A-Za-z0-9]+)\)", cells[1])
            items.append(dict(kind="interest", entity_name=re.sub(r"\s*\(PAN:.*?\)", "", cells[1]).strip(),
                              pan_raw=m.group(1) if m else "", nature=cells[2], shareholding=cells[3],
                              raw_quoted_span=line.strip(), page=page_no))
        elif len(cells) == 4 and cells[0] not in ("Name",) and not cells[0].lower().startswith("name"):
            items.append(dict(kind="relative", name=cells[0], relation_text=cells[1],
                              pan_raw="" if cells[2] in ("-", "") else cells[2],
                              din_raw="" if cells[3] in ("-", "") else cells[3],
                              raw_quoted_span=line.strip(), page=page_no))
    return items


def parse_declaration(path: Path, llm=None) -> Declaration:
    text = Path(path).read_text()
    pg = pages(text)
    m_name = re.search(r"I, ([^,]+), being a director", text) or re.search(r"PERSONNEL: (.+)", text)
    m_din = re.search(r"DIN:\s*([A-Za-z0-9]{8})", text)
    dec = Declaration(file=Path(path).name, declarant=(m_name.group(1).strip() if m_name else Path(path).stem),
                      din=normalize_din(m_din.group(1)) if m_din else None,
                      injection_hits=scan_injection(text), extractor="llm" if llm else "offline")
    raw_items = []
    for p, t in pg.items():
        raw_items += (llm.extract_declaration(p, t) if llm else _offline_items(p, t))
    for it in raw_items:
        ok, how, sim = validate_span(it.get("raw_quoted_span", ""), pg.get(it.get("page", 1), text))
        if not ok:
            dec.rejected.append({**it, "reason": f"span not on page (similarity {sim})"})
            continue
        if it["kind"] == "interest":
            dec.interests.append(Interest(it["entity_name"], normalize_pan(it.get("pan_raw", "")) if it.get("pan_raw") else None,
                                          it.get("nature", ""), _pct(it.get("shareholding", "")),
                                          it["raw_quoted_span"], it["page"], how))
        else:
            dec.relatives.append(Relative(it["name"], canonical_relation(it["relation_text"]), it["relation_text"],
                                          normalize_pan(it["pan_raw"]) if it.get("pan_raw") else None,
                                          normalize_din(it["din_raw"]) if it.get("din_raw") else None,
                                          it["raw_quoted_span"], it["page"], how))
    return dec


def parse_prior_year_note(path: Path) -> list[tuple[str, str]]:
    """Returns (name, category) pairs from the prior year Ind AS 24 note."""
    out = []
    for line in Path(path).read_text().splitlines():
        m = re.match(r"\(([a-z])\)\s*([^:]+):\s*(.+)", line)
        if not m:
            continue
        category = m.group(2).strip()
        for part in m.group(3).split(";"):
            name = re.sub(r"\s*\(.*?\)\s*", " ", part).strip()
            for n in name.split(","):
                n = n.strip()
                if n:
                    out.append((n, category))
    return out
