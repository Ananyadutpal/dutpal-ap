"""Reads an invoice (PDF or photo) with Gemini. The AI only EXTRACTS; it never approves or rejects."""
import base64, json, os, pathlib, re, sys, time
import requests

def _load_env():
    f = pathlib.Path(__file__).with_name(".env")
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
_load_env()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
CONF_FIELDS = ["vendor_name", "vendor_gstin", "invoice_no", "date", "po_ref", "total", "bank_account"]
PROMPT = f"""You read Indian GST tax invoices. Return ONLY JSON with exactly these keys:
vendor_name, vendor_gstin, buyer_name, buyer_gstin, invoice_no, date (YYYY-MM-DD), po_ref (the purchase order number),
currency (ISO code), lines (array of {{item, qty, unit_price, amount}}), subtotal, tax, total (plain numbers, no symbols or commas),
bank_name, bank_account, bank_ifsc, and confidence (an object with a number from 0 to 1 for each of: {", ".join(CONF_FIELDS)}).
Use null for anything not on the document. Never guess, correct or invent a value: copy exactly what is printed, even if it looks wrong.
Give a LOW confidence when text is blurry, cut off, handwritten, overwritten or ambiguous."""

class ReadError(Exception): pass

def _num(x):
    if x is None or x == "": return None
    try: return float(re.sub(r"[^\d.\-]", "", str(x)))
    except ValueError: return None

def parse(text):
    """Turn the model's reply into the dict our rules engine expects."""
    try: d = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip())
    except json.JSONDecodeError: raise ReadError("The reply was not valid JSON")
    for k in ("subtotal", "tax", "total"): d[k] = _num(d.get(k))
    d["lines"] = [{"item": l.get("item"), "qty": _num(l.get("qty")), "unit_price": _num(l.get("unit_price")),
                   "amount": _num(l.get("amount"))} for l in (d.get("lines") or []) if isinstance(l, dict)]
    for k in ("po_ref", "invoice_no", "bank_account"):
        if d.get(k) is not None: d[k] = str(d[k]).strip()
    d["currency"] = d.get("currency") or "INR"
    d["confidence"] = {k: float(v) for k, v in (d.get("confidence") or {}).items() if _num(v) is not None}
    for k in ("subtotal", "tax", "total"): d[k] = d[k] or 0
    return d

def ask(parts, key=None):
    key = key or os.getenv("GEMINI_API_KEY")
    if not key: raise ReadError("GEMINI_API_KEY is not set (put it in the .env file)")
    body = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    for attempt in range(2):
        r = requests.post(URL.format(MODEL), headers={"x-goog-api-key": key}, json=body, timeout=90)
        if r.status_code in (429, 503) and attempt == 0: time.sleep(4); continue
        break
    if r.status_code != 200: raise ReadError(f"Gemini returned {r.status_code}: {r.text[:300]}")
    try: return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError): raise ReadError("Gemini returned no text (the file may have been blocked)")

def extract(data: bytes, mime: str):
    return parse(ask([{"text": PROMPT}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]))

def extract_file(path):
    p = pathlib.Path(path); return extract(p.read_bytes(), MIME[p.suffix.lower()])

if __name__ == "__main__":      # python reader.py some_invoice.pdf  -> read it, then decide it
    from data import make_ctx
    from engine import evaluate
    inv = extract_file(sys.argv[1]); print(json.dumps(inv, indent=1, ensure_ascii=False))
    r = evaluate(inv, make_ctx())
    print(f"\nDECISION: {r['outcome'].upper()}  risk={r['risk']} ({r['points']} pts)\n{r['summary']}\nVendor sees: {r['vendor_message']}")
    for c in r["checks"]: print(f"  [{c['status']:4}] {c['stage']:9} {c['name']:22} {c['detail']}")
