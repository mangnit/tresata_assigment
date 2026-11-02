"""Fixed pipeline S1 to S7. Not a free roaming agent: the audit procedure is fixed,
so the control flow is fixed. Output = findings for human review (S8)."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import networkx as nx

from .agent import Budget, LLMPolicy, ScriptedPolicy, investigate
from .calc import approval_checks, fmt
from .extract import scan_injection
from .graph import KnowledgeGraph
from .memo import write_memo
from .rules import RULES, RuleEngine

STATUS_ORDER = {"confirmed": 0, "exception": 1, "candidate": 2, "red_flag": 3}


def _tier1_groups(kg: KnowledgeGraph, exclude_bank: bool) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(kg.records)
    for u, v, d in kg.ident.edges(data=True):
        if d["tier"] == 1 and not (exclude_bank and d["method"] == "bank"):
            g.add_edge(u, v)
    return g


def run(data_dir: Path, out_dir: Path | None = None, llm=None, investigate_candidates: bool = True) -> dict:
    data_dir = Path(data_dir)
    kg = KnowledgeGraph(data_dir, llm=llm).build()
    eng = RuleEngine(kg)
    report_date = date.fromisoformat(kg.company["report_date"])
    vendors = [r.id for r in kg.records.values() if r.kind == "vendor"]

    hits_by_vendor = {v: eng.evaluate(v) for v in vendors}
    for h in eng.conduit_pass(hits_by_vendor):
        hits_by_vendor[h.vendor].append(h)

    strict = _tier1_groups(kg, exclude_bank=True)
    findings = []
    for v, hits in hits_by_vendor.items():
        if not hits:
            continue
        vrec = kg.records[v]
        known = any(kg.records[r].attrs.get("mgmt_list") for r in nx.node_connected_component(strict, v))
        party_listed = kg.in_management_list(kg.cluster(v))
        for h in sorted(hits, key=lambda h: STATUS_ORDER[h.status]):
            rdef = RULES[h.rule]
            stale = [e for e in h.evidence if kg.ev.get(e).is_stale(report_date)]
            f = dict(vendor=v, vendor_name=vrec.name, rule=h.rule, citation=rdef.citation, rule_text=rdef.text,
                     rule_version=rdef.version, list_type=rdef.list_type, status=h.status, note=h.note,
                     anchor=h.anchor, anchor_name=kg.records[h.anchor].name if h.anchor in kg.records else h.anchor,
                     path=h.path, evidence=h.evidence, known_to_management=known, party_in_management_list=party_listed,
                     stale_evidence=stale, investigation=None, investigation_evidence=[])
            if h.status in ("confirmed", "exception") and not known:
                f["exception"] = ("TRANSACTIONS_WITH_LISTED_PARTY_UNDER_OTHER_NAME" if party_listed and h.rule != "SA550_PRIOR_YEAR"
                                  else "PRIOR_YEAR_PARTY_MISSING" if h.rule == "SA550_PRIOR_YEAR" else "UNDISCLOSED_RELATED_PARTY")
            elif h.status == "candidate":
                f["exception"] = "JUDGEMENT_IND_AS_24" if rdef.list_type == "judgement" else "CANDIDATE_RELATED_PARTY"
            elif h.status == "red_flag":
                f["exception"] = "RED_FLAG_POSSIBLE_CONDUIT"
            else:
                f["exception"] = "KNOWN_RELATED_PARTY"
            findings.append(f)

    # S6: bounded investigation of candidates and red flags only
    if investigate_candidates:
        for f in findings:
            if f["status"] not in ("candidate", "red_flag"):
                continue
            people = [kg.records[s["dst"]].name for s in f["path"] if s["kind"] in ("holds_office", "relative_of")
                      and s["dst"] in kg.records and kg.records[s["dst"]].kind == "person"]
            people += [kg.records[s["src"]].name for s in f["path"] if s["kind"] == "holds_office" and s["src"] in kg.records]
            if llm is not None:
                pol = LLMPolicy(llm, f"{f['vendor_name']} may be linked to {f['anchor_name']} ({f['note']})",
                                ["search_minutes(query)", "search_declarations(query)", "search_ledger(vendor_id)", "lookup_registry(name)"])
            else:
                pol = ScriptedPolicy(f["vendor_name"], list(dict.fromkeys(people)))
            inv = investigate(kg, f["vendor"], pol, Budget())
            f["investigation"] = {"status": inv.status, "policy": inv.policy, "tool_calls": len(inv.steps),
                                  "steps": [{"tool": s["tool"], "args": s["args"], "hits": len(s["result"])} for s in inv.steps]}
            f["investigation_evidence"] = inv.new_evidence

    # S5: calculators per related party entity (vendor records merged by exact keys)
    approvals = list(csv.DictReader(open(data_dir / "approvals.csv")))
    ledger = list(csv.DictReader(open(data_dir / "ledger.csv")))
    calc_results, done = [], set()
    for f in findings:
        if f["status"] != "confirmed" or f["vendor"] in done:
            continue
        group = sorted(r for r in nx.node_connected_component(strict, f["vendor"]) if kg.records[r].kind == "vendor")
        done.update(group)
        cl = kg.cluster(f["vendor"])
        pans = {kg.records[r].pan for r in cl if kg.records[r].pan}
        names = {kg.records[r].name.lower() for r in cl}
        apps = [a for a in approvals if a["party_pan"] in pans or a["party_name"].lower() in names]
        txns = [t for t in ledger if t["vendor_id"] in group]
        wos = any(e.attrs.get("holding_pct") == 100 and e.attrs.get("accounts_consolidated")
                  for e in kg.rel_from(cl, "group_entity"))
        items, summary = approval_checks(txns, apps, kg.company, exempt_wos=wos)
        calc_results.append({"vendors": group, "party": kg.display_name(cl), "total": fmt(summary["total_paise"]),
                             **summary, "approvals_found": [a["approval_id"] for a in apps],
                             "exceptions": [i.__dict__ for i in items]})

    # S7: memos
    for f in findings:
        f["memo"], f["memo_source"] = write_memo(f, kg.ev.get, llm=llm)

    doc_flags = []
    for p in list(data_dir.glob("*.txt")) + list((data_dir / "declarations").glob("*.txt")):
        hits = scan_injection(p.read_text())
        if hits:
            doc_flags.append({"file": p.name, "flag": "possible prompt injection", "matches": hits,
                              "handling": "document still parsed by code; text passed to any model only as data; flagged for human"})

    findings.sort(key=lambda f: (STATUS_ORDER[f["status"]], f["vendor"]))
    result = {
        "client": kg.company["name"], "period": kg.company["fy_label"], "report_date": kg.company["report_date"],
        "synthetic_data": kg.company.get("synthetic", False),
        "model": getattr(llm, "model", None) or "none (offline: Tier 3 abstains, investigator scripted, memos templated)",
        "rule_versions": {k: {"citation": r.citation, "version": r.version, "effective_from": r.effective_from} for k, r in RULES.items()},
        "input_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(data_dir.glob("*.*"))},
        "coverage": {"vendors_checked": len(vendors),
                     "vendors_without_pan_or_gstin": sum(1 for v in vendors if not kg.records[v].pan)},
        "findings": findings, "calculations": calc_results, "document_flags": doc_flags,
        "conflicts": kg.conflicts, "not_counted_outside_period_or_threshold": eng.outside_period,
        "extraction_rejections": [l for l in kg.log if "rejected" in l], "tier3_log": [l for l in kg.log if l["stage"] == "S2-tier3"],
        "evidence": kg.ev.as_dicts(),
    }
    if out_dir:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "findings.json").write_text(json.dumps(result, indent=2, default=str))
        with open(out_dir / "findings.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["vendor", "vendor_name", "exception", "status", "rule", "citation", "rule_version", "path",
                        "evidence_ids", "stale_evidence", "ai_role", "investigation", "reviewer_decision", "reviewer_name", "reason"])
            for f in findings:
                path = " > ".join(f"{s['kind']}({s.get('method', s.get('provenance', ''))}{', tier ' + str(s['tier']) if 'tier' in s else ''})"
                                  for s in f["path"])
                ai = "tier3 identity" if any(s.get("tier") == 3 for s in f["path"]) else "none"
                if f["memo_source"] == "llm":
                    ai += "; memo"
                w.writerow([f["vendor"], f["vendor_name"], f["exception"], f["status"], f["rule"], f["citation"], f["rule_version"],
                            path, " ".join(f["evidence"]), " ".join(f["stale_evidence"]), ai,
                            (f["investigation"] or {}).get("status", ""), "", "", ""])
        from .report import write_html
        write_html(result, out_dir / "review.html")
    return result
