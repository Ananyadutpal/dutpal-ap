"""Generates the demo invoices (PDFs and photos). Run: python make_invoices.py"""
import os, random
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from data import BUYER, VENDORS

STYLES = {  # four vendor layouts: different alignment, colours, labels and column order
 "V1": dict(accent=(20, 70, 140), right=False, inv="Invoice No", po="PO No", cols=["item", "qty", "unit_price", "amount"]),
 "V2": dict(accent=(30, 30, 30), right=True, inv="Bill #", po="Purchase Order", cols=["item", "unit_price", "qty", "amount"]),
 "V3": dict(accent=(150, 40, 40), right=False, inv="Tax Invoice No.", po="Your Order Ref", cols=["item", "qty", "unit_price", "amount"]),
 "V4": dict(accent=(20, 110, 70), right=True, inv="Inv. No", po="PO Reference", cols=["item", "qty", "unit_price", "amount"]),
}
TITLE = {"item": "Description", "qty": "Qty", "unit_price": "Rate", "amount": "Amount"}
XPOS = [40, 330, 440, 555]

def mk(vid, po, item, qty, price, no, **kw):
    v, sub = VENDORS[vid], qty * price
    s = dict(vid=vid, vendor_name=v["name"], vendor_gstin=v["gstin"], buyer_name=BUYER["name"], buyer_gstin=BUYER["gstin"],
             invoice_no=no, date="2026-10-25", po_ref=po, item=item, qty=qty, price=price, sub=sub, tax=sub * 0.18,
             total=sub * 1.18, bank_name=v["bank_name"], bank_account=v["account"], bank_ifsc=v["ifsc"])
    s.update(kw); return s

def ops(s):
    st, o, y = STYLES[s["vid"]], [], 50
    def T(x, y, t, size=10, bold=False, color=(0, 0, 0), right=False): o.append((x, y, str(t), size, bold, color, right))
    hx = 555 if st["right"] else 40
    T(hx, y, s["vendor_name"], 18, True, st["accent"], st["right"]); y += 22
    T(hx, y, f"GSTIN: {s['vendor_gstin']}", 10, right=st["right"]); y += 14
    T(hx, y, "TAX INVOICE", 11, True, (90, 90, 90), st["right"]); y += 34
    for k, v in [(st["inv"], s["invoice_no"] or ""), ("Date", s["date"]), (st["po"], s["po_ref"])]:
        T(40, y, k + ":", 10, True); T(160, y, v); y += 15
    y += 10; T(40, y, "Bill To:", 10, True); y += 14; T(40, y, s["buyer_name"]); y += 14
    T(40, y, f"GSTIN: {s['buyer_gstin']}"); y += 34
    for key, x in zip(st["cols"], XPOS): T(x, y, TITLE[key], 10, True, st["accent"], key != "item")
    y += 18
    vals = {"item": s["item"], "qty": s["qty"], "unit_price": f"{s['price']:,.2f}", "amount": f"{s['sub']:,.2f}"}
    for key, x in zip(st["cols"], XPOS): T(x, y, vals[key], 10, right=key != "item")
    y += 40
    for lab, val, b in [("Subtotal", s["sub"], False), ("GST @18% (CGST 9% + SGST 9%)", s["tax"], False), ("Total (INR)", s["total"], True)]:
        T(330, y, lab, 10, b); T(555, y, f"{val:,.2f}", 10, b, right=True); y += 16
    y += 30; T(40, y, f"Bank: {s['bank_name']}   A/c No: {s['bank_account']}   IFSC: {s['bank_ifsc']}", 10)
    T(40, y + 40, "Thank you for your business.", 9, color=(110, 110, 110))
    return o

def save_pdf(s, path):
    c = canvas.Canvas(path, pagesize=A4)
    for x, y, t, size, bold, col, right in ops(s):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size); c.setFillColorRGB(*[v / 255 for v in col])
        (c.drawRightString if right else c.drawString)(x, 842 - y, t)
    c.save()

def font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try: return ImageFont.truetype(f, size)
        except OSError: pass
    return ImageFont.load_default()

def save_photo(s, path, rot=3, blur=0, crop=False):
    k = 1.6; paper = Image.new("RGB", (int(595 * k), int(842 * k)), (247, 245, 238)); d = ImageDraw.Draw(paper)
    for x, y, t, size, bold, col, right in ops(s):
        f = font(int(size * k * 0.92)); w = d.textlength(t, font=f)
        d.text((x * k - (w if right else 0), y * k), t, font=f, fill=col)
    if crop: paper = paper.crop((0, 0, paper.width, int(paper.height * 0.5)))
    desk = Image.new("RGB", (paper.width + 160, paper.height + 160), (70, 62, 55))
    r = paper.rotate(rot, expand=True, fillcolor=(70, 62, 55)); desk.paste(r, (60, 60))
    img = ImageEnhance.Brightness(desk).enhance(0.92)
    img = Image.blend(img, Image.merge("RGB", [Image.effect_noise(img.size, 22)] * 3), 0.05)
    if blur: img = img.resize((img.width // 3, img.height // 3)).filter(ImageFilter.GaussianBlur(blur)).resize(img.size)
    img.convert("RGB").save(path, quality=80)

M = mk
SCEN = [
 ("01_happy_path_beta.pdf", M("V2", "7790", "Steel plate", 50, 2200, "BS-1001")),
 ("02_edge1_bank_changed_acme.pdf", M("V1", "7781", "Bearing set", 100, 4800, "INV-2041", bank_name="ICICI", bank_account="0123 4567 7305", bank_ifsc="ICIC0001234")),
 ("03_edge2_partial_delivery_nova.pdf", M("V4", "7802", "Carton box", 100, 350, "NP-077")),
 ("04_edge3_late_delivery_kiran.pdf", M("V3", "7810", "Rice bag 25kg", 200, 1450, "KF-310")),
 ("05_edge4_rupee_cap_beta.pdf", M("V2", "7820", "Hydraulic pump", 10, 50080, "BS-1010")),
 ("06_missing_invoice_number.pdf", M("V2", "7790", "Steel plate", 50, 2200, None)),
 ("07_blurry_photo.jpg", M("V2", "7790", "Steel plate", 50, 2200, "BS-1004"), dict(blur=5, rot=-4, crop=True)),
 ("08_unknown_vendor.pdf", M("V1", "7781", "Bearing set", 100, 4800, "ZT-12", vendor_name="Zed Traders", vendor_gstin="36AAAZT1111Z1Z4")),
 ("09_lookalike_vendor_acrne.pdf", M("V1", "7781", "Bearing set", 100, 4800, "INV-2050", vendor_name="Acrne Private Limited")),
 ("10_other_vendors_po.pdf", M("V1", "7790", "Steel plate", 50, 2200, "INV-2060")),
 ("11_closed_po.pdf", M("V1", "7770", "Bearing set", 50, 4800, "INV-1990")),
 ("12_fake_po_number.pdf", M("V2", "9999", "Steel plate", 50, 2200, "BS-1005")),
 ("13_totals_do_not_add_up.pdf", M("V2", "7790", "Steel plate", 50, 2200, "BS-1006", total=134800.0)),
 ("14_wrong_bill_to.pdf", M("V2", "7790", "Steel plate", 50, 2200, "BS-1007", buyer_name="Other Corp Ltd", buyer_gstin="27AAACO1111A1Z5")),
 ("15_overbilled_beyond_limit.pdf", M("V2", "7790", "Steel plate", 50, 2240, "BS-1002")),
 ("17_perfect_invoice_acme.pdf", M("V1", "7850", "Bearing set", 40, 4800, "INV-3001")),
 ("18_perfect_invoice_nova.pdf", M("V4", "7860", "Carton box", 60, 350, "NP-120")),
 ("16_happy_path_photo_kiran.jpg", M("V3", "7830", "Cooking oil 15L", 40, 2100, "KF-401"), dict(rot=2)),
]
if __name__ == "__main__":
    os.makedirs("test_invoices", exist_ok=True); random.seed(7)
    for name, s, *opt in SCEN:
        p = os.path.join("test_invoices", name)
        save_photo(s, p, **(opt[0] if opt else {})) if name.endswith(".jpg") else save_pdf(s, p)
    print(f"Created {len(SCEN)} invoices in test_invoices/")