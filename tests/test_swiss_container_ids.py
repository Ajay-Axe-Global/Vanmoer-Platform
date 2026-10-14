"""Regression: Swiss Inbound must never drop/mangle the 4-letter container prefix
(Hapag-Lloyd 'HAMU 4353443' came back as '4353443' / '259O528')."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers.doc_common import fix_container_id
from clients.swiss.inbound.extractor import repair_mbl_container_prefixes, validate

PKL_IDS = ["HAMU4353443", "HLBU2590528", "TGSU5069269", "UETU5284540"]


def _mbl(ids):
    return {"carrier": "HAPAG-LLOYD", "mbl_no": "X", "containers": [{"id": fix_container_id(i)[0]} for i in ids]}


def _pkl():
    return {"containers": [{"container_id": i} for i in PKL_IDS]}


def test_bare_digits_not_mangled():
    assert fix_container_id("2590528")[0] == "2590528"
    assert fix_container_id("5069269")[0] == "5069269"
    assert fix_container_id("HAMU 4353443")[0] == "HAMU4353443"
    assert fix_container_id("OOCU7228009")[0] == "OOCU7228009"


def test_prefix_restored_from_packing_list():
    mbl, pkl = _mbl(["4353443", "2590528", "5069269", "5284540"]), _pkl()
    repair_mbl_container_prefixes(mbl, pkl)
    assert sorted(c["id"] for c in mbl["containers"]) == sorted(PKL_IDS)
    assert not any("only in" in r or r.startswith("[X]") for r in validate(mbl, pkl))


def test_unmatched_bare_id_is_flagged():
    mbl, pkl = _mbl(["9999999"]), _pkl()
    notes = repair_mbl_container_prefixes(mbl, pkl)
    assert notes and notes[0].startswith("[!]")
    assert any(r.startswith("[X]") for r in validate(mbl, pkl))


def test_merge_and_mbl_order():
    from clients.swiss.inbound.extractor import build_rows, merge_packing_lists, COSCO_MBL_PROMPT, CARRIER_MBL_PROMPTS
    assert CARRIER_MBL_PROMPTS["COSCO"] is COSCO_MBL_PROMPT
    mbl = {"carrier": "COSCO", "mbl_no": "COSU1", "port_of_loading": "HOUSTON, USA", "containers": [
        {"id": i, "seal": "UL-1", "type": "40HQ", "bags": 880, "gross_weight_kg": 26000}
        for i in ("CSNU6617617", "FFAU6276540", "TIIU7441390", "FCIU9520805", "CSNU7325939")]}

    def pk(*ids):
        return {"packing_list_source": "pdf", "packing_list_layout": "COA", "total_bags": 0,
                "containers": [{"container_id": i, "net_weight_kg": 22000, "lot_no": "L", "product": "P"} for i in ids]}

    # file order differs from the MBL order on purpose
    merged = merge_packing_lists([pk("TIIU7441390", "FCIU9520805", "CSNU7325939"), pk("CSNU6617617", "FFAU6276540")])
    rows = build_rows(mbl, merged, "R", "", "S")
    assert [r["container_no"] for r in rows] == [c["id"] for c in mbl["containers"]]
    assert all(r["bags"] == 880 and r["pallet_qty"] == 22 for r in rows)


def test_misread_mbl_ids_reconciled_by_check_digit():
    pkl = {"containers": [{"container_id": i} for i in
           ("CSNU6617617", "FFAU6276540", "TIIU7441390", "FCIU9520805", "CSNU7325939")]}
    mbl = {"containers": [{"id": i} for i in
           ("CSNU6617617", "FFAU6765970", "TIIU7441390", "FCIU9520805", "CSNU0732539")]}
    notes = repair_mbl_container_prefixes(mbl, pkl)
    assert [c["id"] for c in mbl["containers"]] == ["CSNU6617617", "FFAU6276540", "TIIU7441390",
                                                      "FCIU9520805", "CSNU7325939"]
    assert len(notes) == 2 and not any("only in" in r for r in validate(mbl, pkl))


def test_zoom_read_votes_out_random_misreads():
    import clients.swiss.inbound.extractor as ex
    truth = [("CSNU6617617", "UL-1141548", 26388.643), ("FFAU6276540", "UL-1141525", 26288.853),
             ("TIIU7441390", "UL-1141327", 26288.853), ("FCIU9520805", "UL-1141348", 26415.859),
             ("CSNU7325939", "UL-1141349", 26388.643)]
    # one wrong read among the first reads; each misreads a DIFFERENT field
    wrong = {0: (1, "FFAU6765970", None, None), 2: (2, None, "UL-1141512", None)}
    calls = {"n": 0}

    def fake_gemini(prompt, pdf_bytes=None, **kw):
        k = calls["n"]
        calls["n"] += 1
        rows = [{"id": i, "seal": s, "bags": 880, "type": "40HQ", "gross_weight_kg": g} for i, s, g in truth]
        if k in wrong:
            idx, bid, bseal, _ = wrong[k]
            if bid:
                rows[idx]["id"] = bid
            if bseal:
                rows[idx]["seal"] = bseal
        return {"rows": rows}

    old_g, old_s = ex.call_gemini, ex._zoomed_strips
    ex.call_gemini, ex._zoomed_strips = fake_gemini, lambda *a, **k: [b"x"]
    try:
        data = {"containers": [{"id": "BAD", "type": "40HQ"}] * 5, "container_pages": [4]}
        notes = ex.read_container_lines_zoomed("nofile.pdf", data)
    finally:
        ex.call_gemini, ex._zoomed_strips = old_g, old_s
    assert [(c["id"], c["seal"], c["gross_weight_kg"]) for c in data["containers"]] == truth
    assert all(c["type"] == "40HQ" for c in data["containers"])
    assert not any("check digit" in n for n in notes)
