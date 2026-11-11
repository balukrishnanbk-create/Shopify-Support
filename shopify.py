"""Shopify Admin GraphQL client: staged image upload + productSet (create-or-update by handle)."""
import mimetypes, os, time
from pathlib import Path

import requests

_tok = {"value": None, "exp": 0}


def _shop():
    s = os.getenv("SHOPIFY_SHOP", "").strip().replace("https://", "").rstrip("/")
    if not s:
        raise RuntimeError("SHOPIFY_SHOP is not set (e.g. koshaa.myshopify.com)")
    return s


def _token():
    t = os.getenv("SHOPIFY_ACCESS_TOKEN")
    if t:
        return t
    cid, sec = os.getenv("SHOPIFY_CLIENT_ID"), os.getenv("SHOPIFY_CLIENT_SECRET")
    if not (cid and sec):
        raise RuntimeError("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")
    if _tok["value"] and time.time() < _tok["exp"] - 60:
        return _tok["value"]
    r = requests.post(f"https://{_shop()}/admin/oauth/access_token",
                      json={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Shopify token request failed {r.status_code}: {r.text[:200]}")
    j = r.json()
    _tok.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 86000)))
    return _tok["value"]


def gql(query, variables=None):
    ver = os.getenv("SHOPIFY_API_VERSION", "2025-10")
    for i in range(4):
        r = requests.post(f"https://{_shop()}/admin/api/{ver}/graphql.json",
                          headers={"X-Shopify-Access-Token": _token()},
                          json={"query": query, "variables": variables or {}}, timeout=90)
        if r.status_code == 429 and i < 3:
            time.sleep(2 * (i + 1)); continue
        if not r.ok:
            raise RuntimeError(f"Shopify HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        if j.get("errors"):
            throttled = any("THROTTLED" in str(e) for e in j["errors"])
            if throttled and i < 3:
                time.sleep(2 * (i + 1)); continue
            raise RuntimeError(f"Shopify: {j['errors']}")
        return j["data"]


def check_connection():
    d = gql("{ shop { name myshopifyDomain } }")
    return d["shop"]


def first_location_id():
    try:
        d = gql("{ locations(first:1){ nodes{ id } } }")
        return d["locations"]["nodes"][0]["id"]
    except Exception:
        return None  # missing read_locations scope -> skip stock levels


def upload_image(path):
    """Staged upload -> returns resourceUrl usable as originalSource."""
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = path.read_bytes()
    q = """mutation($input:[StagedUploadInput!]!){ stagedUploadsCreate(input:$input){
      stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ field message } } }"""
    d = gql(q, {"input": [{"resource": "IMAGE", "filename": path.name, "mimeType": mime,
                           "httpMethod": "POST", "fileSize": str(len(data))}]})["stagedUploadsCreate"]
    if d["userErrors"]:
        raise RuntimeError(f"stagedUploadsCreate: {d['userErrors']}")
    t = d["stagedTargets"][0]
    r = requests.post(t["url"], data={x["name"]: x["value"] for x in t["parameters"]},
                      files={"file": (path.name, data, mime)}, timeout=180)
    if r.status_code >= 300:
        raise RuntimeError(f"Image upload failed {r.status_code}: {r.text[:200]}")
    return t["resourceUrl"]


def find_category_id(path):
    """Resolve a 'A > B > C' category path to Shopify's taxonomy id (None if not found)."""
    if not path:
        return None
    leaf = path.split(">")[-1].strip()
    try:
        d = gql("query($s:String!){ taxonomy{ categories(first:10, search:$s){ nodes{ id fullName } } } }", {"s": leaf})
        nodes = d["taxonomy"]["categories"]["nodes"]
        want = path.replace(" ", "").lower()
        for n in nodes:
            if n["fullName"].replace(" ", "").lower() == want:
                return n["id"]
        return nodes[0]["id"] if nodes else None
    except Exception:
        return None


PRODUCT_SET = """mutation($input:ProductSetInput!, $identifier:ProductSetIdentifiers){
  productSet(synchronous:true, input:$input, identifier:$identifier){
    product{ id handle status }
    userErrors{ field message code } } }"""


def push_product(p, publish_status="ACTIVE", log=print):
    """Create or update (matched by handle) one product with variants, price, images and SEO."""
    log("uploading images")
    files = []
    for img in p["images"]:
        if not img.get("include", True):
            continue
        url = upload_image(img["path"])
        files.append({"originalSource": url, "contentType": "IMAGE", "alt": p["title"]})

    loc = first_location_id() if p.get("stock") not in (None, "") else None
    variants = []
    for size in p["sizes"]:
        v = {
            "optionValues": [{"optionName": "Size", "name": size}, {"optionName": "Color", "name": p["color_label"]}],
            "price": str(p["price"]),
            "inventoryPolicy": "DENY",
            "inventoryItem": {"tracked": True, "requiresShipping": True,
                              "measurement": {"weight": {"value": float(p.get("weight_grams") or 300), "unit": "GRAMS"}}},
        }
        if p.get("compare_at"):
            v["compareAtPrice"] = str(p["compare_at"])
        if p.get("sku_prefix"):
            v["sku"] = f"{p['sku_prefix']}-{size}"
        if loc:
            v["inventoryQuantities"] = [{"locationId": loc, "name": "available", "quantity": int(p["stock"])}]
        variants.append(v)

    inp = {
        "title": p["title"], "handle": p["handle"], "descriptionHtml": p["description_html"],
        "vendor": os.getenv("STORE_VENDOR", "KOSHAA"), "productType": p["type"],
        "tags": [t.strip() for t in p["tags"].split(",") if t.strip()],
        "status": publish_status,
        "seo": {"title": p["seo_title"], "description": p["seo_description"]},
        "productOptions": [
            {"name": "Size", "position": 1, "values": [{"name": s} for s in p["sizes"]]},
            {"name": "Color", "position": 2, "values": [{"name": p["color_label"]}]},
        ],
        "variants": variants,
    }
    cat = find_category_id(p.get("category"))
    if cat:
        inp["category"] = cat
    if files:
        inp["files"] = files
    log("saving product")
    d = gql(PRODUCT_SET, {"input": inp, "identifier": {"handle": p["handle"]}})["productSet"]
    if d["userErrors"]:
        raise RuntimeError("; ".join(f"{e['field']}: {e['message']}" for e in d["userErrors"]))
    prod = d["product"]
    gid = prod["id"].split("/")[-1]
    return {"id": prod["id"], "admin_url": f"https://{_shop()}/admin/products/{gid}"}
