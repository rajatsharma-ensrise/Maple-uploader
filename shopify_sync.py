import requests
import pandas as pd
import time
import re
from datetime import datetime

import config

MODE = "IMPORT"

INPUT_EXCEL = r"C:\Users\LENOVO\Downloads\Maple Inventory 8 1.0 June 2026.xlsx"

SHOP = config.SHOPIFY_SHOP
API_VERSION = "2024-01"
ACCESS_TOKEN = config.SHOPIFY_ACCESS_TOKEN

CSV_FILE = r"C:\Users\LENOVO\Downloads\shopifymapple-new-formet_newwready.csv"

GRAPHQL_URL = f"https://{SHOP}.myshopify.com/admin/api/{API_VERSION}/graphql.json"
REST_URL = f"https://{SHOP}.myshopify.com/admin/api/{API_VERSION}"

HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json"
}

def convert_excel_to_csv(input_file, output_file):
    """
    Client ka Excel (Maple Inventory format) ko
    hamare Shopify import CSV format mein convert karo.
    """
    print("\n===== STEP 1: EXCEL → CSV CONVERT =====")

    raw = pd.read_excel(input_file, dtype=str)
    raw.columns = raw.columns.str.strip()

    def get_col(col_name):
        return raw[col_name] if col_name in raw.columns else pd.Series([""] * len(raw))

    def generate_handle(title, part_number):
        title       = "" if pd.isna(title)       else str(title)
        part_number = "" if pd.isna(part_number) else str(part_number)
        text = title.lower().strip()
        text = text.replace(" ", "-")
        text = re.sub(r"[^a-z0-9\-]", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        final = f"{text}-{part_number.strip()}"
        final = re.sub(r"-+", "-", final).strip("-")
        return final

    def extract_model(title):
        if pd.isna(title):
            return ""
        return str(title).split("-")[0].strip()

    def clean_and_format_battery(val):
        val_str = str(val).strip()
        if pd.isna(val) or val_str.lower() in ["nan", "none", "<na>", ""]:
            return ""
        match = re.search(r"([0-9\.]+)", val_str)
        if match:
            num_str = match.group(1)
            try:
                num_val = float(num_str)
                if 0.0 < num_val <= 1.0:
                    num_val = num_val * 100
                clean_num = str(int(round(num_val)))
                val_str = val_str.replace(num_str, clean_num)
                if "%" not in val_str:
                    val_str = val_str.replace(clean_num, f"{clean_num}%")
            except ValueError:
                pass
        val_str = re.sub(r"\.0(?=\D|$)", "", val_str)
        return val_str

    def smart_set(col, value):
        if col not in raw.columns:
            raw[col] = value
        else:
            raw[col] = raw[col].fillna(value)
            raw.loc[raw[col] == "", col] = value

    def smart_set_series(col, series):
        if col not in raw.columns:
            raw[col] = series
        else:
            raw[col] = raw[col].fillna(series)
            raw.loc[raw[col] == "", col] = series

    title_col   = get_col("Title")
    sku_col     = get_col("Product Part Number (product.metafields.custom.product_part_name)")
    imei_col    = get_col("Product Part  IMEI (product.metafields.custom.product_part_name)")
    battery_col = get_col("Battery")

    imei_clean   = imei_col.astype(str).str.replace(r"\.0$", "", regex=True)
    battery_clean = battery_col.apply(clean_and_format_battery)
    today_tag    = "custom-" + datetime.now().strftime("%d-%m-%y")

    raw["Handle"] = [generate_handle(t, s) for t, s in zip(title_col, sku_col)]

    model_col      = title_col.apply(extract_model)
    model_col_name = "Product model name (product.metafields.custom.product_model_name)"
    primary_col_name = "Primary Product name (product.metafields.custom.primary_product_name)"

    if model_col_name not in raw.columns:
        if primary_col_name in raw.columns:
            idx = raw.columns.get_loc(primary_col_name)
            raw.insert(idx + 1, model_col_name, model_col)
        else:
            raw[model_col_name] = model_col

    smart_set("Vendor", "Apple")
    smart_set("Tags", today_tag)
    smart_set("Published", "FALSE")
    smart_set("Option1 Name", "Title")
    smart_set("Option1 Value", "Default Title")
    smart_set_series("Variant SKU", sku_col)
    smart_set_series("Variant Barcode", imei_clean)
    smart_set("Variant Grams", 0)
    smart_set("Variant Inventory Tracker", "shopify")
    smart_set("Variant Inventory Qty", 0)
    smart_set("Variant Inventory Policy", "deny")
    smart_set("Variant Fulfillment Service", "manual")
    smart_set("Variant Requires Shipping", "TRUE")
    smart_set("Variant Taxable", "TRUE")
    smart_set("Gift Card", "FALSE")
    smart_set("Variant Weight Unit", "kg")
    smart_set("Status", "draft")
    smart_set_series(
        "Actual Bettery Percentage (product.metafields.custom.actual_bettery_percentage)",
        battery_clean
    )

    raw = raw[raw["Title"].notna()]
    raw = raw[raw["Title"] != ""]

    raw = raw.iloc[1:]

    unnamed_cols = [c for c in raw.columns if str(c).startswith("Unnamed:")]
    raw = raw.drop(columns=unnamed_cols, errors="ignore")
    raw = raw.drop(columns=["Battery", "warranty"], errors="ignore")

    def gc(col): return raw[col] if col in raw.columns else pd.Series([""] * len(raw))

    final = pd.DataFrame()
    final["Product ID"]                   = ""
    final["Handle"]                       = gc("Handle")
    final["Title"]                        = gc("Title")
    final["Vendor"]                       = gc("Vendor")
    final["Type"]                         = gc("Type")
    final["Status"]                       = gc("Status")
    final["Tags"]                         = gc("Tags")
    final["Product Command"]              = "create"
    final["Tag Command"]                  = "replace"
    final["Variant ID"]                   = ""
    final["Variant SKU"]                  = gc("Variant SKU")
    final["Barcode"]                      = gc("Variant Barcode")
    final["Variant Price"]                = gc("Variant Price")
    final["Variant Compare At Price"]     = gc("Variant Compare At Price")
    final["Option1 Name"]                 = gc("Option1 Name")
    final["Option1 Value"]                = gc("Option1 Value")
    final["Option2 Name"]                 = gc("Option2 Name")
    final["Option2 Value"]                = gc("Option2 Value")
    final["Option3 Name"]                 = gc("Option3 Name")
    final["Option3 Value"]                = gc("Option3 Value")
    final["Variant Command"]              = "create"

    final["Metafield:custom.product_option_1_value[single_line_text_field]"] = gc(
        "Product option 1 value (product.metafields.custom.product_option_1_value)"
    )
    final["Metafield:custom.product_option_2_value[single_line_text_field]"] = gc(
        "Product option 2 value (product.metafields.custom.product_option_2_value)"
    )
    final["Metafield:custom.product_option_3_value[single_line_text_field]"] = gc(
        "Product option 3 value (product.metafields.custom.product_option_3_value)"
    )
    final["Metafield:custom.actual_bettery_percentage[single_line_text_field]"] = gc(
        "Actual Bettery Percentage (product.metafields.custom.actual_bettery_percentage)"
    )
    final["Metafield:custom.product_part_name[single_line_text_field]"] = gc(
        "Product Part Number (product.metafields.custom.product_part_name)"
    )
    final["Metafield:custom.product_model_name[single_line_text_field]"] = gc(
        "Product model name (product.metafields.custom.product_model_name)"
    )

    final.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"  Done CSV ready: {output_file} ({len(final)} rows)")
    print("===== STEP 1 DONE =====\n")


def read_csv_safe(path):
    try:
        return pd.read_csv(path, dtype=str, encoding='utf-8-sig').fillna("")
    except UnicodeDecodeError:
        try:
            return pd.read_csv(path, dtype=str, encoding='utf-8').fillna("")
        except UnicodeDecodeError:
            return pd.read_csv(path, dtype=str, encoding='latin-1').fillna("")


def clean_id(gid):
    return gid.split("/")[-1] if gid else ""


def graphql(query, variables=None):
    r = requests.post(GRAPHQL_URL, headers=HEADERS, json={
        "query": query,
        "variables": variables or {}
    })
    j = r.json()
    if "errors" in j:
        print("GraphQL ERROR:", j["errors"])
    return j.get("data")


def apply_tag_command(pid, tags_csv, cmd):
    if not pid or not cmd:
        return
    try:
        r = requests.get(f"{REST_URL}/products/{pid}.json", headers=HEADERS)
        if r.status_code != 200:
            return
        current = set(t.strip() for t in r.json()["product"]["tags"].split(",") if t.strip())
        new = set(t.strip() for t in tags_csv.split(",") if t.strip())

        if cmd == "replace":
            final = new
        elif cmd == "add":
            final = current | new
        elif cmd == "remove":
            final = current - new
        else:
            return

        requests.put(
            f"{REST_URL}/products/{pid}.json",
            headers=HEADERS,
            json={"product": {"id": pid, "tags": ",".join(sorted(final))}}
        )
    except Exception as e:
        print(f"Tag update failed for product {pid}: {e}")


def get_product_metafield_defs():
    q = """
    {
      metafieldDefinitions(first: 100, ownerType: PRODUCT) {
        edges {
          node {
            namespace
            key
            type { name }
          }
        }
      }
    }
    """
    data = graphql(q)
    if not data:
        return []
    return [
        f"Metafield:{n['node']['namespace']}.{n['node']['key']}[{n['node']['type']['name']}]"
        for n in data["metafieldDefinitions"]["edges"]
    ]


def get_all_metaobjects():
    q = """
    {
      metaobjectDefinitions(first: 50) {
        edges {
          node {
            type
            fieldDefinitions { key }
          }
        }
      }
    }
    """
    data = graphql(q)
    if not data:
        return {}, []

    meta_map = {}
    cols = []

    for d in data["metaobjectDefinitions"]["edges"]:
        t = d["node"]["type"]
        for f in d["node"]["fieldDefinitions"]:
            cols.append(f"Metaobject:{t}.{f['key']}")

        q2 = f"""
        {{
          metaobjects(type: "{t}", first: 100) {{
            edges {{
              node {{
                id
                fields {{ key value }}
              }}
            }}
          }}
        }}
        """
        mo = graphql(q2)
        if not mo:
            continue

        for m in mo["metaobjects"]["edges"]:
            meta_map[clean_id(m["node"]["id"])] = {
                f"Metaobject:{t}.{f['key']}": f["value"]
                for f in m["node"]["fields"]
            }

    return meta_map, sorted(set(cols))


def export_all():
    print("EXPORT MODE")

    metafield_cols = get_product_metafield_defs()
    metaobject_map, metaobject_cols = get_all_metaobjects()

    rows = []
    cursor = None

    while True:
        q = """
        query ($cursor: String) {
          products(first: 50, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                id
                handle
                title
                vendor
                productType
                status
                tags
                variants(first: 50) {
                  edges {
                    node {
                      id
                      sku
                      barcode
                      price
                      compareAtPrice
                      selectedOptions { name value }
                    }
                  }
                }
                metafields(first: 100) {
                  edges {
                    node {
                      namespace
                      key
                      type
                      value
                      reference { ... on Metaobject { id } }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = graphql(q, {"cursor": cursor})
        if not data:
            break

        for p in data["products"]["edges"]:
            product = p["node"]

            base = {
                "Product ID": clean_id(product["id"]),
                "Handle": product["handle"],
                "Title": product["title"],
                "Vendor": product["vendor"],
                "Type": product["productType"],
                "Status": product["status"],
                "Tags": ",".join(product["tags"]),
                "Product Command": "",
                "Tag Command": "",
            }

            mf_vals = {}
            mf_refs = []

            for mf in product["metafields"]["edges"]:
                m = mf["node"]
                col = f"Metafield:{m['namespace']}.{m['key']}[{m['type']}]"
                mf_vals[col] = m["value"]
                if m.get("reference"):
                    mf_refs.append(clean_id(m["reference"]["id"]))

            for c in metafield_cols:
                base[c] = mf_vals.get(c, "")

            for c in metaobject_cols:
                base[c] = ""

            for r in mf_refs:
                if r in metaobject_map:
                    base.update(metaobject_map[r])

            for v in product["variants"]["edges"]:
                opts = v["node"]["selectedOptions"]

                row = base.copy()
                row.update({
                    "Variant ID": clean_id(v["node"]["id"]),
                    "Variant SKU": v["node"]["sku"],
                    "Barcode": v["node"].get("barcode", ""),
                    "Variant Price": v["node"]["price"],
                    "Variant Compare At Price": v["node"].get("compareAtPrice", ""),
                    "Option1 Name": opts[0]["name"] if len(opts) > 0 else "",
                    "Option1 Value": opts[0]["value"] if len(opts) > 0 else "",
                    "Option2 Name": opts[1]["name"] if len(opts) > 1 else "",
                    "Option2 Value": opts[1]["value"] if len(opts) > 1 else "",
                    "Option3 Name": opts[2]["name"] if len(opts) > 2 else "",
                    "Option3 Value": opts[2]["value"] if len(opts) > 2 else "",
                    "Variant Command": ""
                })
                rows.append(row)

            cursor = p["cursor"]

        if not data["products"]["pageInfo"]["hasNextPage"]:
            break

        time.sleep(0.3)

    product_cols = [
        "Product ID", "Handle", "Title", "Vendor", "Type", "Status", "Tags",
        "Product Command", "Tag Command",
        "Variant ID", "Variant SKU", "Barcode",
        "Variant Price", "Variant Compare At Price",
        "Option1 Name", "Option1 Value",
        "Option2 Name", "Option2 Value",
        "Option3 Name", "Option3 Value",
        "Variant Command"
    ]

    df = pd.DataFrame(rows)
    df = df[product_cols + metafield_cols + metaobject_cols]

    df.to_csv(CSV_FILE, index=False, encoding='utf-8-sig')

    print("EXPORT DONE")


def build_options_from_rows(rows):
    """CSV ki variant rows se options build karo"""
    option_names = {}
    for row in rows:
        for i in range(1, 4):
            name = row.get(f"Option{i} Name", "").strip()
            val = row.get(f"Option{i} Value", "").strip()
            if name and val:
                if name not in option_names:
                    option_names[name] = []
                if val not in option_names[name]:
                    option_names[name].append(val)

    options = []
    for name, values in option_names.items():
        options.append({"name": name, "values": values})
    return options


def build_variants_from_rows(rows):
    """CSV ki variant rows se variants payload build karo"""
    variants = []
    for row in rows:
        v = {
            "sku": row.get("Variant SKU", "").strip(),
            "price": row.get("Variant Price", "").strip(),
            "inventory_management": "shopify",
        }
        if row.get("Barcode", "").strip():
            v["barcode"] = row["Barcode"].strip()
        if row.get("Variant Compare At Price", "").strip():
            v["compare_at_price"] = row["Variant Compare At Price"].strip()
        if row.get("Option1 Value", "").strip():
            v["option1"] = row["Option1 Value"].strip()
        if row.get("Option2 Value", "").strip():
            v["option2"] = row["Option2 Value"].strip()
        if row.get("Option3 Value", "").strip():
            v["option3"] = row["Option3 Value"].strip()
        variants.append(v)
    return variants


def get_target_location_id():
    """
    'Vikhroli Warehouse' location dhundo.
    Nahi mila to pehli available location fallback use karo.
    Returns: single location_id string
    """
    TARGET_NAME = "Vikhroli Warehouse"

    r = requests.get(f"{REST_URL}/locations.json", headers=HEADERS)
    if r.status_code != 200:
        print("   Location fetch failed")
        return ""

    locs = r.json().get("locations", [])
    if not locs:
        print("   No locations found")
        return ""

    for loc in locs:
        if loc["name"].strip().lower() == TARGET_NAME.lower():
            print(f"  📍 Location found: '{loc['name']}' (ID: {loc['id']})")
            return str(loc["id"])

    fallback = locs[0]
    print(f" '{TARGET_NAME}' nahi mila — fallback: '{fallback['name']}' (ID: {fallback['id']})")
    return str(fallback["id"])


def set_inventory_levels(created_variants, location_id, quantity=1):
    """Har variant ka inventory level target location pe set karo"""
    if not location_id:
        return
    for cv in created_variants:
        iid = cv.get("inventory_item_id")
        if not iid:
            continue
        r = requests.post(
            f"{REST_URL}/inventory_levels/set.json",
            headers=HEADERS,
            json={
                "location_id": location_id,
                "inventory_item_id": iid,
                "available": quantity
            }
        )
        if r.status_code == 200:
            print(f"     Inventory set: SKU={cv.get('sku','')} → {quantity} @ location {location_id}")
        else:
            print(f"     Inventory set failed: {r.status_code} | {r.text[:200]}")
        time.sleep(0.1)


def create_product(first_row, all_variant_rows, location_id):
    """
    Naya product create karo REST API se.
    Returns: new product ID (string) ya "" on failure
    """
    options = build_options_from_rows(all_variant_rows)
    variants = build_variants_from_rows(all_variant_rows)

    payload = {
        "product": {
            "title": first_row.get("Title", "").strip(),
            "handle": first_row.get("Handle", "").strip(),
            "vendor": first_row.get("Vendor", "").strip(),
            "product_type": first_row.get("Type", "").strip(),
            "status": first_row.get("Status", "active").lower().strip(),
            "tags": first_row.get("Tags", "").strip(),
            "variants": variants,
        }
    }

    if options:
        payload["product"]["options"] = [{"name": o["name"]} for o in options]

    r = requests.post(f"{REST_URL}/products.json", headers=HEADERS, json=payload)

    if r.status_code in (200, 201):
        product_data = r.json()["product"]
        new_pid = str(product_data["id"])
        created_variants = product_data.get("variants", [])
        print(f"  Done Product created: {first_row.get('Title','')} | ID: {new_pid}")

        set_inventory_levels(created_variants, location_id, quantity=1)

        return new_pid, created_variants
    else:
        print(f"  X Product create failed: {r.status_code} | {r.text[:300]}")
        return "", []


def get_all_publication_ids():
    """Store ke saare sales channels/publications (Online Store, POS, Shop, etc.) ki IDs lao"""
    q = """
    {
      publications(first: 50) {
        edges {
          node {
            id
            name
          }
        }
      }
    }
    """
    data = graphql(q)
    if not data:
        return []

    pubs = [edge["node"] for edge in data["publications"]["edges"]]
    return pubs


def publish_product(pid, publications):
    """Product ko sabhi sales channels (Online Store, POS, Shop app, etc.) par publish karo"""
    if not pid:
        return

    r = requests.put(
        f"{REST_URL}/products/{pid}.json",
        headers=HEADERS,
        json={"product": {"id": pid, "published": True}}
    )
    if r.status_code == 200:
        print(f"     Product published (Online Store)")
    else:
        print(f"     Publish failed: {r.status_code} | {r.text[:200]}")

    # Saare sales channels par publish karo
    if not publications:
        print(f"     No publications found to publish on")
        return

    product_gid = f"gid://shopify/Product/{pid}"

    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    publication_inputs = [{"publicationId": p["id"]} for p in publications]

    result = graphql(mutation, {"id": product_gid, "input": publication_inputs})
    if result:
        errors = result.get("publishablePublish", {}).get("userErrors", [])
        if errors:
            print(f"     Channel publish errors: {errors}")
        else:
            channel_names = ", ".join(p["name"] for p in publications)
            print(f"     Product published to all sales channels: {channel_names}")


def build_backend_maps(products):
    """
    Shopify ke existing products se 2 maps banao:
    - backend_image_map : match_key → [image_urls]
    - backend_metafield_map : match_key → collection_handle value
    """
    backend_image_map      = {}
    backend_image_alt_map  = {}
    backend_metafield_map  = {}
    backend_seo_title_map  = {}
    backend_seo_desc_map   = {}
    backend_pdp_map        = {}

    for p in products:
        model = opt1 = opt2 = ""
        collection_handle_val = pdp_heading_val = ""

        for mf in p["metafields"]["edges"]:
            node = mf["node"]
            if node["namespace"] == "custom":
                if node["key"] == "product_model_name":
                    model = node["value"]
                elif node["key"] == "product_option_1_value":
                    opt1 = node["value"]
                elif node["key"] == "product_option_2_value":
                    opt2 = node["value"]
                elif node["key"] == "collection_handle":
                    collection_handle_val = node["value"]
                elif node["key"] == "pdp_heading":
                    pdp_heading_val = node["value"]

        key = f"{model}-{opt1}-{opt2}".strip().lower()

        images   = [img["node"]["src"]     for img in p["images"]["edges"]]
        alt_texts = [img["node"].get("altText", "") or "" for img in p["images"]["edges"]]
        if images:
            backend_image_map[key]     = images
            backend_image_alt_map[key] = alt_texts

        if collection_handle_val:
            backend_metafield_map[key] = collection_handle_val

        if pdp_heading_val:
            backend_pdp_map[key] = pdp_heading_val

        seo = p.get("seo", {})
        if seo.get("title"):
            backend_seo_title_map[key] = seo["title"]
        if seo.get("description"):
            backend_seo_desc_map[key] = seo["description"]

    print(f"    Image map:          {len(backend_image_map)} entries")
    print(f"   collection_handle:  {len(backend_metafield_map)} entries")
    print(f"   pdp_heading:         {len(backend_pdp_map)} entries")
    print(f"   SEO title map:       {len(backend_seo_title_map)} entries")
    print(f"   SEO desc map:        {len(backend_seo_desc_map)} entries")
    return (backend_image_map, backend_image_alt_map,
            backend_metafield_map, backend_seo_title_map,
            backend_seo_desc_map, backend_pdp_map)


def fetch_all_products_for_mapping():
    """Images aur metafields ke saath saare products fetch karo"""
    products = []
    cursor = None
    print("   Fetching existing products for image/metafield mapping...")

    while True:
        query = """
        query ($cursor: String) {
          products(first: 250, after: $cursor) {
            pageInfo { hasNextPage }
            edges {
              cursor
              node {
                id
                handle
                seo { title description }
                images(first: 50) {
                  edges { node { src altText } }
                }
                metafields(first: 20) {
                  edges {
                    node { namespace key value }
                  }
                }
              }
            }
          }
        }
        """
        data = graphql(query, {"cursor": cursor})
        if not data:
            break

        for edge in data["products"]["edges"]:
            products.append(edge["node"])
            cursor = edge["cursor"]

        if not data["products"]["pageInfo"]["hasNextPage"]:
            break

        time.sleep(0.2)

    print(f"  Done Total products fetched for mapping: {len(products)}")
    return products


def upload_images_to_product(pid, image_urls, alt_texts=None):
    """Product pe images upload karo (URL se), alt text bhi set karo"""
    url = f"{REST_URL}/products/{pid}/images.json"
    for i, img_url in enumerate(image_urls, start=1):
        alt = (alt_texts[i-1] if alt_texts and i-1 < len(alt_texts) else "") or ""
        payload = {"image": {"src": img_url, "position": i, "alt": alt}}
        r = requests.post(url, headers=HEADERS, json=payload)
        if r.status_code in (200, 201):
            print(f"      Image uploaded | Position: {i} | Alt: '{alt[:40]}' | Product: {pid}")
        else:
            print(f"     Image FAILED | Position: {i} | Status: {r.status_code} | {r.text[:150]}")
        time.sleep(0.1)  # Done OPTIMIZED: 0.15 → 0.1


def copy_collection_handle_metafield(pid, collection_handle_value):
    """collection_handle metafield set/update karo"""
    url = f"{REST_URL}/products/{pid}/metafields.json"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        print(f"     Metafield fetch failed | Product: {pid}")
        return

    existing_id = None
    for mf in r.json().get("metafields", []):
        if mf["namespace"] == "custom" and mf["key"] == "collection_handle":
            existing_id = mf["id"]
            break

    if existing_id:
        res = requests.put(
            f"{REST_URL}/metafields/{existing_id}.json",
            headers=HEADERS,
            json={"metafield": {"id": existing_id, "value": collection_handle_value, "type": "single_line_text_field"}}
        )
        action = "UPDATED"
    else:
        res = requests.post(
            url,
            headers=HEADERS,
            json={"metafield": {"namespace": "custom", "key": "collection_handle", "value": collection_handle_value, "type": "single_line_text_field"}}
        )
        action = "CREATED"

    if res.status_code in (200, 201):
        print(f"     collection_handle {action} | Product: {pid} | Value: {collection_handle_value}")
    else:
        print(f"     collection_handle FAILED | Product: {pid} | {res.status_code} | {res.text[:150]}")


def update_seo_and_pdp(pid, seo_title, seo_desc, pdp_heading):
    """SEO title, SEO description aur pdp_heading ek saath update karo"""

    seo_payload = {}
    if seo_title:
        seo_payload["metafields_global_title_tag"] = seo_title
    if seo_desc:
        seo_payload["metafields_global_description_tag"] = seo_desc

    if seo_payload:
        seo_payload["id"] = pid
        r = requests.put(
            f"{REST_URL}/products/{pid}.json",
            headers=HEADERS,
            json={"product": seo_payload}
        )
        if r.status_code == 200:
            print(f"     SEO updated | title='{seo_title[:40]}...' " if len(seo_title) > 40 else f"     SEO updated | title='{seo_title}'")
        else:
            print(f"     SEO update failed | {r.status_code} | {r.text[:150]}")

    if pdp_heading:
        url = f"{REST_URL}/products/{pid}/metafields.json"
        r = requests.get(url, headers=HEADERS)
        existing_id = None
        if r.status_code == 200:
            for mf in r.json().get("metafields", []):
                if mf["namespace"] == "custom" and mf["key"] == "pdp_heading":
                    existing_id = mf["id"]
                    break

        if existing_id:
            res = requests.put(
                f"{REST_URL}/metafields/{existing_id}.json",
                headers=HEADERS,
                json={"metafield": {"id": existing_id, "value": pdp_heading, "type": "single_line_text_field"}}
            )
            action = "UPDATED"
        else:
            res = requests.post(
                url,
                headers=HEADERS,
                json={"metafield": {"namespace": "custom", "key": "pdp_heading", "value": pdp_heading, "type": "single_line_text_field"}}
            )
            action = "CREATED"

        if res.status_code in (200, 201):
            print(f"     pdp_heading {action} | Value: {pdp_heading[:60]}")
        else:
            print(f"     pdp_heading FAILED | {res.status_code} | {res.text[:150]}")


def make_match_key(row):
    """CSV row se match_key banao (model-opt1-opt2)"""
    model = str(row.get("Metafield:custom.product_model_name[single_line_text_field]", "")).strip()
    opt1  = str(row.get("Metafield:custom.product_option_1_value[single_line_text_field]", "")).strip()
    opt2  = str(row.get("Metafield:custom.product_option_2_value[single_line_text_field]", "")).strip()
    return f"{model}-{opt1}-{opt2}".strip().lower()


def import_all():
    print("IMPORT MODE")

    df = read_csv_safe(CSV_FILE)

    location_id = get_target_location_id()
    if not location_id:
        print("   No location found — inventory levels will not be set")

    metafield_cols = [c for c in df.columns if c.startswith("Metafield:")]

    existing_products = fetch_all_products_for_mapping()
    (backend_image_map, backend_image_alt_map,
     backend_metafield_map, backend_seo_title_map,
     backend_seo_desc_map, backend_pdp_map) = build_backend_maps(existing_products)

    publications = get_all_publication_ids()
    print(f"   Publications loaded: {len(publications)} channels")

    groups = {}
    order = []
    for idx, row in df.iterrows():
        handle = row.get("Handle", "").strip()
        if not handle:
            continue
        if handle not in groups:
            groups[handle] = []
            order.append(handle)
        groups[handle].append(row)

    for handle in order:
        rows = groups[handle]
        first_row = rows[0]

        pid = first_row.get("Product ID", "").strip()
        p_cmd = first_row.get("Product Command", "").lower().strip()

        if p_cmd == "delete" and pid:
            r = requests.delete(f"{REST_URL}/products/{pid}.json", headers=HEADERS)
            print(f"   Deleted product {pid} | status: {r.status_code}")
            time.sleep(0.3)
            continue

        if not pid:
            print(f"   Creating new product: {first_row.get('Title', handle)}")
            pid, created_variants = create_product(first_row, rows, location_id)

            if not pid:
                print(f"   Skipping handle '{handle}' — product create failed")
                time.sleep(0.3)
                continue

            sku_to_vid = {}
            for cv in created_variants:
                sku_to_vid[cv.get("sku", "")] = str(cv.get("id", ""))

            set_metafields_for_product(pid, first_row, metafield_cols)

            publish_product(pid, publications)

            match_key = make_match_key(first_row)

            if match_key in backend_image_map:
                imgs = backend_image_map[match_key]
                alts = backend_image_alt_map.get(match_key, [])
                print(f"    Uploading {len(imgs)} image(s) for: {match_key}")
                upload_images_to_product(pid, imgs, alts)
            else:
                print(f"   No image match found for key: {match_key}")

            if match_key in backend_metafield_map:
                print(f"   Copying collection_handle for: {match_key}")
                copy_collection_handle_metafield(pid, backend_metafield_map[match_key])
            else:
                print(f"   No collection_handle match for key: {match_key}")

            seo_title  = backend_seo_title_map.get(match_key, "")
            seo_desc   = backend_seo_desc_map.get(match_key, "")
            pdp        = backend_pdp_map.get(match_key, "")
            if seo_title or seo_desc or pdp:
                print(f"   Copying SEO + pdp_heading for: {match_key}")
                update_seo_and_pdp(pid, seo_title, seo_desc, pdp)
            else:
                print(f"   No SEO/pdp match for key: {match_key}")

            time.sleep(0.3)
            continue

        if p_cmd == "update" and pid:
            requests.put(f"{REST_URL}/products/{pid}.json", headers=HEADERS, json={
                "product": {
                    "id": pid,
                    "title": first_row.get("Title", ""),
                    "vendor": first_row.get("Vendor", ""),
                    "product_type": first_row.get("Type", ""),
                    "status": first_row.get("Status", "").lower()
                }
            })
            print(f"  ✏ Updated product {pid}")

        tag_cmd = first_row.get("Tag Command", "").lower().strip()
        if tag_cmd:
            apply_tag_command(pid, first_row.get("Tags", ""), tag_cmd)

        for row in rows:
            vid = row.get("Variant ID", "").strip()
            vcmd = row.get("Variant Command", "").lower().strip()

            if vcmd == "create" and pid:
                payload = {
                    "variant": {
                        "product_id": pid,
                        "sku": row.get("Variant SKU", ""),
                        "barcode": row.get("Barcode", ""),
                        "price": row.get("Variant Price", ""),
                        "compare_at_price": row.get("Variant Compare At Price", "") or None,
                        "option1": row.get("Option1 Value") or None,
                        "option2": row.get("Option2 Value") or None,
                        "option3": row.get("Option3 Value") or None,
                    }
                }
                r = requests.post(f"{REST_URL}/variants.json", headers=HEADERS, json=payload)
                print(f"    + Variant created SKU={row.get('Variant SKU','')} | {r.status_code}")

            elif vcmd == "update" and vid:
                payload = {
                    "id": vid,
                    "sku": row.get("Variant SKU", ""),
                    "barcode": row.get("Barcode", ""),
                    "price": row.get("Variant Price", "")
                }
                if row.get("Variant Compare At Price"):
                    payload["compare_at_price"] = row["Variant Compare At Price"]
                if row.get("Option1 Value"):
                    payload["option1"] = row["Option1 Value"]
                if row.get("Option2 Value"):
                    payload["option2"] = row["Option2 Value"]
                if row.get("Option3 Value"):
                    payload["option3"] = row["Option3 Value"]

                requests.put(
                    f"{REST_URL}/variants/{vid}.json",
                    headers=HEADERS,
                    json={"variant": payload}
                )

            elif vcmd == "delete" and vid:
                requests.delete(f"{REST_URL}/variants/{vid}.json", headers=HEADERS)
                print(f"     Variant deleted: {vid}")

            time.sleep(0.1)

        set_metafields_for_product(pid, first_row, metafield_cols)

        time.sleep(0.1)

    print("IMPORT DONE")


def set_metafields_for_product(pid, row, metafield_cols):
    """Ek product ke liye metafields set karo"""
    if not pid:
        return

    mfs = []
    for c in metafield_cols:
        if row.get(c, "").strip():
            try:
                ns_key, t = c.replace("Metafield:", "").split("[")
                ns, key = ns_key.split(".")
                mfs.append({
                    "ownerId": f"gid://shopify/Product/{pid}",
                    "namespace": ns,
                    "key": key,
                    "type": t.replace("]", ""),
                    "value": row[c]
                })
            except ValueError:
                print(f"   Malformed metafield column: {c}")

    if mfs:
        result = graphql("""
        mutation ($m: [MetafieldsSetInput!]!) {
          metafieldsSet(metafields: $m) {
            userErrors { field message }
          }
        }
        """, {"m": mfs})

        if result:
            errors = result.get("metafieldsSet", {}).get("userErrors", [])
            if errors:
                print(f"   Metafield errors for product {pid}: {errors}")
            else:
                print(f"   Metafields set for product {pid} ({len(mfs)} fields)")


def get_variant_id_by_sku(sku):
    """SKU se variant ID (numeric, REST ke liye) aur product title nikalo"""
    query = """
    query($query: String!) {
      productVariants(first: 1, query: $query) {
        edges {
          node {
            id
            sku
            product { title }
          }
        }
      }
    }
    """
    data = graphql(query, {"query": f"sku:{sku}"})
    if not data:
        return None, None

    edges = data.get("productVariants", {}).get("edges", [])
    if not edges:
        return None, None

    node = edges[0]["node"]
    variant_id = clean_id(node["id"])
    product_title = node["product"]["title"]
    return variant_id, product_title


def update_variant_price(variant_id, new_price, compare_at_price=None):
    """REST API se variant ka price (aur optional compare_at_price) update karo"""
    variant_payload = {"id": variant_id, "price": new_price}
    if compare_at_price:
        variant_payload["compare_at_price"] = compare_at_price

    r = requests.put(
        f"{REST_URL}/variants/{variant_id}.json",
        headers=HEADERS,
        json={"variant": variant_payload}
    )
    return r.status_code == 200


def update_prices_from_excel(excel_path):
    """
    Excel mein 'SKU'/'Variant SKU' aur 'Price'/'Variant Price' columns hone chahiye.
    Optional 'Compare At Price'/'Variant Compare At Price' column bhi support hai.
    Har row ke SKU se Shopify variant dhundo aur price update karo.
    """
    print("===== PRICE UPDATE: START =====")

    df = pd.read_excel(excel_path, dtype=str)
    df.columns = df.columns.str.strip()

    cols = {c.lower().strip(): c for c in df.columns}
    sku_col     = cols.get("sku") or cols.get("variant sku")
    price_col   = cols.get("price") or cols.get("variant price")
    compare_col = cols.get("compare at price") or cols.get("variant compare at price")

    if not sku_col or not price_col:
        print("X ERROR: Excel mein 'SKU'/'Variant SKU' aur 'Price'/'Variant Price' column hone chahiye.")
        print("===== PRICE UPDATE: DONE =====")
        return

    success, skipped, failed = 0, 0, 0

    for idx, row in df.iterrows():
        sku = str(row.get(sku_col, "")).strip()
        price = str(row.get(price_col, "")).strip()
        compare_price = str(row.get(compare_col, "")).strip() if compare_col else ""

        if not sku or not price or sku.lower() == "nan" or price.lower() == "nan":
            continue

        if compare_price.lower() in ("nan", "none", ""):
            compare_price = None

        log_msg = f" SKU: {sku} → New Price: {price}"
        if compare_price:
            log_msg += f" | Compare At: {compare_price}"
        print(log_msg)

        variant_id, product_title = get_variant_id_by_sku(sku)

        if not variant_id:
            print(f"   ️ SKU '{sku}' Shopify par nahi mila. Skip.")
            skipped += 1
            print("-" * 50)
            continue

        ok = update_variant_price(variant_id, price, compare_price)
        if ok:
            print(f"   Done Price updated | Product: {product_title} | SKU: {sku} | New Price: {price}")
            success += 1
        else:
            print(f"   X Price update FAILED | SKU: {sku}")
            failed += 1

        print("-" * 50)
        time.sleep(0.2)

    print("\n" + "=" * 50)
    print(" PRICE UPDATE SUMMARY")
    print("=" * 50)
    print(f"Done Updated : {success}")
    print(f"️ Skipped : {skipped}")
    print(f"X Failed  : {failed}")
    print("=" * 50)
    print("===== PRICE UPDATE: DONE =====")

def get_product_gid_by_sku(sku):
    """
    SKU se product GID (numeric REST id) aur product title nikalo.
    """
    query = """
    query($q: String!) {
      productVariants(first: 1, query: $q) {
        edges {
          node {
            product { id title }
          }
        }
      }
    }
    """
    data = graphql(query, {"q": f"sku:{sku}"})
    if not data:
        return None, None
    edges = data.get("productVariants", {}).get("edges", [])
    if not edges:
        return None, None
    node = edges[0]["node"]["product"]
    return clean_id(node["id"]), node["title"]


def update_status_from_excel(excel_path):
    """
    Excel mein 2 columns chahiye:
      - 'SKU' ya 'Variant SKU'
      - 'Status'  →  valid values: active | draft | archived

    Har SKU ke liye uska product dhundha jaata hai aur status update hoti hai.
    Ek product ek baar hi update hoga, chahe Excel mein uske multiple SKUs hon.
    """
    print("===== STATUS UPDATE: START =====")

    df = pd.read_excel(excel_path, dtype=str)
    df.columns = df.columns.str.strip()

    cols = {c.lower().strip(): c for c in df.columns}
    sku_col    = cols.get("sku") or cols.get("variant sku")
    status_col = cols.get("status")

    if not sku_col or not status_col:
        print("X ERROR: Excel mein 'SKU' (ya 'Variant SKU') aur 'Status' column hone chahiye.")
        print("===== STATUS UPDATE: DONE =====")
        return

    valid_statuses = {"active", "draft", "archived"}
    success, skipped, failed = 0, 0, 0
    updated_products = set()

    for _, row in df.iterrows():
        sku    = str(row.get(sku_col,    "")).strip()
        status = str(row.get(status_col, "")).strip().lower()

        if not sku or not status or sku.lower() == "nan" or status.lower() == "nan":
            continue

        if status not in valid_statuses:
            print(f"️ SKU: {sku} → Invalid status '{status}'. Valid: active / draft / archived")
            skipped += 1
            print("-" * 50)
            continue

        print(f" SKU: {sku} → Target Status: {status}")

        pid, title = get_product_gid_by_sku(sku)

        if not pid:
            print(f"   ️ SKU '{sku}' Shopify par nahi mila. Skip.")
            skipped += 1
            print("-" * 50)
            continue

        if pid in updated_products:
            print(f"    Product '{title}' is run mein already update ho chuka hai. Skip.")
            skipped += 1
            print("-" * 50)
            continue

        r = requests.put(
            f"{REST_URL}/products/{pid}.json",
            headers=HEADERS,
            json={"product": {"id": pid, "status": status}}
        )

        if r.status_code == 200:
            print(f"   Done Status updated | Product: {title} | SKU: {sku} | → {status}")
            updated_products.add(pid)
            success += 1
        else:
            print(f"   X Status update FAILED | SKU: {sku} | {r.status_code} | {r.text[:150]}")
            failed += 1

        print("-" * 50)
        time.sleep(0.2)

    print("\n" + "=" * 50)
    print(" STATUS UPDATE SUMMARY")
    print("=" * 50)
    print(f"Done Updated : {success}")
    print(f"️  Skipped : {skipped}")
    print(f"X Failed  : {failed}")
    print("=" * 50)
    print("===== STATUS UPDATE: DONE =====")


if __name__ == "__main__":
    if MODE == "EXPORT":
        export_all()
    elif MODE == "IMPORT":
        convert_excel_to_csv(INPUT_EXCEL, CSV_FILE)
        import_all()
    else:
        print("INVALID MODE")
