"""Evaluate the pipeline on the labelled SYNTHETIC cases and compare with two baselines.

    python eval.py            # offline
    python eval.py --llm      # with an OpenRouter model (needs OPENROUTER_API_KEY)

Baselines
  B0 'accept management's list'   : what SA 550 warns against, and what is common in practice
  B1 'fuzzy name match'           : every vendor name vs every name the system can see, ratio >= 0.85
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

from rptcheck.calc import CR, lodr_materiality
from rptcheck.names import company_key, fuzzy_name_ratio
from rptcheck.pipeline import run

DATA = Path("data/kaveri")
RANK = {"confirmed": 0, "exception": 1, "candidate": 2, "red_flag": 3}


def best_status(findings, vid):
    st = [f["status"] for f in findings if f["vendor"] == vid]
    return min(st, key=RANK.get) if st else "none"


def signature(res) -> str:
    rows = sorted((f["vendor"], f["rule"], f["status"]) for f in res["findings"])
    calc = sorted((tuple(c["vendors"]), x["code"], x["amount_paise"]) for c in res["calculations"] for x in c["exceptions"])
    return hashlib.sha256(json.dumps([rows, calc]).encode()).hexdigest()[:16]


def baseline_names(data: Path) -> list[str]:
    names = [r["name"] for r in csv.DictReader(open(data / "register_directors_kmp.csv"))]
    names += [r["name"] for r in csv.DictReader(open(data / "management_rp_list.csv"))]
    reg = json.loads((data / "registry_snapshot.json").read_text())
    names += [c["name"] for c in reg["companies"].values()] + [d["name"] for d in reg["directors"].values()]
    for p in (data / "declarations").glob("*.txt"):
        for line in p.read_text().splitlines():
            cells = [c.strip() for c in line.split("|")]
            if len(cells) == 5 and cells[0].isdigit():
                names.append(cells[1].split("(PAN")[0])
            elif len(cells) == 4 and not cells[0].lower().startswith("name"):
                names.append(cells[0])
    from rptcheck.extract import parse_prior_year_note
    names += [n for n, _ in parse_prior_year_note(data / "prior_year_ar_note38.txt")]
    client = json.loads((data / "company.json").read_text())["name"]
    return [n for n in set(names) if company_key(n) != company_key(client)]


def main(use_llm: bool):
    llm = None
    if use_llm:
        from rptcheck.llm import OpenRouterLLM
        llm = OpenRouterLLM()
    res = run(DATA, Path("out"), llm=llm)
    findings = res["findings"]
    truth = list(csv.DictReader(open(DATA / "ground_truth.csv")))
    vendors = list(csv.DictReader(open(DATA / "vendor_master.csv")))
    mgmt = list(csv.DictReader(open(DATA / "management_rp_list.csv")))
    mgmt_keys = {company_key(m["name"]) for m in mgmt} | {m["pan"] for m in mgmt if m["pan"]}
    label = {t["vendor_id"]: t for t in truth}
    calc_by_vendor = {}
    for c in res["calculations"]:
        for v in c["vendors"]:
            calc_by_vendor[v] = c

    # ---------------- per case
    rows = []
    for t in truth:
        vid, exp = t["vendor_id"], t["expected"]
        got, ok = "", False
        if vid == "MINUTES":
            flagged = any("minutes" in d["file"] for d in res["document_flags"])
            tmp = Path(tempfile.mkdtemp()) / "kaveri"
            shutil.copytree(DATA, tmp)
            mp = next(tmp.glob("board_minutes_*.txt"))
            mp.write_text("\n".join(l for l in mp.read_text().splitlines() if "AI SYSTEM" not in l) + "\n")
            same = signature(run(tmp, None, llm=llm)) == signature(res)
            got, ok = f"flagged={flagged}, findings unchanged without the injected line={same}", flagged and same
        elif vid == "CALC":
            a = lodr_materiality(30000 * CR, date(2026, 1, 15))
            b = lodr_materiality(30000 * CR, date(2025, 12, 18))
            c = lodr_materiality(90000 * CR, date(2026, 1, 15))
            d = lodr_materiality(150000 * CR, date(2026, 1, 15))
            ok = (a.value_paise == 2500 * CR and b.value_paise == 1000 * CR
                  and c.value_paise == 4250 * CR and d.value_paise == 5000 * CR)
            got = (f"Rs 30,000 cr turnover: {a.value_paise // CR} cr ({a.rule_version}); same turnover a day before 19 Dec 2025: "
                   f"{b.value_paise // CR} cr; Rs 90,000 cr: {c.value_paise // CR} cr; Rs 1,50,000 cr: {d.value_paise // CR} cr (cap)")
        else:
            st = best_status(findings, vid)
            c = calc_by_vendor.get(vid)
            fx = [f for f in findings if f["vendor"] == vid]
            if exp == "confirmed":
                ok = st == "confirmed" and any(f["exception"] != "KNOWN_RELATED_PARTY" for f in fx)
                got = f"{st}: {', '.join(sorted({f['rule'] for f in fx}))}"
            elif exp == "candidate":
                ok, got = st in ("candidate", "red_flag"), f"{st}: {', '.join(sorted({f['rule'] for f in fx}))}" + \
                    (f"; investigator {fx[0]['investigation']['status']}" if fx and fx[0]["investigation"] else "")
            elif exp == "none":
                ok, got = st == "none", st
            elif exp == "miss_expected":
                ok, got = st == "none", "missed (as predicted)" if st == "none" else f"found: {st}"
            elif exp == "approval_exception":
                codes = [x["code"] for x in (c or {}).get("exceptions", [])]
                ok, got = bool(codes), ", ".join(codes) or "no exception"
            elif exp == "no_approval_needed":
                ok = bool(c) and not c["exceptions"] and "exemption" in c
                got = c.get("exemption", "no exemption") if c else "no calc"
            elif exp == "known_ok":
                ok = st == "confirmed" and all(f["exception"] == "KNOWN_RELATED_PARTY" for f in fx) and not (c or {}).get("exceptions")
                got = f"{st}, known, approvals within limit" if ok else f"{st}"
        rows.append(dict(case=t["case_id"], vendor=vid, expected=exp, got=got, result="PASS" if ok else "FAIL",
                         description=t["description"]))

    # ---------------- vendor level metrics
    related = {t["vendor_id"] for t in truth if t["truth"] == "related"}
    judgement = {t["vendor_id"] for t in truth if t["truth"] == "judgement"}
    known = {v["vendor_id"] for v in vendors if company_key(v["name"]) in mgmt_keys or (v["pan"] and v["pan"] in mgmt_keys)}
    undisclosed = related - known
    not_related = {v["vendor_id"] for v in vendors} - related - judgement
    surfaced = {f["vendor"] for f in findings}
    confirmed = {f["vendor"] for f in findings if f["status"] == "confirmed"}
    wl = baseline_names(DATA)
    fuzzy = {v["vendor_id"] for v in vendors if max(fuzzy_name_ratio(v["name"], n) for n in wl) >= 0.85}

    def m(flagged, pool):
        tp = len(flagged & pool)
        return f"{tp}/{len(pool)}"

    def prec(flagged):
        rel = related | judgement
        return f"{len(flagged & rel)}/{len(flagged)}" if flagged else "n/a"

    metrics = [
        ("B0 accept management's list", m(known, undisclosed), m(known, related), prec(known), len(known & not_related), "n/a"),
        ("B1 fuzzy name match (>= 0.85)", m(fuzzy, undisclosed), m(fuzzy, related), prec(fuzzy), len(fuzzy & not_related), "no reasons given"),
        ("This design: any finding", m(surfaced, undisclosed), m(surfaced, related), prec(surfaced), len(surfaced & not_related),
         f"{len({f['vendor'] for f in findings if f['status'] in ('candidate', 'red_flag')})} need review"),
        ("This design: Confirmed only", m(confirmed, undisclosed), m(confirmed, related), prec(confirmed), len(confirmed & not_related), "rule path + evidence"),
    ]
    fp_fuzzy = sorted(next(v["name"] for v in vendors if v["vendor_id"] == x) for x in fuzzy & not_related)
    miss_fuzzy = sorted(next(v["name"] for v in vendors if v["vendor_id"] == x) for x in undisclosed - fuzzy)

    sig2 = signature(run(DATA, None, llm=llm))
    det = "identical" if sig2 == signature(res) else "DIFFERENT"

    passed = sum(r["result"] == "PASS" for r in rows)
    md = [f"# Evaluation results ({'model: ' + res['model'] if llm else 'offline, no model'})", "",
          f"Synthetic client: {res['client']}, {res['period']}. {len(vendors)} vendors, {len(truth)} labelled cases. "
          f"**{passed}/{len(rows)} cases behave as expected** (T15 is a predicted miss and counts as expected).", "",
          "| Case | Vendor | Expected | Got | Result | What it tests |", "|---|---|---|---|---|---|"]
    md += [f"| {r['case']} | {r['vendor']} | {r['expected']} | {r['got']} | {r['result']} | {r['description']} |" for r in rows]
    md += ["", "## Against baselines (vendor level)", "",
           "| Method | Undisclosed related found | All related found | Precision of flags | Unrelated vendors flagged | Notes |",
           "|---|---|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | {d} | {e} | {f} |" for a, b, c, d, e, f in metrics]
    md += ["", f"Fuzzy baseline false flags: {', '.join(fp_fuzzy) or 'none'}.",
           f"Fuzzy baseline misses among undisclosed related vendors: {', '.join(miss_fuzzy) or 'none'}.",
           f"Determinism: two runs on the same inputs give {det} findings.", ""]
    md += stress(llm) + [""]
    Path("out/eval_results.md").write_text("\n".join(md))
    print("\n".join(md))


# ---------------------------------------------------------------- stress tests: break the inputs on purpose
def _mutated(mutator):
    tmp = Path(tempfile.mkdtemp()) / "kaveri"
    shutil.copytree(DATA, tmp)
    mutator(tmp)
    return tmp


def _edit_csv(path, fn):
    rows = list(csv.DictReader(open(path)))
    fields = list(rows[0].keys())
    rows = fn(rows)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def stress(llm=None):
    def gstin_typo(d):       # one wrong character in the only identifier of a partnership firm
        _edit_csv(d / "vendor_master.csv", lambda rs: [r | ({"gstin": r["gstin"][:-1] + ("A" if r["gstin"][-1] != "A" else "B")}
                                                            if r["vendor_id"] == "V004" else {}) for r in rs])

    def renamed(d):          # company renamed on MCA; vendor master still has the old name
        reg = json.loads((d / "registry_snapshot.json").read_text())
        reg["companies"]["U60231KA2015PTC081234"]["name"] = "NANDI SUPPLY CHAIN PRIVATE LIMITED"
        (d / "registry_snapshot.json").write_text(json.dumps(reg))

    def lookalike(d):        # unrelated 'Ramesh Stores' on the Chairman's street
        _edit_csv(d / "vendor_master.csv", lambda rs: rs + [dict(vendor_id="V999", name="Ramesh Stores", pan="", gstin="",
                  address="4th Cross, Jayalakshmipuram, Mysuru 570012", bank_account="99887766", ifsc="SBIN0001234")])

    def messy_form(d):       # declaration typed as free text instead of a table
        p = d / "declarations" / "D3_arvind_shetty.txt"
        p.write_text(p.read_text().replace(" | ", "   ").replace("|", " "))

    def no_payroll(d):       # auditor does not get KMP bank details
        _edit_csv(d / "register_directors_kmp.csv", lambda rs: [r | {"bank_account": "", "ifsc": ""} for r in rs])

    cases = [
        ("GSTIN typo on a partnership firm", gstin_typo, ["V004"], "firms are not on MCA, so the GSTIN was the only exact path; it falls back to name + pincode and the WRONG clause (s.2(76)(i) instead of (iii)); the investigator still finds the MBP-1 line"),
        ("Company renamed on MCA, old name in vendor master", renamed, ["V001", "V002"], "registry former names are not indexed"),
        ("Unrelated look alike vendor on the Chairman's street", lookalike, ["V999"], "name + address puts it in the gray zone"),
        ("Declaration typed as free text (no table)", messy_form, ["V004", "V012"], "offline parser needs the table layout; this is the LLM's job"),
        ("No KMP bank details shared with auditor", no_payroll, ["V007"], "the bank key disappears"),
    ]
    rows = []
    for name, fn, vids, why in cases:
        r = run(_mutated(fn), None, llm=llm)
        got = "; ".join(f"{v}: {best_status(r['findings'], v)}" for v in vids)
        rows.append(f"| {name} | {got} | {why} |")
    return ["## Stress tests (inputs broken on purpose)", "",
            "| Perturbation | Outcome for affected vendors | Why |", "|---|---|---|"] + rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    main(ap.parse_args().llm)
