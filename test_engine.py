"""Runs every demo scenario through the rules engine (no AI needed)."""
from datetime import date
from data import make_ctx, BUYER, VENDORS
from engine import evaluate

TODAY = date(2026, 11, 6)

def inv(vid="V2", po="7790", item="Steel plate", qty=50, price=2200, no="BS-1001", **kw):
    v, sub = VENDORS[vid], qty * price
    d = dict(vendor_name=v["name"], vendor_gstin=v["gstin"], buyer_name=BUYER["name"], buyer_gstin=BUYER["gstin"],
             invoice_no=no, date="2026-10-25", po_ref=po, currency="INR",
             lines=[dict(item=item, qty=qty, unit_price=price, amount=sub)], subtotal=sub, tax=sub * 0.18,
             total=sub * 1.18, bank_name=v["bank_name"], bank_account=v["account"], bank_ifsc=v["ifsc"], confidence={})
    d.update(kw); return d

def run(name, expect, expect_risk=None, ctx=None, **kw):
    r = evaluate(inv(**kw), ctx or make_ctx(), TODAY)
    ok = r["outcome"] == expect and (expect_risk is None or r["risk"] == expect_risk)
    print(f"{'PASS' if ok else 'FAIL'}  {name:34} -> {r['outcome']:8} {str(r['risk'] or '-'):9} {r['points']:>2} pts | {r['vendor_message'][:70]}")
    assert ok, r["summary"]; return r

run("Happy path", "approved", "Very low")
run("Perfect invoice (Acme, fresh PO)", "approved", "Very low", vid="V1", po="7850", item="Bearing set", qty=40, price=4800, no="INV-3001")
run("Perfect invoice (Nova, fresh PO)", "approved", "Very low", vid="V4", po="7860", item="Carton box", qty=60, price=350, no="NP-120")
r = run("EDGE 1 bank account changed", "review", "High", vid="V1", po="7781", item="Bearing set", qty=100, price=4800, no="INV-2041", bank_name="ICICI", bank_account="0123456773 05", bank_ifsc="ICIC0001234")
assert r["needs_admin"]
r = run("EDGE 2 billed 100, received 80", "rejected", vid="V4", po="7802", item="Carton box", qty=100, price=350, no="NP-77")
assert "resubmit for 80" in r["vendor_message"]
r = run("EDGE 3 late delivery + penalty note", "review", vid="V3", po="7810", item="Rice bag 25kg", qty=200, price=1450, no="KF-310")
assert r["late"]["days"] == 6 and r["late"]["suggested_penalty"] == 17400
run("EDGE 4 rupee cap beats percentage", "review", vid="V2", po="7820", item="Hydraulic pump", qty=10, price=50080, no="BS-1010")
run("Over review limit (overbilled)", "rejected", price=2240, no="BS-1002")
run("Missing invoice number", "rejected", invoice_no="")
run("Low read confidence -> re-upload", "reupload", confidence={"total": 0.4})
run("Unknown vendor", "rejected", vendor_name="Zed Traders")
run("Lookalike vendor (Acrne)", "review", "Medium", vid="V1", po="7781", item="Bearing set", qty=100, price=4800, vendor_name="Acrne Private Limited")
run("PO belongs to another vendor", "review", vid="V1", po="7790", item="Steel plate")
run("Closed PO", "rejected", vid="V1", po="7770", item="Bearing set", qty=50, price=4800)
run("Fake PO number", "rejected", po="9999")
run("Totals do not add up", "rejected", total=999999)
run("Billed to wrong company", "review", buyer_name="Other Corp Ltd")
c = make_ctx(); c["history"] = [dict(vendor_id="V2", invoice_no="BS-1001", po_ref="7790", status="approved", total=129800, date="2026-10-25", subtotal=110000, lines=[dict(item="Steel plate", qty=50)])]
run("Exact duplicate", "rejected", ctx=c)
c = make_ctx(); c["history"] = [dict(vendor_id="V2", invoice_no=f"X{i}", po_ref="7790", status="approved", total=1, date="2026-10-01", subtotal=1, lines=[]) for i in range(10)]
run("11th invoice on one PO", "review", ctx=c)
c = make_ctx(uploader="V1")
run("Uploaded by a different vendor", "review", ctx=c)
print("\nAll scenarios behave as designed.")