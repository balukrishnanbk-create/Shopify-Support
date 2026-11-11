"""AI helpers: product copy + model photos, via the Gemini REST API (no SDK needed)."""
import base64, json, mimetypes, os, time
from pathlib import Path

import requests

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
STYLE = json.loads((Path(__file__).parent / "style_examples.json").read_text())

POSES = [
    "Full-body front view, standing naturally, facing the camera, plain light-grey studio background, soft even lighting.",
    "Three-quarter angle, relaxed natural pose, bright modern bedroom interior, soft window light, lifestyle catalogue look.",
    "Back or side view showing how the garment falls, plain light-grey studio background, soft even lighting.",
]


def _key():
    k = os.getenv("GEMINI_API_KEY")
    if not k:
        raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
    return k


def _img_part(path):
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(Path(path).read_bytes()).decode()}}


def _call(model, parts, gen_cfg, tries=4):
    body = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}
    for i in range(tries):
        r = requests.post(API.format(model=model), headers={"x-goog-api-key": _key()}, json=body, timeout=180)
        if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
            time.sleep(2 ** (i + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
        return r.json()


CATEGORY_HINTS = [
    "Apparel & Accessories > Clothing > Sleepwear & Loungewear",
    "Apparel & Accessories > Clothing > Dresses",
    "Apparel & Accessories > Clothing > Clothing Tops",
    "Apparel & Accessories > Clothing > Pants",
    "Apparel & Accessories > Clothing > Skirts",
    "Apparel & Accessories > Clothing > Traditional & Ceremonial Clothing > Saris & Lehengas",
    "Apparel & Accessories > Clothing > Traditional & Ceremonial Clothing > Kurtas",
    "Home & Garden > Linens & Bedding > Bedding > Bed Sheets",
    "Home & Garden > Linens & Bedding > Bedding > Blankets",
    "Home & Garden > Linens & Bedding > Bedding > Bed Pillows",
]


def analyze_product(p, photos):
    """Look at the photo(s) and return EVERY listing field. Owner-supplied facts (price, sizes, any filled cells) win."""
    vendor = os.getenv("STORE_VENDOR", "KOSHAA")
    examples = json.dumps(STYLE, indent=2)
    given = {k: p.get(k) for k in ("name", "type", "color", "gender", "notes") if p.get(k)}
    prompt = f"""You fill in a Shopify product listing for {vendor}, an Indian textile / apparel store, from the product photo(s).
The owner only supplied the price and sizes, so YOU must work out everything else from the photo.

Existing listings from this store - copy their style, length and tone exactly:
{examples}

Sizes on sale: {', '.join(p['sizes'])}
Price: Rs {p['price']}
Facts the owner typed (trust these over the photo): {json.dumps(given) if given else 'none'}

Rules:
- Describe only what is visible. Never invent fabric, GSM, care instructions, offers or claims.
- title: short and specific, like "Yellow Ikat Printed Nighty" (colour + print + garment). No brand name, no size.
- type: the store's product-type label (e.g. "Printed Nighty", "Cotton Kurti", "Bedsheet").
- color: lowercase colour/print name (like "yellow ikat"). color_label: same in Title Case.
- gender: one of female, male, unisex, kids.
- category: the best Shopify product category path, preferably one of: {json.dumps(CATEGORY_HINTS)}
- description_html: one short <p> paragraph, like the examples.
- tags: lowercase list (garment kind, audience, colour/print, one or two style words).
- seo_title: "<title> | {vendor}". seo_description: "Shop <title> at {vendor}. Check product details and delivery information."
- weight_grams: realistic packed weight in grams for one piece of this garment (integer).
Return JSON with keys: title, type, color, color_label, gender, category, description_html, tags (array), seo_title, seo_description, weight_grams."""
    parts = [{"text": prompt}] + [_img_part(x) for x in photos[:3]]
    data = _call(os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash"), parts, {"responseMimeType": "application/json"})
    out = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
    if isinstance(out.get("tags"), list):
        out["tags"] = ", ".join(out["tags"])
    try:
        out["weight_grams"] = int(float(out.get("weight_grams") or 0)) or int(os.getenv("DEFAULT_WEIGHT_GRAMS", 300))
    except Exception:
        out["weight_grams"] = int(os.getenv("DEFAULT_WEIGHT_GRAMS", 300))
    return out


def generate_model_image(p, photos, pose_idx, out_path):
    """Create one photo of a model wearing the garment from the reference photo(s)."""
    model_desc = os.getenv("MODEL_DESCRIPTION", "an Indian woman in her late 20s")
    pose = POSES[pose_idx % len(POSES)]
    prompt = (
        f"Create a photorealistic e-commerce fashion photograph of {model_desc} wearing the exact garment shown in the reference photo(s). "
        "Reproduce the garment faithfully: same colour, same print/pattern, same neckline, sleeves, length and trims. "
        "Do not add logos, text or watermarks. Natural skin, natural proportions, correct hands. "
        f"{pose} Portrait orientation 3:4."
    )
    parts = [{"text": prompt}] + [_img_part(x) for x in photos[:3]]
    data = _call(os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"), parts,
                 {"responseModalities": ["TEXT", "IMAGE"]})
    for part in data["candidates"][0]["content"]["parts"]:
        blob = part.get("inlineData") or part.get("inline_data")
        if blob:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(base64.b64decode(blob["data"]))
            return out_path
    raise RuntimeError("Gemini returned no image (possibly blocked by safety filters) – try again")
