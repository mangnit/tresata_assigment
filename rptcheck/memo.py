"""S7: exception memo + citation checker.

Rule: every sentence must cite at least one evidence ID, and every cited ID must
belong to the finding. A memo that fails is rejected and replaced by the template.
"""
from __future__ import annotations

import re

ID_RE = re.compile(r"\[(E\d{3,})\]")


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])|(?<=\])\s+(?=[A-Z(])", text)
    return [p.strip() for p in parts if p.strip()]


def check_citations(memo: str, allowed: set[str]) -> tuple[bool, list[str]]:
    problems = []
    for s in split_sentences(memo):
        ids = ID_RE.findall(s)
        if not ids:
            problems.append(f"no evidence ID: '{s[:60]}'")
        bad = [i for i in ids if i not in allowed]
        if bad:
            problems.append(f"unknown IDs {bad}: '{s[:60]}'")
    return (not problems), problems


def _q(s: str) -> str:
    """Quoted content without full stops, so abbreviations like 'Pvt.' cannot split a sentence."""
    return re.sub(r"\s*\|\s*", ", ", s.replace(".", "")).strip(", ")


def template_memo(f: dict, ev_lookup) -> str:
    lines = []
    path_ids = f["evidence"]
    first = path_ids[0] if path_ids else ""
    lines.append(f"Vendor {f['vendor']} ({_q(f['vendor_name'])}) was linked to {_q(f['anchor_name'])} through "
                 f"{len(f['path'])} recorded steps [{first}].")
    for step in f["path"]:
        if step["kind"] == "same_entity":
            continue
        e = step["evidence"][0]
        lines.append(f"Source {ev_lookup(e).source} shows: {_q(ev_lookup(e).content[:110])} [{e}].")
    lines.append(f"Rule {f['rule']} ({f['citation']}) applies with status {f['status'].upper()} [{path_ids[-1]}].")
    if f.get("investigation_evidence"):
        lines.append(f"The investigator found supporting text in other records [{f['investigation_evidence'][0]}].")
    return " ".join(lines)


def write_memo(f: dict, ev_lookup, llm=None) -> tuple[str, str]:
    allowed = set(f["evidence"]) | set(f.get("investigation_evidence", []))
    if llm is not None:
        try:
            evidence = [{"id": e, "content": ev_lookup(e).content[:200], "source": ev_lookup(e).source} for e in allowed]
            draft = llm.write_memo({k: f[k] for k in ("vendor_name", "rule", "citation", "status", "note")}, evidence)
            ok, problems = check_citations(draft, allowed)
            if ok:
                return draft, "llm"
            return template_memo(f, ev_lookup), f"llm memo rejected: {problems[:2]}"
        except Exception as exc:
            return template_memo(f, ev_lookup), f"llm error: {exc}"
    return template_memo(f, ev_lookup), "template"
