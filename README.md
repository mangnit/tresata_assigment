# Related party completeness check (prototype)

Indian statutory auditors must check that management's list of related parties is complete (SA 550). In practice the list is often accepted as given. This prototype starts from the other end: it takes every counterparty in the ledger, resolves it against director declarations, the client's own registers, a public registry snapshot and last year's annual report, and tests each link against the legal definitions in code. An auditor reviews every finding.

**All data in this repo is SYNTHETIC.** "Kaveri Agro Limited" and every person, PAN, DIN, CIN, GSTIN and bank account are made up. PANs and GSTINs follow the real formats and GSTIN check characters are computed, so the validators run real logic.

## Run it

```bash
pip install -r requirements.txt
python data/make_synthetic.py      # builds data/kaveri/ (already included)
python run.py                      # findings -> out/findings.json, out/findings.csv, out/review.html
python eval.py                     # 20 labelled cases, baselines, stress tests -> out/eval_results.md
python -m pytest -q                # 25 unit tests
```

Optional model mode (OpenRouter free tier):

```bash
export OPENROUTER_API_KEY=...            # free key from openrouter.ai
export OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free   # any free model id works; check openrouter.ai/models
python run.py --llm && python eval.py --llm
```

**Model used for the reported results: none.** The results below are from offline mode, where Tier 3 matching abstains (routes to a human), the investigator runs a fixed lookup script, and memos come from a template. The model code paths (extraction, Tier 3 judge, investigator loop, memo writer) are exercised in the unit tests with a scripted stand in, not with a live model. Running `--llm` is the next step and its results are not claimed here.

## Pipeline

| Stage | File | Exact or probabilistic |
|---|---|---|
| S1 read declarations, span check, injection scan | `rptcheck/extract.py` | parser exact; model extraction probabilistic, validated by code |
| S2 resolve identities in tiers | `rptcheck/graph.py`, `ids.py`, `names.py` | Tier 1 exact keys; Tier 2 scored; Tier 3 model or abstain |
| S3 knowledge graph with provenance and TTL | `rptcheck/graph.py`, `evidence.py` | exact |
| S4 legal rules as versioned path queries | `rptcheck/rules.py` | exact |
| S5 thresholds and approvals (integer paise) | `rptcheck/calc.py` | exact |
| S6 bounded investigator (8 calls, 60 s) | `rptcheck/agent.py` | probabilistic, logged, cannot confirm |
| S7 memo with citation checker | `rptcheck/memo.py` | text probabilistic, citations checked by code |
| S8 human review | `out/review.html` (mock), `out/findings.csv` | human |

Tier 1 keys: PAN (including the PAN inside a GSTIN), DIN, CIN, bank account plus IFSC, and the normalised registered name for companies (company names are unique by law). A PAN mismatch vetoes any match. Any Tier 2 or 3 link on a path caps the finding at Candidate.

## Results (offline, synthetic)

20 of 20 labelled cases behave as expected, including one predicted miss. Full table: `out/eval_results.md`.

| Method | Undisclosed related vendors found | Unrelated vendors flagged |
|---|---|---|
| Accept management's list | 0 of 10 | 0 |
| Fuzzy name match (ratio 0.85) | 6 of 10 | 3 |
| This design, Confirmed only | 7 of 10 | 0 |
| This design, any finding (Confirmed, Candidate, red flag) | 9 of 10 | 0 |

## Where it fails (honest list)

* **Undeclared relative with no shared identifier** (case T15, son in law's consultancy): missed, as designed. No public registry lists relatives.
* **Company renamed on MCA** while the vendor master keeps the old name: both Nandi records missed. Fix: index MCA former names.
* **Free text declaration** instead of a table: the offline parser drops the partnership firm. This is the job the model is meant to do, and it is untested live.
* **GSTIN typo on a firm**: falls back to a name plus pincode match and reports the wrong clause (s.2(76)(i) instead of (iii)).
* **No KMP bank details**: the CFO payee case disappears.
* **Look alike vendor on a director's street**: lands in the review queue as a Candidate (a false alarm, but never a false Confirmed).
* **Bias warning**: I wrote both the test cases and the system, so these numbers show the logic works on cases I thought of. They are not an accuracy estimate.

## Test cases

| Case | What it tests |
|---|---|
| T01 | Private company where the Chairman's son is a director (2 hop: director, declared relative, MCA directorship) |
| T02 | Same company's Tamil Nadu GSTIN with no PAN column (PAN read from GSTIN) |
| T03 | Chairman's own proprietorship, no PAN, initials plus address only (gray zone) |
| T04 | Common name matches a director's brother, PAN differs (must not match) |
| T05 | Unregistered vendor paid into the CFO's salary account |
| T06 | Related party in last year's annual report, missing this year |
| T07 | Director resigned before the year (must not count) |
| T08 | Known 76% subsidiary with no audit committee approval |
| T09 | Twelve Rs 13 crore invoices: omnibus limit, Reg 23 materiality and Rule 15 |
| T10 | Different PAN, same bank account as the promoter firm (possible conduit) |
| T11 | Scanned MBP-1 with OCR noise in the PAN |
| T12 | Prompt injection inside board minutes |
| T13 | Schedule XII bands and rule version by date |
| T14 | Wife's brother: not a s.2(77) relative, Ind AS 24 judgement |
| T15 | Undeclared son in law (predicted miss) |
| T16 | Firm where a director is partner |
| T17, T17b | Public company with 3.5% vs 0.8% holding (s.2(76)(v)) |
| T18 | Wholly owned subsidiary exemption, Reg 23(5)(b) |

## Legal sources encoded

Companies Act 2013 s.2(76), s.2(77) with Rule 4 of the Companies (Specification of Definitions Details) Rules 2014, s.177(4)(iv), s.184 (Form MBP-1), s.188 with Rule 15(3); Ind AS 24 para 9; SEBI LODR Reg 2(1)(zc), Reg 23 and Schedule XII (Fifth Amendment 2025, in force for RPTs from 19 December 2025); SA 550. This is a prototype, not legal advice; thresholds should be checked against the current notified text before any real use.
