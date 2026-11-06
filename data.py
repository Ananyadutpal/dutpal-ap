"""Fake Dutpal Industries data: the company 'system' (vendors, POs, goods receipts, settings)."""
import copy

BUYER = {"name": "Dutpal Industries Pvt Ltd", "gstin": "36AADCD1234A1Z5"}

VENDORS = {
 "V1": {"id": "V1", "name": "Acme Private Limited", "gstin": "36AABCA1234F1Z5", "bank_name": "HDFC",
        "account": "50100012344821", "ifsc": "HDFC0001234", "rejections": 0},
 "V2": {"id": "V2", "name": "Beta Supplies Pvt Ltd", "gstin": "36AAACB5678C1Z2", "bank_name": "SBI",
        "account": "30112233445566", "ifsc": "SBIN0004321", "rejections": 0},
 "V3": {"id": "V3", "name": "Kiran Foods Pvt Ltd", "gstin": "36AAFCK9012D1Z7", "bank_name": "Axis",
        "account": "91702003345512", "ifsc": "UTIB0000567", "rejections": 0},
 "V4": {"id": "V4", "name": "Nova Pack Ltd", "gstin": "36AAHCN3456E1Z1", "bank_name": "Kotak",
        "account": "7412589630", "ifsc": "KKBK0000258", "rejections": 0},
}

def _po(no, vid, d, status, promised, item, qty, price):
    return {"no": no, "vendor_id": vid, "date": d, "status": status, "promised": promised,
            "lines": [{"item": item, "qty": qty, "price": price}], "total": qty * price}

POS = {p["no"]: p for p in [
 _po("7770", "V1", "2026-08-01", "closed", "2026-08-20", "Bearing set", 50, 4800),
 _po("7781", "V1", "2026-10-01", "open", "2026-10-15", "Bearing set", 100, 4800),
 _po("7790", "V2", "2026-10-02", "open", "2026-10-16", "Steel plate", 50, 2200),
 _po("7802", "V4", "2026-10-03", "open", "2026-10-18", "Carton box", 100, 350),
 _po("7810", "V3", "2026-10-01", "open", "2026-10-10", "Rice bag 25kg", 200, 1450),
 _po("7820", "V2", "2026-10-04", "open", "2026-10-20", "Hydraulic pump", 10, 50000),
]}

def _g(item, rec, rej, delivered):
    return {item.lower(): {"received": rec, "rejected": rej, "delivered": delivered}}

GRN = {
 "7781": _g("Bearing set", 100, 0, "2026-10-14"),
 "7790": _g("Steel plate", 50, 0, "2026-10-15"),
 "7802": _g("Carton box", 80, 0, "2026-10-17"),          # only 80 of 100 arrived
 "7810": _g("Rice bag 25kg", 200, 0, "2026-10-16"),      # 6 days late
 "7820": _g("Hydraulic pump", 10, 0, "2026-10-19"),
}

SETTINGS = {"auto_pct": 1, "auto_rs": 100, "review_pct": 5, "review_rs": 1000, "rounding": 2,
            "max_invoices_per_po": 10, "rejection_limit": 3, "pending_days": 5,
            "min_confidence": 0.80, "lookalike_ratio": 0.85,
            "late_pct_per_day": 1, "late_cap_pct": 10}   # late rates only suggest an amount

def make_ctx(uploader=None):
    return copy.deepcopy({"buyer": BUYER, "vendors": VENDORS, "pos": POS, "grn": GRN,
                          "settings": SETTINGS, "history": [], "uploader": uploader})
