"""Dutpal AP web app. Stage 3: logins, database, vendor portal, background invoice processing."""
import json, os, pathlib, sqlite3, threading, time, uuid
from datetime import datetime
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import data, engine, reader

BASE = pathlib.Path(__file__).parent
DB, UPLOADS = os.getenv("DB_PATH", str(BASE / "app.db")), BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
PASSWORD = os.getenv("DEMO_PASSWORD", "dutpal")
OFFLINE = os.getenv("DEMO_OFFLINE") == "1"      # dev only: skips Gemini so the app can be tested without a key
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-secret-change-me"), max_age=86400)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

def q(sql, args=(), one=False, write=False):
    c = sqlite3.connect(DB, timeout=10); c.row_factory = sqlite3.Row
    cur = c.execute(sql, args)
    out = cur.fetchone() if one else cur.fetchall()
    if write: c.commit()
    c.close(); return out

def init(reset=False):
    q("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)", write=True)
    q("""CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY, vendor_id TEXT, filename TEXT, path TEXT, status TEXT,
         created TEXT, invoice_no TEXT, po_ref TEXT, total REAL, subtotal REAL, date TEXT, lines TEXT, extracted TEXT, result TEXT)""", write=True)
    q("""CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, invoice_id TEXT, detail TEXT)""", write=True)
    for t in ("DELETE", "UPDATE"):
        q(f"CREATE TRIGGER IF NOT EXISTS audit_no_{t.lower()} BEFORE {t} ON audit BEGIN SELECT RAISE(ABORT,'The audit log cannot be changed or deleted'); END", write=True)
    try: q("ALTER TABLE invoices ADD COLUMN live TEXT", write=True)
    except sqlite3.OperationalError: pass
    if reset or not q("SELECT 1 FROM kv", one=True):
        for k, v in dict(vendors=data.VENDORS, pos=data.POS, grn=data.GRN, settings=data.SETTINGS).items():
            q("REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)), write=True)
        if reset: q("DELETE FROM invoices", write=True)

def kvd(k, d):
    r = q("SELECT v FROM kv WHERE k=?", (k,), one=True); return json.loads(r["v"]) if r else d
def setkv(k, v): q("REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v)), write=True)
def kv(k): return json.loads(q("SELECT v FROM kv WHERE k=?", (k,), one=True)["v"])
def audit(actor, action, inv_id="", detail=""):
    q("INSERT INTO audit(ts,actor,action,invoice_id,detail) VALUES(?,?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), actor, action, inv_id, detail), write=True)

def ctx(uploader):
    hist = [dict(vendor_id=r["vendor_id"], invoice_no=r["invoice_no"], po_ref=r["po_ref"], status=r["status"], total=r["total"],
                 date=r["date"], subtotal=r["subtotal"], lines=json.loads(r["lines"] or "[]"))
            for r in q("SELECT * FROM invoices WHERE status IN ('approved','review','admin_pending')")]
    return dict(buyer=data.BUYER, vendors=kv("vendors"), pos=kv("pos"), grn=kv("grn"), settings=kv("settings"), history=hist, uploader=uploader)

def offline_extract(filename):
    from make_invoices import SCEN
    s = next((x[1] for x in SCEN if x[0] == filename), None)
    if not s: raise reader.ReadError("Offline mode only knows the demo invoice filenames")
    ln = [dict(item=s["item"], qty=s["qty"], unit_price=s["price"], amount=s["sub"])]
    return dict(vendor_name=s["vendor_name"], vendor_gstin=s["vendor_gstin"], buyer_name=s["buyer_name"], buyer_gstin=s["buyer_gstin"],
                invoice_no=s["invoice_no"], date=s["date"], po_ref=s["po_ref"], currency="INR", lines=ln, subtotal=s["sub"], tax=s["tax"],
                total=s["total"], bank_name=s["bank_name"], bank_account=s["bank_account"], bank_ifsc=s["bank_ifsc"],
                confidence={"total": 0.4} if "blurry" in filename else {})

STAGES = ["Read", "Vendor", "Document", "PO", "GRN", "Tolerance", "Decision"]
PACE = float(os.getenv("STAGE_PACE", "0.25"))   # rules run in milliseconds; the reveal is paced so people can follow each stage
def setlive(i, live): q("UPDATE invoices SET live=? WHERE id=?", (json.dumps(live), i), write=True)

NOT_COUNTED = {"Required fields", "Read confidence", "Totals add up", "Invoice date", "Currency"}   # honest slips are not red flags
def bump(vid):
    vs = kvd("vendors", {})
    if vid in vs: vs[vid]["rejections"] = vs[vid].get("rejections", 0) + 1; setkv("vendors", vs)

def process(inv_id):
    row = q("SELECT * FROM invoices WHERE id=?", (inv_id,), one=True)
    live = [dict(stage=s, status="pending", detail="") for s in STAGES]; live[0]["status"] = "running"; setlive(inv_id, live)
    try:
        t = time.time()
        inv = offline_extract(row["filename"]) if OFFLINE else reader.extract_file(row["path"])
        read_s = round(time.time() - t, 1)
        res = engine.evaluate(inv, ctx(row["vendor_id"])); res["read_seconds"] = read_s
        by = {}
        for c in res["checks"]: by.setdefault(c["stage"], []).append(c)
        for i, s in enumerate(STAGES):
            cs = by.get(s, [])
            if s == "Decision": st, det = res["outcome"], res["summary"]
            elif not cs: st, det = "skipped", "Not reached"
            else:
                st = "fail" if any(c["status"] == "fail" for c in cs) else "flag" if any(c["status"] == "flag" for c in cs) else "pass"
                det = "; ".join(c["detail"] for c in cs if c["status"] != "pass") or f"{len(cs)} checks passed"
            live[i].update(status=st, detail=det[:200], secs=read_s if s == "Read" else None)
            if i + 1 < len(STAGES): live[i + 1]["status"] = "running"
            setlive(inv_id, live)
            if i + 1 < len(STAGES): time.sleep(PACE)
        q("""UPDATE invoices SET status=?, invoice_no=?, po_ref=?, total=?, subtotal=?, date=?, lines=?, extracted=?, result=? WHERE id=?""",
          (res["outcome"], inv.get("invoice_no"), inv.get("po_ref"), inv.get("total"), inv.get("subtotal"), inv.get("date"),
           json.dumps(inv["lines"]), json.dumps(inv), json.dumps(res), inv_id), write=True)
        if res["outcome"] == "rejected" and any(c["status"] == "fail" and c["name"] not in NOT_COUNTED for c in res["checks"]): bump(row["vendor_id"])
        audit("system", "processed", inv_id, f"{res['outcome']} | {res['risk']} | {res['summary'][:200]}")
    except Exception as e:
        for l in live:
            if l["status"] == "running": l.update(status="fail", detail=str(e)[:200])
        setlive(inv_id, live)
        q("UPDATE invoices SET status='error', result=? WHERE id=?",
          (json.dumps(dict(vendor_message="We could not process this invoice. Please try again.", summary=str(e), checks=[])), inv_id), write=True)
        audit("system", "error", inv_id, str(e)[:300])

USERS = {"vendor": None, "reviewer": "reviewer", "admin": "admin"}
def me(request, role=None):
    u = request.session.get("user")
    if not u or (role and u["role"] != role): raise HTTPException(401, "Please sign in")
    return u

@app.post("/api/login")
async def login(request: Request):
    b = await request.json()
    role, vid = b.get("role"), b.get("vendor_id")
    if role not in USERS or b.get("password") != PASSWORD: raise HTTPException(401, "Wrong role or password")
    if role == "vendor" and vid not in kv("vendors"): raise HTTPException(401, "Choose a vendor")
    name = kv("vendors")[vid]["name"] if role == "vendor" else role.title()
    request.session["user"] = dict(role=role, vendor_id=vid if role == "vendor" else None, name=name)
    audit(name, "login"); return request.session["user"]

@app.post("/api/logout")
async def logout(request: Request): request.session.clear(); return {"ok": True}

@app.get("/api/vendors-public")
def vendors_public(): return [dict(id=v["id"], name=v["name"]) for v in kv("vendors").values()]

@app.get("/api/me")
def whoami(request: Request): return me(request)

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    u = me(request, "vendor"); ext = pathlib.Path(file.filename).suffix.lower()
    body = await file.read()
    if ext not in reader.MIME or len(body) > 10_000_000: raise HTTPException(400, "Upload a PDF, JPG or PNG under 10 MB")
    inv_id = uuid.uuid4().hex[:10]; path = UPLOADS / f"{inv_id}{ext}"; path.write_bytes(body)
    q("INSERT INTO invoices(id,vendor_id,filename,path,status,created) VALUES(?,?,?,?,?,?)",
      (inv_id, u["vendor_id"], file.filename, str(path), "processing", datetime.now().isoformat(timespec="seconds")), write=True)
    audit(u["name"], "uploaded", inv_id, file.filename)
    threading.Thread(target=process, args=(inv_id,), daemon=True).start(); return {"id": inv_id}

LABEL = {"processing": "Checking", "approved": "Approved", "review": "In review", "rejected": "Needs changes", "reupload": "Needs changes", "error": "Needs changes", "admin_pending": "In review"}
@app.get("/api/my-invoices")     # vendors only ever get a status and a message, never internal checks or risk
def my_invoices(request: Request):
    u = me(request, "vendor")
    return [dict(id=r["id"], filename=r["filename"], invoice_no=r["invoice_no"], status=r["status"], label=LABEL[r["status"]], created=r["created"],
                 message=(json.loads(r["result"] or "{}").get("vendor_message") if r["status"] in ("rejected", "reupload", "error") else None))
            for r in q("SELECT * FROM invoices WHERE vendor_id=? ORDER BY created DESC", (u["vendor_id"],))]

def vname(vid): return kv("vendors").get(vid, {}).get("name", vid)
def staff(request):
    u = me(request)
    if u["role"] not in ("reviewer", "admin"): raise HTTPException(403, "Staff only")
    return u
def J(x): return json.loads(x) if x else None

@app.get("/api/r/invoices")
def r_list(request: Request):
    staff(request); days, now, out = kv("settings")["pending_days"], datetime.now(), []
    for r in q("SELECT * FROM invoices ORDER BY created DESC"):
        res, age = J(r["result"]) or {}, (datetime.now() - datetime.fromisoformat(r["created"])).days
        out.append(dict(id=r["id"], vendor=vname(r["vendor_id"]), invoice_no=r["invoice_no"], filename=r["filename"], status=r["status"], created=r["created"],
                        total=r["total"], risk=res.get("risk"), points=res.get("points"), age=age,
                        overdue=r["status"] in ("review", "admin_pending") and age >= days))
    return out

@app.get("/api/r/invoice/{iid}")
def r_get(iid: str, request: Request):
    staff(request); r = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    if not r: raise HTTPException(404, "Not found")
    return dict(id=iid, vendor=vname(r["vendor_id"]), status=r["status"], filename=r["filename"], ext=pathlib.Path(r["path"]).suffix.lower(),
                live=J(r["live"]), extracted=J(r["extracted"]), result=J(r["result"]),
                audit=[dict(r2) for r2 in q("SELECT ts,actor,action,detail FROM audit WHERE invoice_id=? ORDER BY id", (iid,))])

@app.get("/api/r/file/{iid}")
def r_file(iid: str, request: Request):
    staff(request); r = q("SELECT path FROM invoices WHERE id=?", (iid,), one=True)
    if not r: raise HTTPException(404, "Not found")
    return FileResponse(r["path"], media_type=reader.MIME[pathlib.Path(r["path"]).suffix.lower()])

@app.post("/api/r/invoice/{iid}/action")
async def r_action(iid: str, request: Request):
    u = staff(request); b = await request.json(); a, note = b.get("action"), (b.get("note") or "").strip()
    r = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    if not r: raise HTTPException(404, "Not found")
    res = J(r["result"]) or {}
    if a not in ("approve", "reject", "note", "send_admin"): raise HTTPException(400, "Unknown action")
    if a != "note" and r["status"] != "review": raise HTTPException(409, "This invoice is not waiting for a reviewer")
    if a in ("reject", "note") and not note: raise HTTPException(400, "Please write a note")
    if a == "approve" and res.get("needs_admin"): raise HTTPException(400, "Bank-detail changes need Admin approval")
    if a == "note": audit(u["name"], "note", iid, note); return {"ok": True}
    status = {"approve": "approved", "reject": "rejected", "send_admin": "admin_pending"}[a]
    payable = r["total"]
    if a == "approve" and b.get("cut_penalty") and res.get("late"): payable = round(r["total"] - res["late"]["suggested_penalty"])
    res["decision"] = dict(by=u["name"], action=a, note=note, at=datetime.now().isoformat(timespec="seconds"), payable=payable)
    if a == "reject": res["vendor_message"] = note; bump(r["vendor_id"])
    q("UPDATE invoices SET status=?, result=? WHERE id=?", (status, json.dumps(res), iid), write=True)
    audit(u["name"], a, iid, (note + f" | payable {payable}") if a == "approve" else note); return {"ok": True, "status": status}

def admin(request):
    u = me(request)
    if u["role"] != "admin": raise HTTPException(403, "Admin only")
    return u
def bad(m): raise HTTPException(400, m)
def mask(v): return dict(v, account="••" + v["account"][-4:])

@app.get("/api/s/settings")
def s_settings(request: Request): staff(request); return kv("settings")

@app.post("/api/a/settings")
async def a_settings(request: Request):
    u = admin(request); b = await request.json(); reason = (b.get("reason") or "").strip(); cur = kv("settings")
    if not reason: bad("Please give a reason for the change")
    for k, v in (b.get("changes") or {}).items():
        try: v = float(v)
        except (TypeError, ValueError): bad(f"{k} must be a number")
        if k in cur and v >= 0 and cur[k] != v:
            audit(u["name"], "setting", "", f"{k}: {cur[k]} -> {v:g} ({reason})"); cur[k] = int(v) if v == int(v) else v
    setkv("settings", cur); return cur

@app.get("/api/s/vendors")
def s_vendors(request: Request): staff(request); return [mask(v) for v in kv("vendors").values()]

@app.post("/api/s/vendors")
async def s_add(request: Request):
    u = staff(request); b = await request.json(); vs = kv("vendors")
    n, g, ifsc, acct = (b.get("name") or "").strip(), (b.get("gstin") or "").upper().strip(), (b.get("ifsc") or "").upper().strip(), "".join(c for c in str(b.get("account", "")) if c.isdigit())
    if not n or any(engine.norm(v["name"]) == engine.norm(n) for v in vs.values()): bad("Enter a new vendor name")
    if not engine.GST_RE.match(g): bad("GSTIN format is invalid")
    if not engine.IFSC_RE.match(ifsc) or not 6 <= len(acct) <= 18: bad("Check the IFSC and account number")
    vid = "V" + str(1 + max(int(k[1:]) for k in vs)); vs[vid] = dict(id=vid, name=n, gstin=g, bank_name=(b.get("bank_name") or "").strip(), account=acct, ifsc=ifsc, rejections=0)
    setkv("vendors", vs); audit(u["name"], "vendor_added", "", f"{n} ({vid})"); return {"id": vid}

@app.post("/api/s/vendors/{vid}/remove")
def s_remove(vid: str, request: Request):
    u = staff(request); vs = kv("vendors")
    if vid not in vs: raise HTTPException(404, "No such vendor")
    n = vs.pop(vid)["name"]; setkv("vendors", vs); audit(u["name"], "vendor_removed", "", n); return {"ok": True}

@app.post("/api/s/vendors/{vid}/bank")      # a reviewer can only REQUEST a bank change; an Admin must approve it
async def s_bank(vid: str, request: Request):
    u = staff(request); b = await request.json(); v = kv("vendors").get(vid)
    acct = "".join(c for c in str(b.get("account", "")) if c.isdigit())
    if not v or not engine.IFSC_RE.match((b.get("ifsc") or "").upper()) or not 6 <= len(acct) <= 18: bad("Check the vendor, IFSC and account number")
    ch = kvd("changes", []); ch.append(dict(id=uuid.uuid4().hex[:8], vendor_id=vid, vendor=v["name"], old=f"{v['bank_name']} ••{v['account'][-4:]}", new=f"{b.get('bank_name','')} ••{acct[-4:]}",
        bank_name=(b.get("bank_name") or "").strip(), account=acct, ifsc=b["ifsc"].upper(), by=u["name"], at=datetime.now().isoformat(timespec="seconds"), status="pending"))
    setkv("changes", ch); audit(u["name"], "bank_change_requested", "", f"{v['name']}: {ch[-1]['old']} -> {ch[-1]['new']}"); return {"ok": True}

@app.get("/api/s/changes")
def s_changes(request: Request):
    staff(request); return [{k: c[k] for k in ("id", "vendor", "old", "new", "by", "at")} for c in kvd("changes", []) if c["status"] == "pending"]

@app.post("/api/a/change/{cid}")
async def a_change(cid: str, request: Request):
    u = admin(request); b = await request.json(); ch = kvd("changes", []); c = next((x for x in ch if x["id"] == cid and x["status"] == "pending"), None)
    if not c or b.get("action") not in ("approve", "decline"): bad("Nothing to decide")
    c["status"] = "approved" if b["action"] == "approve" else "declined"
    if c["status"] == "approved":
        vs = kv("vendors"); vs[c["vendor_id"]].update(bank_name=c["bank_name"], account=c["account"], ifsc=c["ifsc"]); setkv("vendors", vs)
    setkv("changes", ch); audit(u["name"], "bank_change_" + c["status"], "", f"{c['vendor']}: {c['old']} -> {c['new']}"); return {"ok": True}

@app.get("/api/a/pending-invoices")
def a_pending(request: Request):
    admin(request); return [dict(id=r["id"], vendor=vname(r["vendor_id"]), invoice_no=r["invoice_no"], total=r["total"], summary=(J(r["result"]) or {}).get("summary"))
                            for r in q("SELECT * FROM invoices WHERE status='admin_pending' ORDER BY created")]

@app.post("/api/a/invoice/{iid}")
async def a_invoice(iid: str, request: Request):
    u = admin(request); b = await request.json(); r = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True); a = b.get("action")
    if not r or r["status"] != "admin_pending" or a not in ("approve", "decline"): bad("Nothing to decide")
    res = J(r["result"]); res["decision"] = dict(by=u["name"], action=a, note=(b.get("note") or "").strip(), at=datetime.now().isoformat(timespec="seconds"), payable=r["total"])
    if a == "decline": bump(r["vendor_id"]); res["vendor_message"] = "We could not verify the bank details on this invoice. Please contact Dutpal Industries AP and resubmit."
    st = "approved" if a == "approve" else "rejected"
    q("UPDATE invoices SET status=?, result=? WHERE id=?", (st, json.dumps(res), iid), write=True); audit(u["name"], "admin_" + a, iid, res["decision"]["note"]); return {"ok": True}

@app.post("/api/a/reset")
def a_reset(request: Request):
    u = admin(request); init(reset=True); setkv("changes", []); audit(u["name"], "demo_data_reloaded", "", "Invoices cleared, vendors and settings restored"); return {"ok": True}

@app.get("/api/s/stats")
def s_stats(request: Request):
    staff(request); rows = q("SELECT * FROM invoices"); c, amt, bv = {}, dict(approved=0, held=0, blocked=0), {}
    for r in rows:
        c[r["status"]] = c.get(r["status"], 0) + 1; t = r["total"] or 0
        k = "approved" if r["status"] == "approved" else "blocked" if r["status"] in ("rejected", "reupload") else "held"
        amt[k] += t; v = bv.setdefault(vname(r["vendor_id"]), dict(approved=0, held=0, blocked=0)); v[k] += 1
    secs = [(J(r["result"]) or {}).get("read_seconds") for r in rows]; secs = [x for x in secs if x is not None]
    return dict(counts=c, amounts=amt, vendors=bv, total=len(rows), avg_read=round(sum(secs) / len(secs), 1) if secs else None)

@app.get("/api/s/audit")
def s_audit(request: Request): staff(request); return [dict(r) for r in q("SELECT ts,actor,action,invoice_id,detail FROM audit ORDER BY id DESC LIMIT 400")]

def isodate(x):
    try: datetime.fromisoformat(str(x)); return str(x)[:10]
    except ValueError: bad("Dates must look like 2026-10-25")

@app.get("/api/s/pos")
def s_pos(request: Request):
    staff(request)
    return [dict(no=p["no"], vendor=vname(p["vendor_id"]), date=p["date"], promised=p["promised"], status=p["status"], total=p["total"],
                 lines=[dict(item=l["item"], qty=l["qty"], price=l["price"]) for l in p["lines"]]) for p in kv("pos").values()]

@app.post("/api/s/pos")
async def s_po_add(request: Request):
    u = staff(request); b = await request.json(); pos, no = kv("pos"), str(b.get("no", "")).strip()
    try: qty, price = float(b.get("qty")), float(b.get("price"))
    except (TypeError, ValueError): bad("Quantity and price must be numbers")
    item = (b.get("item") or "").strip()
    if not no or no in pos: bad("Enter a new PO number")
    if b.get("vendor_id") not in kv("vendors"): bad("Choose a vendor")
    if not item or qty <= 0 or price <= 0: bad("Enter an item, a quantity and a price above zero")
    qty = int(qty) if qty == int(qty) else qty
    pos[no] = dict(no=no, vendor_id=b["vendor_id"], date=isodate(b.get("date")), status="open", promised=isodate(b.get("promised")),
                   lines=[dict(item=item, qty=qty, price=price)], total=qty * price)
    setkv("pos", pos); audit(u["name"], "po_added", "", f"PO {no}: {item} x{qty} @ {price:g}"); return {"ok": True}

@app.post("/api/s/pos/{no}/status")
async def s_po_status(no: str, request: Request):
    u = staff(request); b = await request.json(); pos = kv("pos")
    if no not in pos or b.get("status") not in ("open", "closed", "cancelled"): bad("Nothing to change")
    pos[no]["status"] = b["status"]; setkv("pos", pos); audit(u["name"], "po_status", "", f"PO {no} set to {b['status']}"); return {"ok": True}

@app.get("/api/s/grn")
def s_grn(request: Request):
    staff(request)
    return [dict(po=po, item=k, **r) for po, items in kv("grn").items() for k, r in items.items()]

@app.post("/api/s/grn")
async def s_grn_add(request: Request):
    u = staff(request); b = await request.json(); pos, grn, po = kv("pos"), kv("grn"), str(b.get("po", ""))
    item = (b.get("item") or "").strip().lower()
    if po not in pos or item not in [l["item"].lower() for l in pos[po]["lines"]]: bad("That item is not on that PO")
    try: rec, rej = float(b.get("received")), float(b.get("rejected") or 0)
    except (TypeError, ValueError): bad("Quantities must be numbers")
    if rec < 0 or rej < 0 or rej > rec: bad("Check the received and rejected quantities")
    grn.setdefault(po, {})[item] = dict(received=int(rec) if rec == int(rec) else rec, rejected=int(rej) if rej == int(rej) else rej, delivered=isodate(b.get("delivered")))
    setkv("grn", grn); audit(u["name"], "grn_recorded", "", f"PO {po}: {item} received {rec:g}, rejected {rej:g}"); return {"ok": True}

def page(name, role=None):
    def h(request: Request):
        u = request.session.get("user")
        if role and (not u or u["role"] not in ((role,) if isinstance(role, str) else role)): return FileResponse(BASE / "static/login.html")
        return FileResponse(BASE / f"static/{name}.html")
    return h
app.get("/")(page("login")); app.get("/vendor")(page("vendor", "vendor")); app.get("/reviewer")(page("reviewer", "reviewer")); app.get("/office")(page("office", ("reviewer", "admin")))
@app.get("/healthz")
def healthz(): return {"ok": True}

init()