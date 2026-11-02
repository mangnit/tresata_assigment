"""S6: bounded investigator for CANDIDATE findings only.

One loop, read only tools, hard budget (8 tool calls, 60 seconds), explicit stop
states. It can add evidence and say corroborated / refuted / inconclusive.
It can NOT confirm a related party: a human does that.

Two policies share the same harness:
* ScriptedPolicy  (offline): a fixed lookup order, no model
* LLMPolicy       (with OPENROUTER_API_KEY): ReAct style, model picks the next tool
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .graph import KnowledgeGraph
from .names import person_tokens

STOP_STATES = ("corroborated", "refuted", "inconclusive", "budget_exhausted")
LINK_WORDS = r"(proprietary concern|concern of|owned by|partner|director|promoter|chairman|relative|son|daughter|wife|husband)"


@dataclass
class Budget:
    max_calls: int = 8
    max_seconds: float = 60.0


@dataclass
class Investigation:
    vendor: str
    status: str = "inconclusive"
    steps: list[dict] = field(default_factory=list)
    new_evidence: list[str] = field(default_factory=list)
    policy: str = ""


class Tools:
    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg
        self.minutes = sorted(kg.dir.glob("board_minutes_*.txt"))
        self.decls = sorted((kg.dir / "declarations").glob("*.txt"))

    def names(self):
        return ["search_minutes", "search_declarations", "search_ledger", "lookup_registry"]

    def _grep(self, files, query: str, kind: str) -> list[dict]:
        toks = [t for t in person_tokens(query) if len(t) > 2]
        hits = []
        for f in files:
            for n, line in enumerate(f.read_text().splitlines(), start=1):
                low = line.lower()
                if toks and all(t in low for t in toks):
                    eid = self.kg.ev.add(f, f"line {n}", line.strip(), kind)
                    hits.append({"evidence": eid, "text": line.strip()})
        return hits

    def search_minutes(self, query: str):
        return self._grep(self.minutes, query, "minutes")

    def search_declarations(self, query: str):
        return self._grep(self.decls, query, "declaration")

    def search_ledger(self, vendor_id: str):
        import csv
        with open(self.kg.dir / "ledger.csv") as f:
            rows = [r for r in csv.DictReader(f) if r["vendor_id"] == vendor_id]
        return [{"count": len(rows), "total_paise": sum(int(r["amount_paise"]) for r in rows),
                 "sample_narrations": [r["narration"] for r in rows[:3]]}]

    def lookup_registry(self, name: str):
        toks = [t for t in person_tokens(name) if len(t) > 2]
        return [{"record": r.id, "name": r.name} for r in self.kg.records.values()
                if r.source == "registry" and toks and all(t in r.name.lower() for t in toks)]

    def call(self, tool: str, args: dict):
        if tool not in self.names():
            raise ValueError(f"unknown tool {tool}")
        return getattr(self, tool)(**args)


class ScriptedPolicy:
    name = "scripted (offline, no model)"

    def __init__(self, vendor_name: str, person_names: list[str]):
        core = " ".join(person_tokens(vendor_name)) or vendor_name
        plan = [("search_minutes", {"query": core}), ("search_declarations", {"query": core})]
        for p in person_names[:2]:
            plan.append(("search_minutes", {"query": p}))
        self.plan = plan

    def next(self, steps: list[dict]) -> dict:
        for s in steps:
            for h in s.get("result", []):
                if isinstance(h, dict) and "text" in h and re.search(LINK_WORDS, h["text"], re.I):
                    return {"action": "stop", "status": "corroborated", "evidence": [h["evidence"]]}
        if len(steps) < len(self.plan):
            tool, args = self.plan[len(steps)]
            return {"action": "call", "tool": tool, "args": args}
        return {"action": "stop", "status": "inconclusive", "evidence": []}


class LLMPolicy:
    name = "llm (ReAct)"

    def __init__(self, llm, candidate_text: str, tools: list[str]):
        self.llm, self.candidate, self.tools = llm, candidate_text, tools

    def next(self, steps: list[dict], left: int = 8) -> dict:
        slim = [{"tool": s["tool"], "args": s["args"], "result": str(s["result"])[:400]} for s in steps]
        out = self.llm.next_action(self.candidate, self.tools, slim, left)
        if out.get("action") == "stop":
            status = out.get("status") if out.get("status") in ("corroborated", "refuted", "inconclusive") else "inconclusive"
            # quotes must come from tool results we actually returned
            seen = {h["evidence"]: h["text"] for s in steps for h in s["result"] if isinstance(h, dict) and "evidence" in h}
            ev = [e for e, t in seen.items() if any(q and q[:40].lower() in t.lower() for q in out.get("evidence_quotes", []))]
            if status == "corroborated" and not ev:
                status = "inconclusive"   # no grounded quote, no corroboration
            return {"action": "stop", "status": status, "evidence": ev}
        return out


def investigate(kg: KnowledgeGraph, vendor: str, policy, budget: Budget = Budget()) -> Investigation:
    tools = Tools(kg)
    inv = Investigation(vendor, policy=policy.name)
    t0 = time.time()
    while True:
        if len(inv.steps) >= budget.max_calls or time.time() - t0 > budget.max_seconds:
            inv.status = "budget_exhausted"
            break
        try:
            act = policy.next(inv.steps, budget.max_calls - len(inv.steps)) if isinstance(policy, LLMPolicy) else policy.next(inv.steps)
        except Exception as exc:
            inv.status = "inconclusive"
            inv.steps.append({"tool": "policy_error", "args": {}, "result": [str(exc)]})
            break
        if act.get("action") == "stop":
            inv.status = act["status"]
            inv.new_evidence = act.get("evidence", [])
            break
        try:
            res = tools.call(act["tool"], act.get("args", {}))
        except Exception as exc:
            res = [f"tool error: {exc}"]
        inv.steps.append({"tool": act["tool"], "args": act.get("args", {}), "result": res})
    return inv
