"""Rules engine. The AI only READS invoices; every decision here is plain, explainable rules."""
import re, difflib
from datetime import date

GST_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
BANKS = {"HDFC": "hdfc", "ICIC": "icici", "SBIN": "sbi", "UTIB": "axis", "KKBK": "kotak"}
BANDS = [(12, "Very high"), (8, "High"), (4, "Medium"), (2, "Low"), (0, "Very low")]
REQUIRED = ["vendor_name", "vendor_gstin", "invoice_no", "date", "po_ref", "total", "bank_account"]

FIELD = {"invoice_no": "invoice number", "vendor_name": "vendor name", "vendor_gstin": "vendor GSTIN", "po_ref": "PO number",
         "bank_account": "bank account", "date": "invoice date", "total": "total amount"}

def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    s = re.sub(r"\bpvt\b", "private", s)
    return " ".join(re.sub(r"\bltd\b", "limited", s).split())

def inr(x):
    s = str(abs(round(x))); head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return "₹" + (head + "," + tail if head else tail)

def band(p): return next(b for t, b in BANDS if p >= t)

def evaluate(inv, ctx, today=None):
    S, today = ctx["settings"], today or date.today()
    C, st = [], {"admin": False, "late": None}

    def add(stage, name, status, detail, pts=0, msg=None):
        C.append(dict(stage=stage, name=name, status=status, detail=detail, points=pts,
                      vendor_msg=(msg or detail) if status == "fail" else None))

    def done(outcome=None):
        fails = [c for c in C if c["status"] == "fail"]
        flags = sorted([c for c in C if c["status"] == "flag"], key=lambda c: -c["points"])
        outcome = outcome or ("rejected" if fails else "review" if flags else "approved")
        pts = sum(c["points"] for c in C)
        vm = {"approved": "Approved.", "review": "Under review."}.get(
            outcome, " ".join(c["vendor_msg"] for c in fails))
        if outcome in ("rejected", "reupload"): summ = " ".join(c["detail"] for c in fails)
        elif flags: summ = ("All checks passed except: " if len(flags) == 1 else "Flags: ") + \
                           "; ".join(f"{c['name'].lower()} ({c['detail']})" for c in flags)
        else: summ = "All checks passed."
        return dict(outcome=outcome, points=pts, risk=None if outcome in ("rejected", "reupload") else band(pts), checks=C, vendor_message=vm,
                    summary=summ, needs_admin=st["admin"], late=st["late"])

    # 1. READ: low confidence means "upload again", not a rejection
    conf = inv.get("confidence", {})
    low = [f for f in REQUIRED if conf.get(f, 1) < S["min_confidence"]]
    if low:
        add("Read", "Read confidence", "fail", f"Could not read {', '.join(FIELD[f] for f in low)} clearly",
            msg="We could not read this clearly. Please upload a sharper copy.")
        return done("reupload")
    add("Read", "Read confidence", "pass", "All key fields read clearly")

    # 2. REQUIRED FIELDS: anything missing is rejected, no guessing
    miss = [f for f in REQUIRED if not inv.get(f)] + ([] if inv.get("lines") else ["line items"])
    if miss:
        miss = [FIELD.get(m, m) for m in miss]
        add("Document", "Required fields", "fail", f"Missing: {', '.join(miss)}",
            msg=f"Required details are missing: {', '.join(miss)}. Please add them and resubmit.")
        return done()
    add("Document", "Required fields", "pass", "All required fields present")

    # 3. VENDOR
    nv, vend = norm(inv["vendor_name"]), None
    for v in ctx["vendors"].values():
        if norm(v["name"]) == nv: vend = v
    if vend: add("Vendor", "Approved vendor", "pass", f"{vend['name']} is approved")
    else:
        best = max(ctx["vendors"].values(), key=lambda v: difflib.SequenceMatcher(None, norm(v["name"]), nv).ratio())
        if difflib.SequenceMatcher(None, norm(best["name"]), nv).ratio() >= S["lookalike_ratio"]:
            vend = best
            add("Vendor", "Lookalike vendor name", "flag", f"'{inv['vendor_name']}' is close to, but not exactly, '{best['name']}'", 6)
        else:
            add("Vendor", "Approved vendor", "fail", f"'{inv['vendor_name']}' is not on the approved list",
                msg="This vendor is not on our approved list.")
            return done()
    if ctx.get("uploader") and ctx["uploader"] != vend["id"]:
        add("Vendor", "Uploader identity", "flag", f"Uploaded by {ctx['uploader']} but invoice is from {vend['name']}", 6)
    g = inv["vendor_gstin"]
    if not GST_RE.match(g): add("Vendor", "GSTIN", "fail", f"GSTIN {g} has an invalid format", msg="The GSTIN on the invoice is not valid.")
    elif g != vend["gstin"]:
        add("Vendor", "GSTIN", "flag", f"GSTIN {g} differs from registered {vend['gstin']} (PAN part {'matches' if g[2:12] == vend['gstin'][2:12] else 'differs'})", 5)
    else: add("Vendor", "GSTIN", "pass", "GSTIN matches the registered one")
    acct = re.sub(r"\D", "", str(inv["bank_account"]))
    if acct != vend["account"]:
        st["admin"] = True
        add("Vendor", "Bank account", "flag", f"Invoice shows {inv.get('bank_name','')} ••{acct[-4:]}; on file is {vend['bank_name']} ••{vend['account'][-4:]}. Confirm with the vendor before paying", 8)
    else: add("Vendor", "Bank account", "pass", "Bank account matches the one on file")
    ifsc = str(inv.get("bank_ifsc", "")).upper()
    if not IFSC_RE.match(ifsc) or (BANKS.get(ifsc[:4]) and BANKS[ifsc[:4]] not in str(inv.get("bank_name", "")).lower()):
        add("Vendor", "IFSC", "flag", f"IFSC {ifsc or 'missing'} is invalid or does not match the bank name", 3)
    if vend["rejections"] >= S["rejection_limit"]:
        add("Vendor", "Vendor history", "flag", f"{vend['rejections']} past rejections", 4)

    # 4. DOCUMENT
    L, tol = inv["lines"], S["rounding"]
    if abs(sum(l["amount"] for l in L) - inv["subtotal"]) > tol or abs(inv["subtotal"] + inv["tax"] - inv["total"]) > tol:
        add("Document", "Totals add up", "fail", f"Line items give {inr(sum(l['amount'] for l in L))} but the invoice total is {inr(inv['total'])}",
            msg="The invoice totals do not add up. Please correct and resubmit.")
    else: add("Document", "Totals add up", "pass", "Lines, tax and total agree")
    idate = date.fromisoformat(inv["date"])
    if idate > today: add("Document", "Invoice date", "fail", "Invoice is dated in the future", msg="The invoice date is in the future.")
    else: add("Document", "Invoice date", "pass", "Date is valid")
    if inv.get("currency", "INR") != "INR": add("Document", "Currency", "fail", f"Invoice is in {inv['currency']}, PO is in INR", msg="Invoice currency must be INR.")
    if norm(inv.get("buyer_name", "")) != norm(ctx["buyer"]["name"]) or inv.get("buyer_gstin") != ctx["buyer"]["gstin"]:
        add("Document", "Bill-to", "flag", f"Billed to '{inv.get('buyer_name')}' / {inv.get('buyer_gstin')}, not {ctx['buyer']['name']}", 4)
    else: add("Document", "Bill-to", "pass", "Billed to Dutpal Industries")
    active = [h for h in ctx["history"] if h["vendor_id"] == vend["id"] and h["status"] in ("approved", "review", "admin_pending")]
    dup = [h for h in active if h["invoice_no"] == inv["invoice_no"]]
    near = [h for h in active if h["total"] == inv["total"] and h["date"] == inv["date"] and h["invoice_no"] != inv["invoice_no"]]
    if dup: add("Document", "Duplicate", "fail", f"{inv['invoice_no']} was already submitted", msg=f"Invoice {inv['invoice_no']} has already been submitted.")
    if dup: return done()
    if near: add("Document", "Near-duplicate", "flag", f"Same amount and date as {near[0]['invoice_no']}", 5)
    else: add("Document", "Duplicate", "pass", "Not seen before")

    # 5. PO
    po = ctx["pos"].get(str(inv["po_ref"]))
    if not po:
        add("PO", "PO found", "fail", f"PO {inv['po_ref']} does not exist", msg="The PO number on the invoice was not found.")
        return done()
    if po["vendor_id"] != vend["id"]:
        add("PO", "PO vendor", "flag", f"PO {po['no']} belongs to {ctx['vendors'][po['vendor_id']]['name']}", 6)
        return done()
    add("PO", "PO vendor", "pass", f"PO {po['no']} belongs to this vendor")
    if po["status"] != "open":
        add("PO", "PO open", "fail", f"PO {po['no']} is {po['status']}", msg=f"PO {po['no']} is {po['status']}.")
        return done()
    if idate < date.fromisoformat(po["date"]): add("PO", "PO date", "fail", "Invoice is dated before the PO", msg="The invoice is dated before the PO.")
    po_inv = [h for h in active if h["po_ref"] == po["no"]]
    if len(po_inv) >= S["max_invoices_per_po"]:
        add("PO", "Invoices on PO", "flag", f"This would be invoice {len(po_inv)+1} on PO {po['no']}", 4)
    price = {norm(l["item"]): l["price"] for l in po["lines"]}
    unknown = [l["item"] for l in L if norm(l["item"]) not in price]
    if unknown: add("PO", "Line items", "fail", f"Not on the PO: {', '.join(unknown)}", msg=f"These items are not on the PO: {', '.join(unknown)}.")
    else: add("PO", "Line items", "pass", "All items are on the PO")
    left = po["total"] - sum(h["subtotal"] for h in po_inv)

    # 6. GOODS RECEIVED (3-way match)
    grn = ctx["grn"].get(po["no"])
    if not grn: add("GRN", "Goods received", "fail", "No goods receipt recorded", msg="We have no record of these goods being received.")
    else:
        late_amt = 0
        for l in L:
            k, r = norm(l["item"]), grn.get(norm(l["item"]))
            if not r: continue
            ok = r["received"] - r["rejected"] - sum(x["qty"] for h in po_inv for x in h["lines"] if norm(x["item"]) == k)
            ordered = next(x["qty"] for x in po["lines"] if norm(x["item"]) == k)
            if l["qty"] > ok:
                add("GRN", "Goods received", "fail", f"Billed {l['qty']} {l['item']} but only {ok} received", msg=f"You billed {l['qty']} units of {l['item']} but {ok} were received. Please resubmit for {ok}.")
            else: add("GRN", "Goods received", "pass", f"{l['qty']} billed, {ok} available")
            if r["received"] > ordered: add("GRN", "Over-delivery", "flag", f"{r['received']} received vs {ordered} ordered", 3)
            if r["rejected"]: add("GRN", "Rejected goods", "flag", f"{r['rejected']} {l['item']} rejected on receipt", 3)
            days = (date.fromisoformat(r["delivered"]) - date.fromisoformat(po["promised"])).days
            if days > 0:
                late_amt += l["qty"] * price[k] * min(days * S["late_pct_per_day"], S["late_cap_pct"]) / 100
                st["late"] = {"days": days, "suggested_penalty": round(late_amt)}
                add("GRN", "Late delivery", "flag", f"Delivered {days} days late. Suggested cut {inr(late_amt)} (needs reviewer approval)", 2)

    # 7. TOLERANCE: percentage and rupee limits together, the stricter wins
    exp = sum(price[norm(l["item"])] * l["qty"] for l in L if norm(l["item"]) in price)
    diff = inv["subtotal"] - exp
    a, r = min(S["auto_pct"] / 100 * exp, S["auto_rs"]), min(S["review_pct"] / 100 * exp, S["review_rs"])
    if po_inv and inv["subtotal"] > left + r: add("Tolerance", "PO balance", "fail", f"Only {inr(left)} left on the PO", msg=f"Only {inr(left)} is left on this PO.")
    if diff <= max(a, tol) and diff >= -r: add("Tolerance", "Amount vs PO", "pass", f"{inr(abs(diff))} {'over' if diff > 0 else 'under'}, within auto-approve limit of {inr(a)}")
    elif diff < -r: add("Tolerance", "Amount vs PO", "flag", f"Billed {inr(-diff)} less than the PO", 1)
    elif diff <= r: add("Tolerance", "Amount vs PO", "flag", f"{inr(diff)} over the PO; auto-approve limit is {inr(a)}", 3 if diff > r / 2 else 2)
    else: add("Tolerance", "Amount vs PO", "fail", f"Overbilled by {inr(diff)}; limit is {inr(r)}", msg=f"The amount is {inr(diff)} above the PO, which is more than allowed.")
    return done()