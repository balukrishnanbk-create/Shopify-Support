"""Shopify product uploader: Excel + photos -> AI copy + AI model photos -> review -> push to Shopify."""
import csv, io, json, os, re, threading, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

import ai, shopify

load_dotenv()
ROOT = Path(__file__).parent
DATA, UPLOADS, GEN = ROOT / "data", ROOT / "data/uploads", ROOT / "data/generated"
for d in (UPLOADS, GEN):
    d.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / "state.json"
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2)
app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

PRODUCTS = {}  # id -> product dict
if STATE_FILE.exists():
    PRODUCTS = {p["id"]: p for p in json.loads(STATE_FILE.read_text())}
    for p in PRODUCTS.values():  # a restart interrupts running jobs
        if p["status"] in ("generating", "pushing"):
            p["status"] = "new"


def save():
    with LOCK:
        STATE_FILE.write_text(json.dumps(list(PRODUCTS.values()), indent=1))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def rel(path):
    return str(Path(path).relative_to(DATA))


# ---------- sheet parsing ----------
ALIASES = {
    "name": ["name", "product name", "title", "product"],
    "color": ["color", "colour"],
    "type": ["type", "product type", "category"],
    "gender": ["gender", "target gender"],
    "sizes": ["sizes", "size", "available sizes"],
    "price": ["price", "selling price", "variant price", "mrp"],
    "compare_at": ["compare at price", "compare at", "original price"],
    "stock": ["stock", "qty", "quantity", "inventory"],
    "photo": ["photo", "photos", "image", "images", "photo filename"],
    "notes": ["notes", "fabric", "details"],
    "sku_prefix": ["sku", "sku prefix", "code"],
}


def read_sheet(file):
    name = file.filename.lower()
    df = pd.read_csv(file, dtype=str) if name.endswith(".csv") else pd.read_excel(file, dtype=str)
    df = df.dropna(how="all")
    cols = {str(c).strip().lower(): c for c in df.columns}
    out = []
    for _, row in df.iterrows():
        rec = {}
        for key, names in ALIASES.items():
            rec[key] = ""
            for n in names:
                if n in cols and pd.notna(row[cols[n]]):
                    rec[key] = str(row[cols[n]]).strip()
                    break
        out.append(rec)
    return out


def photo_match(rec, photos):
    """photos: {lowercase filename: Path}. Match by the 'Photo' column (comma separated for front/back)."""
    found = []
    for f in re.split(r"[,;|]", rec["photo"]):
        f = f.strip().lower()
        if f in photos:
            found.append(photos[f])
    return found


def new_product(rec, photo_paths):
    sizes = [x.strip().upper() for x in re.split(r"[,/;|]", rec["sizes"]) if x.strip()]
    d = {
        "id": uuid.uuid4().hex[:8], "name": rec["name"], "handle": "", "title": rec["name"],
        "color": rec["color"], "color_label": rec["color"].title(),
        "type": rec["type"], "gender": rec["gender"].lower(), "category": "", "weight_grams": 0,
        "sizes": sizes or ["Free Size"], "price": rec["price"], "compare_at": rec["compare_at"], "stock": rec["stock"],
        "notes": rec["notes"], "sku_prefix": rec["sku_prefix"],
        "source_photos": [str(p) for p in photo_paths],
        "images": [{"path": str(p), "kind": "original", "include": True} for p in photo_paths],
        "description_html": "", "tags": "", "seo_title": "", "seo_description": "",
        "status": "new", "error": "", "admin_url": "", "progress": "",
    }
    d["locked"] = [k for k in ("title", "type", "color", "gender") if d[k]]  # cells the owner filled stay as typed
    return d


def unique_handle(p):
    base = slug(p["title"]) or f"product-{p['id']}"
    taken = {x["handle"] for x in PRODUCTS.values() if x["id"] != p["id"] and x["handle"]}
    h, n = base, 2
    while h in taken:
        h, n = f"{base}-{n}", n + 1
    return h


# ---------- background jobs ----------
def set_state(p, status, progress="", error=""):
    p["status"], p["progress"], p["error"] = status, progress, error
    save()


def job_generate(pid, n_images, do_text=True, do_images=True):
    p = PRODUCTS[pid]
    try:
        photos = p["source_photos"]
        if not photos:
            raise RuntimeError("No product photo matched this row")
        set_state(p, "generating", "reading your photo")
        if do_text:
            fill = ai.analyze_product(p, photos)
            for k, v in fill.items():
                if k not in p.get("locked", []):
                    p[k] = v
            p["color_label"] = p["color_label"] or p["color"].title()
            if not p.get("handle") or "title" not in p.get("locked", []):
                p["handle"] = unique_handle(p)
        if do_images:
            p["images"] = [i for i in p["images"] if i["kind"] == "original"]
            gen = []
            for k in range(n_images):
                set_state(p, "generating", f"model photo {k + 1}/{n_images}")
                out = GEN / pid / f"{p['handle'] or pid}-model-{k + 1}.png"
                ai.generate_model_image(p, photos, k, out)
                gen.append({"path": str(out), "kind": "model", "include": True})
            p["images"] = gen + p["images"]
        set_state(p, "ready")
        return True
    except Exception as e:
        set_state(p, "error", error=str(e))
        return False


def job_push(pid, status):
    p = PRODUCTS[pid]
    try:
        set_state(p, "pushing", "sending to Shopify")
        res = shopify.push_product(p, status, log=lambda m: set_state(p, "pushing", m))
        p["admin_url"] = res["admin_url"]
        set_state(p, "pushed")
    except Exception as e:
        set_state(p, "error", error=str(e))


def job_auto(pid, n_images, status):
    """Photo + size + price in -> AI fills everything -> product created/updated in Shopify."""
    if job_generate(pid, n_images, True, n_images > 0):
        job_push(pid, status)


# ---------- routes ----------
@app.get("/")
def index():
    return send_file(ROOT / "templates/index.html")


@app.get("/files/<path:f>")
def files(f):
    return send_from_directory(DATA, f)


@app.get("/api/config")
def config():
    return jsonify(gemini=bool(os.getenv("GEMINI_API_KEY")), shop=os.getenv("SHOPIFY_SHOP", ""),
                   shopify=bool(os.getenv("SHOPIFY_SHOP") and (os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_CLIENT_ID"))))


@app.get("/api/shopify/test")
def test_shop():
    try:
        return jsonify(ok=True, **shopify.check_connection())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.get("/api/template")
def template():
    return send_file(ROOT / "product_input_template.xlsx", as_attachment=True)


@app.post("/api/upload")
def upload():
    f = request.form
    photos = {}
    for ph in request.files.getlist("photos"):
        if ph.filename:
            path = UPLOADS / f"{uuid.uuid4().hex[:6]}_{secure_filename(ph.filename)}"
            ph.save(path)
            photos[ph.filename.lower()] = path
    blank = {k: "" for k in ALIASES}
    recs, skipped = [], []
    sheet = request.files.get("sheet")
    if sheet and sheet.filename:
        try:
            for rec in read_sheet(sheet):
                rec["sizes"] = rec["sizes"] or f.get("default_sizes", "")
                rec["price"] = rec["price"] or f.get("default_price", "")
                rec["stock"] = rec["stock"] or f.get("default_stock", "")
                matched = photo_match(rec, photos)
                if not (rec["price"] and rec["sizes"]):
                    skipped.append(f"{rec['photo'] or rec['name'] or 'row'}: needs Sizes and Price")
                elif not matched:
                    skipped.append(f"{rec['photo'] or rec['name'] or 'row'}: photo not uploaded")
                else:
                    recs.append((rec, matched))
        except Exception as e:
            return jsonify(error=f"Could not read sheet: {e}"), 400
    else:  # no sheet: every photo is one product, with the sizes/price typed in the form
        if not (f.get("default_price") and f.get("default_sizes")):
            return jsonify(error="Without a sheet, enter Sizes and Price in the form"), 400
        for name, path in photos.items():
            recs.append(({**blank, "sizes": f["default_sizes"], "price": f["default_price"],
                          "compare_at": f.get("default_compare_at", ""), "stock": f.get("default_stock", "")}, [path]))
    if not recs:
        return jsonify(error="Nothing to add. " + "; ".join(skipped)), 400
    n_images = int(f.get("images", 2))
    auto = f.get("auto") == "true"
    status = "ACTIVE" if f.get("publish") == "true" else "DRAFT"
    for rec, matched in recs:
        p = new_product(rec, matched)
        PRODUCTS[p["id"]] = p
        if auto:
            set_state(p, "generating", "queued")
            POOL.submit(job_auto, p["id"], n_images, status)
        else:
            set_state(p, "generating", "queued")
            POOL.submit(job_generate, p["id"], n_images, True, n_images > 0)
    save()
    return jsonify(added=len(recs), skipped=skipped, auto=auto)


@app.get("/api/products")
def products():
    return jsonify(list(PRODUCTS.values()))


@app.post("/api/generate")
def generate():
    b = request.json or {}
    n = int(b.get("images", 2))
    for pid in b.get("ids") or list(PRODUCTS):
        p = PRODUCTS.get(pid)
        if p and p["status"] not in ("generating", "pushing"):
            set_state(p, "generating", "queued")
            POOL.submit(job_generate, pid, n, b.get("text", True), b.get("photos", True))
    return jsonify(ok=True)


@app.post("/api/push")
def push():
    b = request.json or {}
    status = "ACTIVE" if b.get("publish") else "DRAFT"
    for pid in b.get("ids") or []:
        p = PRODUCTS.get(pid)
        if p and p["status"] in ("ready", "error", "pushed"):
            set_state(p, "pushing", "queued")
            POOL.submit(job_push, pid, status)
    return jsonify(ok=True)


@app.post("/api/product/<pid>")
def update(pid):
    p = PRODUCTS[pid]
    for k, v in (request.json or {}).items():
        if k in ("title", "description_html", "tags", "seo_title", "seo_description", "price", "compare_at",
                 "stock", "color", "color_label", "type", "sizes", "images", "handle"):
            p[k] = v
    save()
    return jsonify(ok=True)


@app.delete("/api/product/<pid>")
def delete(pid):
    PRODUCTS.pop(pid, None)
    save()
    return jsonify(ok=True)


CSV_COLS = ["Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags", "Published", "Option1 Name",
            "Option1 Value", "Option1 Linked To", "Option2 Name", "Option2 Value", "Option2 Linked To", "Option3 Name",
            "Option3 Value", "Option3 Linked To", "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
            "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service", "Variant Price",
            "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable", "Unit Price Total Measure",
            "Unit Price Total Measure Unit", "Unit Price Base Measure", "Unit Price Base Measure Unit",
            "Variant Barcodes", "Image Src", "Image Position", "Image Alt Text", "Gift Card", "SEO Title",
            "SEO Description", "Color (product.metafields.shopify.color-pattern)",
            "Target gender (product.metafields.shopify.target-gender)", "Variant Image", "Variant Weight Unit",
            "Variant Tax Code", "Cost per item", "Status", "Variant Packed Length", "Variant Packed Width",
            "Variant Packed Height", "Variant Packed Dimension Unit"]


@app.get("/api/export.csv")
def export_csv():
    """Same column layout as your products_only_two.csv, one row per size variant."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS)
    w.writeheader()
    for p in PRODUCTS.values():
        for i, size in enumerate(p["sizes"]):
            row = {"Handle": p["handle"], "Option1 Name": "Size", "Option1 Value": size, "Option2 Name": "Color",
                   "Option2 Value": p["color_label"], "Variant Inventory Qty": p["stock"] or 0,
                   "Variant Grams": p.get("weight_grams") or "", "Variant Inventory Tracker": "shopify",
                   "Variant Inventory Policy": "deny", "Variant Fulfillment Service": "manual",
                   "Variant Price": p["price"], "Variant Compare At Price": p["compare_at"],
                   "Variant Requires Shipping": True, "Variant Taxable": True, "Variant Weight Unit": "kg"}
            if p.get("sku_prefix"):
                row["Variant SKU"] = f"{p['sku_prefix']}-{size}"
            if i == 0:
                row.update({"Title": p["title"], "Body (HTML)": p["description_html"],
                            "Vendor": os.getenv("STORE_VENDOR", "KOSHAA"),
                            "Product Category": p.get("category") or "",
                            "Type": p["type"], "Tags": p["tags"], "Published": True, "Gift Card": False,
                            "SEO Title": p["seo_title"], "SEO Description": p["seo_description"],
                            "Color (product.metafields.shopify.color-pattern)": p["color"].lower(),
                            "Target gender (product.metafields.shopify.target-gender)": p["gender"],
                            "Status": "active"})
            w.writerow(row)
    return app.response_class(buf.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": "attachment; filename=shopify_products.csv"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5050)), debug=False, threaded=True)
