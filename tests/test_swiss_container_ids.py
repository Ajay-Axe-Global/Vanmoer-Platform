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
