# Shopify Product Studio

Excel + product photos -> AI product info + AI model photos -> review -> create/update products in Shopify.

## Run
```
pip install -r requirements.txt
copy .env.example .env      (Windows)   |   cp .env.example .env   (Mac/Linux)
# fill in .env, then:
python app.py
```
Open http://127.0.0.1:5050

## Daily use (automatic)
1. Pick your product photos (one product per photo), type Sizes and Price, press Start.
   - Different prices/sizes per product? Upload `product_input_template.xlsx` (only Photo, Sizes, Price needed).
2. AI reads each photo and fills: title, handle, description, type, colour, gender, tags, Shopify category, weight, SEO title/description.
   It also makes the AI model photos.
3. With "Send to Shopify automatically" ticked, each product is created in Shopify as soon as it is ready
   (Draft by default; tick "Publish live" to make it live).
Anything you type in the optional sheet columns (Name, Color, Type, Gender...) is kept; AI only fills blanks.

## Keys
- Gemini: https://aistudio.google.com/apikey
- Shopify: Settings > Apps and sales channels > Develop apps (or Dev Dashboard) > create app with Admin API scopes
  `write_products`, `read_products`, `write_files`, `read_files`, `read_locations`, `write_inventory`, `read_inventory`.
  Put the Admin API access token (shpat_...) in SHOPIFY_ACCESS_TOKEN, then click "Test store connection".

## Notes
- AI model photos can drift from the real garment (print, length). Always compare with the original before publishing.
- "Download Shopify CSV" gives your existing 49-column format; images are not included there (they need public URLs), so use the Push button for photos.
- Category is looked up in Shopify's taxonomy from the AI's suggestion; if no match is found the product is created without one.
- The Colour and Target-gender metafields are filled in the CSV export, not in the API push (Shopify needs taxonomy IDs for them).
