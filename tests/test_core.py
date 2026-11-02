import json
from datetime import date
from pathlib import Path

import pytest

from rptcheck.agent import LLMPolicy
from rptcheck.calc import CR, lodr_materiality, rule15_threshold, subsidiary_rpt_needs_parent_ac
from rptcheck.extract import canonical_relation, parse_declaration, validate_span
from rptcheck.ids import is_valid_gstin, make_gstin, normalize_din, normalize_gstin, normalize_pan, pan_from_gstin, parse_cin
from rptcheck.llm import ScriptedLLM, redacted_card
from rptcheck.memo import check_citations
from rptcheck.names import company_key, person_name_score
from rptcheck.pipeline import run

DATA = Path(__file__).resolve().parents[1] / "data" / "kaveri"


# ---------------- identifiers (exact)
def test_gstin_checksum_on_public_example():
    assert is_valid_gstin("27AAPFU0939F1ZV")
    assert not is_valid_gstin("27AAPFU0939F1ZA")


def test_pan_inside_gstin_collapses_states():
    ka, tn = make_gstin("29", "AACCN1234F"), make_gstin("33", "AACCN1234F", "2")
    assert pan_from_gstin(ka) == pan_from_gstin(tn) == "AACCN1234F"


def test_ocr_repair_is_position_aware_and_never_guesses_past_checksum():
    assert normalize_pan("AAICM5O1BQ") == "AAICM5018Q"
    assert normalize_din("O4567890") == "04567890"
    good = make_gstin("29", "AAICM5018Q")
    assert normalize_gstin(good.replace("0", "O", 1)) == good
    assert normalize_gstin(good[:-1] + ("A" if good[-1] != "A" else "B")) is None


def test_cin_routes_private_vs_public():
    assert parse_cin("U60231KA2015PTC081234").company_class == "private"
    assert parse_cin("U01119TG2001PLC036969").company_class == "public"


def test_company_names_normalise_to_one_key():
    assert company_key("Nandi Logistics Pvt. Ltd.") == company_key("NANDI LOGISTICS PRIVATE LIMITED")


def test_initials_are_scored_but_flagged():
    s, note = person_name_score("Sri K Ramesh Traders", "Ramesh Kumar Krishnan")
    assert 0.7 <= s <= 0.85 and note.startswith("1")


# ---------------- calculators (exact, integer paise)
@pytest.mark.parametrize("turnover_cr,on,expected_cr", [
    (1450, date(2026, 1, 1), 145),          # band 1
    (20000, date(2026, 1, 1), 2000),        # band 1 upper edge
    (30000, date(2026, 1, 1), 2500),        # band 2
    (90000, date(2026, 1, 1), 4250),        # band 3
    (150000, date(2026, 1, 1), 5000),       # band 3 cap
    (30000, date(2025, 12, 18), 1000),      # old rule, day before Schedule XII
])
def test_lodr_materiality(turnover_cr, on, expected_cr):
    assert lodr_materiality(turnover_cr * CR, on).value_paise == expected_cr * CR


def test_band_two_starts_one_paisa_above_twenty_thousand_crore():
    t = 20000 * CR + 1
    assert lodr_materiality(t, date(2026, 1, 1)).rule_version.endswith("band 2")


def test_rule15_and_subsidiary_rule():
    assert rule15_threshold("purchase_goods", 1320 * CR, 620 * CR).value_paise == 132 * CR
    assert subsidiary_rpt_needs_parent_ac(int(1.5 * CR), 10 * CR, 145 * CR)       # > 1 cr and > 10% of 10 cr
    assert not subsidiary_rpt_needs_parent_ac(int(0.9 * CR), 5 * CR, 145 * CR)    # below 1 cr


# ---------------- S1 validator
def test_span_validator():
    page = "1 | Malnad Agrltech Private Limited (PAN: AAICM5O1BQ) | Director and member | 15% | 03-02-2020"
    assert validate_span("Malnad Agrltech Private Limited (PAN: AAICM5O1BQ)", page)[1] == "exact"
    assert validate_span("Malnad Agritech Private Limited (PAN: AAICM5018Q)", page)[0]       # model fixed OCR: accepted
    assert not validate_span("Kabini Agritech Private Limited (PAN: AAKCK9999Z)", page)[0]  # not on page: rejected


def test_relations_follow_rule4():
    assert canonical_relation("Wife's brother").startswith("other")
    assert canonical_relation("Son-in-law") == "daughters_husband"
    assert canonical_relation("Daughter's husband") == "daughters_husband"
    assert canonical_relation("Brother") == "brother"
    assert canonical_relation("Sister's husband").startswith("other")     # brother in law: not a relative
    assert canonical_relation("Father-in-law").startswith("other")
    assert canonical_relation("Son's wife") == "sons_wife"
    assert canonical_relation("Step-son") == "son"


# ---------------- S7 checker
def test_citation_checker():
    ok, _ = check_citations("The vendor shares a PAN with the firm [E001]. The rule applies [E002].", {"E001", "E002"})
    assert ok
    ok, probs = check_citations("The vendor is clearly related. The rule applies [E002].", {"E002"})
    assert not ok and "no evidence ID" in probs[0]
    ok, probs = check_citations("It matches [E999].", {"E001"})
    assert not ok and "unknown" in probs[0]


# ---------------- LLM plumbing, tested with a scripted stand in (no network)
def test_llm_extraction_goes_through_the_same_validator(tmp_path):
    p = tmp_path / "D4.txt"
    p.write_text((DATA / "declarations" / "D4_meera_hegde_scan.txt").read_text())
    page1 = json.dumps({"items": [
        {"kind": "interest", "entity_name": "Malnad Agritech Private Limited", "pan_raw": "AAICM5O1BQ", "nature": "Director and member",
         "shareholding": "15%", "raw_quoted_span": "1 | Malnad Agritech Private Limited (PAN: AAICM5O1BQ) | Director and member | 15% | 03-02-2020"},
        {"kind": "interest", "entity_name": "Imaginary Holdings Private Limited", "pan_raw": "AAICI1111A", "nature": "Director",
         "shareholding": "50%", "raw_quoted_span": "3 | Imaginary Holdings Private Limited (PAN: AAICI1111A) | Director | 50% | 01-01-2020"}]})
    page2 = json.dumps({"items": [{"kind": "relative", "name": "Ravi Hegde", "relation_text": "Husband", "pan_raw": "AERPH4471J",
                                   "din_raw": "", "raw_quoted_span": "Ravi Hegde | Husband | AERPH4471J | -"}]})
    dec = parse_declaration(p, llm=ScriptedLLM([page1, page2]))
    assert [i.pan for i in dec.interests] == ["AAICM5018Q"]          # OCR repaired by code, not by the model
    assert dec.interests[0].span_check == "fuzzy"
    assert len(dec.rejected) == 1 and "Imaginary" in dec.rejected[0]["entity_name"]   # hallucinated row dropped


def test_tier3_bad_output_becomes_cannot_tell_and_card_is_redacted():
    llm = ScriptedLLM(['{"decision": "definitely related", "reason": "trust me"}'])
    assert llm.judge_match({"name": "a"}, {"name": "b"})["decision"] == "cannot_tell"
    card = redacted_card("Sri K Ramesh Traders", "12, 4th Cross, Jayalakshmipuram, Mysore - 570012", "unknown")
    assert set(card) == {"name", "city", "pincode", "entity_type"}


def test_agent_cannot_corroborate_without_a_grounded_quote():
    llm = ScriptedLLM(['{"action":"stop","status":"corroborated","evidence_quotes":["the vendor is owned by the chairman"]}'])
    pol = LLMPolicy(llm, "candidate", ["search_minutes(query)"])
    steps = [{"tool": "search_minutes", "args": {"query": "x"}, "result": [{"evidence": "E900", "text": "Item 5: results approved"}]}]
    assert pol.next(steps)["status"] == "inconclusive"


# ---------------- end to end guarantees
@pytest.fixture(scope="module")
def result():
    return run(DATA, None)


def test_judgement_rule_is_never_confirmed(result):
    assert all(f["status"] != "confirmed" for f in result["findings"] if f["rule"] == "INDAS24_P9_CLOSE_FAMILY")


def test_tier3_link_caps_status_at_candidate(result):
    for f in result["findings"]:
        if any(s.get("tier") == 3 for s in f["path"]):
            assert f["status"] == "candidate"


def test_resigned_director_and_small_holding_not_flagged(result):
    vendors = {f["vendor"] for f in result["findings"]}
    assert "V016" not in vendors and "V017" not in vendors


def test_every_memo_cites_only_its_own_evidence(result):
    for f in result["findings"]:
        ok, probs = check_citations(f["memo"], set(f["evidence"]) | set(f["investigation_evidence"]))
        assert ok, (f["vendor"], probs)


def test_stale_registry_fact_is_flagged(result):
    nandi = next(f for f in result["findings"] if f["vendor"] == "V001")
    assert nandi["stale_evidence"], "relative's DIN record is older than its 30 day TTL"
