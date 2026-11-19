#!/usr/bin/env python3
"""Pure unit tests for dashboard/finance_import.py: parser, normaliser, rule priority, dedupe.

No database, no network. Every row is SYNTHETIC (ZZT* merchants, made-up numbers). Never paste
real transactions into this file.

    python3 tests/finance_import_unit.py
"""
import os
import re
import sys
from datetime import date
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
import finance_import as fi  # noqa: E402

FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


ANZ = (
    "Date,Value,Details\n"
    "01/10/2026,-12.50,EFTPOS ZZTGROCER 1234 RICHMOND AU\n"
    "01/10/2026,-12.50,EFTPOS ZZTGROCER 1234 RICHMOND AU\n"          # genuine duplicate purchase
    '02/10/2026,"-1,234.00",PAYMENT TO ZZT LANDLORD 123456789\n'      # thousands separator
    "03/10/2026,2500.00,ZZT EMPLOYER PAY 987654\n"
    "04/10/2026,-300.00,ANZ M-BANKING FUNDS TFER TRANSFER 112233 TO 012345678\n"
    "05/10/2026,-8.40,VISA DEBIT PURCHASE CARD 4821 ZZT CAFE CO CARLTON AU\n"
    "not a date,-1.00,ZZT BROKEN ROW\n"
)


def test_parser():
    rows, info = fi.parse_csv(ANZ.encode())
    check("anz format picked", info["format"] == "anz", info["format"])
    check("comma delimiter", info["delimiter"] == ",")
    check("bad row skipped", info["skipped"] == 1, str(info))
    check("6 good rows", len(rows) == 6, str(len(rows)))
    check("dd/mm/yyyy date", rows[0]["date"] == date(2026, 10, 1))
    check("signed spend negative", rows[0]["amount"] == Decimal("-12.50"))
    check("thousands separator stripped", rows[2]["amount"] == Decimal("-1234.00"), str(rows[2]["amount"]))
    check("income positive", rows[3]["amount"] == Decimal("2500.00"))

    # semicolon delimiter, same columns, different header case/order
    semi = "details;date;value\nZZT SHOP;07/10/2026;-5,00\n".replace("-5,00", "-5.00")
    rows, info = fi.parse_csv(semi.encode())
    check("semicolon detected", info["delimiter"] == ";", info["delimiter"])
    check("header order independent", rows and rows[0]["description"] == "ZZT SHOP")

    # headerless ANZ export (Date, Amount, Description)
    rows, info = fi.parse_csv(b"06/10/2026,-4.00,ZZT KIOSK\n07/10/2026,-6.00,ZZT KIOSK\n")
    check("headerless anz picked", info["format"] == "anz_headerless", info["format"])
    check("headerless keeps first row", len(rows) == 2, str(len(rows)))

    # debit/credit columns via alias auto-detect
    dc = "Transaction Date,Narrative,Debit,Credit\n2026-10-08,ZZT FUEL,40.00,\n2026-10-09,ZZT REFUND,,15.00\n"
    rows, info = fi.parse_csv(dc.encode())
    check("debit/credit auto", info["format"] == "auto")
    check("debit negative", rows[0]["amount"] == Decimal("-40.00"), str(rows[0]["amount"]))
    check("credit positive", rows[1]["amount"] == Decimal("15.00"))

    # configurable mapping from config, no code: a made-up bank
    fmt = [("zztbank", {"headers": ["when", "what", "howmuch"], "date": "when", "description": "what",
                        "amount": "howmuch", "date_formats": ["%Y%m%d"]})]
    rows, info = fi.parse_csv(b"when,what,howmuch\n20261010,ZZT THING,-3.30\n", fmt)
    check("db-configured format used", info["format"] == "zztbank" and rows[0]["date"] == date(2026, 10, 10))

    # refusals: unknown columns, oversize. Error text must not echo a cell value.
    try:
        fi.parse_csv(b"foo,bar\nZZTSECRETVALUE,1\n")
        check("unknown columns refused", False)
    except fi.ImportError_ as e:
        check("unknown columns refused", True)
        check("error has no cell value", "ZZTSECRETVALUE" not in str(e), str(e))
    try:
        fi.parse_csv(b"x" * (fi.MAX_UPLOAD_BYTES + 1))
        check("oversize refused", False)
    except fi.ImportError_:
        check("oversize refused", True)
    for s, want in [("(12.00)", Decimal("-12.00")), ("12.00 DR", Decimal("-12.00")), ("$1,000.10", Decimal("1000.10"))]:
        check(f"amount {s}", fi.parse_amount(s) == want)


def test_normaliser():
    cases = {
        "EFTPOS ZZTGROCER 1234 RICHMOND AU": "ZZTGROCER",
        "eftpos   zztgrocer   1234 richmond au": "ZZTGROCER",          # case + whitespace
        "VISA DEBIT PURCHASE CARD 4821 ZZT CAFE CO CARLTON AU": "ZZT CAFE CO",
        "PAYMENT TO ZZT POWER CO 123456789": "ZZT POWER CO",
        "ZZT STREAMING DT.AB12 XXXX1234": "ZZT STREAMING",
        "ZZT BOOKS ABCD1234 SYDNEY NSW AU": "ZZT BOOKS",
        "ZZTSHOP 12/11 COLLINGWOOD AU": "ZZTSHOP",
        "7ELEVEN 2045 FITZROY AU": "7ELEVEN",                        # merchant with digits kept
        "EFTPOS ZZTGROCER 77 KEW AU": "ZZTGROCER",                   # short store number
        "ZZT SALARY 998877665544": "ZZT SALARY",
        "ANZ M-BANKING FUNDS TFER TRANSFER 112233 TO 012345678": fi.TRANSFER_KEY,
        "ANZ M-BANKING FUNDS TFER TRANSFER 445566 FROM 987654321": fi.TRANSFER_KEY,
    }
    for raw, want in cases.items():
        got = fi.normalise_description(raw)
        check(f"normalise {raw!r}", got == want, f"got {got!r}")
    for raw in cases:
        key = fi.normalise_description(raw)
        check(f"no 6+ digit run in key {raw!r}", not re.search(r"\d{6,}", key), key)
    check("transfer key has no account number", not any(c.isdigit() for c in fi.TRANSFER_KEY))
    check("stored description masks long digits",
          "012345678" not in fi.mask_description("TFER TO 012345678") and "######" in fi.mask_description("TFER TO 012345678"))


def test_rule_priority():
    rules = [
        {"id": 1, "match_type": "regex", "pattern": "^ZZT", "category": "misc", "priority": 100},
        {"id": 2, "match_type": "contains", "pattern": "CAFE", "category": "dining_takeaway", "priority": 100},
        {"id": 3, "match_type": "exact", "pattern": "ZZT CAFE CO", "category": "business_side_projects", "priority": 100},
        {"id": 4, "match_type": "contains", "pattern": "ZZT", "category": "shopping_retail", "priority": 50},
        {"id": 5, "match_type": "regex", "pattern": "([", "category": "misc", "priority": 0},   # invalid regex
    ]
    check("exact beats contains and regex", fi.match_rule("ZZT CAFE CO", rules)["id"] == 3)
    check("contains: lower priority number first", fi.match_rule("ZZT CAFE BAR", rules)["id"] == 4)
    check("contains beats regex", fi.match_rule("ZZTGROCER", rules)["id"] == 4)
    only_regex = [r for r in rules if r["match_type"] == "regex"]
    check("invalid regex skipped, valid regex matches", fi.match_rule("ZZTGROCER", only_regex)["id"] == 1)
    check("no match -> None", fi.match_rule("OTHER", [rules[2]]) is None)
    same = [{"id": 9, "match_type": "contains", "pattern": "ZZT", "category": "a", "priority": 100},
            {"id": 8, "match_type": "contains", "pattern": "ZZ", "category": "b", "priority": 100}]
    check("tie -> lower id first", fi.match_rule("ZZT X", same)["id"] == 8)
    check("pattern normalised for exact", fi.norm_pattern("exact", "  zzt  cafe co ") == "ZZT CAFE CO")


def test_dedupe():
    rows, _ = fi.parse_csv(ANZ.encode())
    p1 = fi.prepare_rows(rows)
    p2 = fi.prepare_rows(fi.parse_csv(ANZ.encode())[0])
    check("same file -> same hashes", [r["hash"] for r in p1] == [r["hash"] for r in p2])
    check("identical same-day rows get distinct occurrence", p1[0]["occurrence"] == 0 and p1[1]["occurrence"] == 1)
    check("identical same-day rows get distinct hashes", p1[0]["hash"] != p1[1]["hash"])
    check("all hashes unique", len({r["hash"] for r in p1}) == len(p1))
    h = fi.dedupe_hash(date(2026, 1, 1), Decimal("-1.5"), "ZZT", 0)
    check("hash is sha256 and amount-format stable", len(h) == 64 and h == fi.dedupe_hash(date(2026, 1, 1), Decimal("-1.50"), "ZZT", 0))


if __name__ == "__main__":
    test_parser()
    test_normaliser()
    test_rule_priority()
    test_dedupe()
    print(f"finance_import_unit: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
