import requests
import os
import time

import shopify_sync as sync
import config

GOOGLE_API_KEY = config.GOOGLE_API_KEY

API_VERSION = "2024-04"
SHOPIFY_STORE = f"{sync.SHOP}.myshopify.com"
ACCESS_TOKEN = sync.ACCESS_TOKEN

SHOPIFY_GRAPHQL_URL = f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}/graphql.json"
SHOPIFY_HEADERS = {
    "X-Shopify-Access-Token": ACCESS_TOKEN,
    "Content-Type": "application/json"
}


def graphql(query, variables=None):
    r = requests.post(
        SHOPIFY_GRAPHQL_URL,
        headers=SHOPIFY_HEADERS,
        json={"query": query, "variables": variables or {}}
    )
    data = r.json()
    if "errors" in data:
        raise Exception(data["errors"])
    return data["data"]


def get_videos_from_drive(drive_folder_id):
    print(" Google Drive folder se videos scan kar rahe hain...")
    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{drive_folder_id}' in parents and mimeType contains 'video/' and trashed = false",
        "fields": "files(id, name, size)",
        "key": GOOGLE_API_KEY
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        files = response.json().get('files', [])
        print(f" {len(files)} videos mili hain Drive mein.")
        return files
    else:
        print("X Drive API Error:", response.text)
        return []

def get_product_id_by_sku(sku):
    query = """
    query($query: String!) {
      productVariants(first: 1, query: $query) {
        edges {
          node {
            sku
            product {
              id
              handle
            }
          }
        }
      }
    }
    """
    variables = {"query": f"sku:{sku}"}
    response = requests.post(
        SHOPIFY_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=SHOPIFY_HEADERS
    )

    print(f"   📡 Shopify API Status: {response.status_code}")
    res_data = response.json()

    if response.status_code != 200:
        print(f"   X Shopify API call failed with status {response.status_code}")
        return None, None

    if 'errors' in res_data:
        print(f"   X GraphQL Errors: {res_data['errors']}")
        return None, None

    edges = res_data.get('data', {}).get('productVariants', {}).get('edges', [])
    if edges:
        node = edges[0]['node']
        print(f"    Shopify mein mila SKU: {node['sku']}")
        product_info = node['product']
        return product_info['id'], product_info['handle']

    print(f"   Koi variant nahi mila SKU '{sku}' ke liye.")
    return None, None


def create_staged_upload(filename, filesize):
    print(f"    Shopify se staged upload URL le rahe hain...")
    staged_mutation = """
    mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
      stagedUploadsCreate(input: $input) {
        stagedTargets {
          url
          resourceUrl
          parameters { name value }
        }
        userErrors { message }
      }
    }
    """
    variables = {
        "input": [{
            "resource": "VIDEO",
            "filename": filename,
            "mimeType": "video/mp4",
            "httpMethod": "POST",
            "fileSize": str(filesize)
        }]
    }
    res = requests.post(
        SHOPIFY_GRAPHQL_URL,
        json={"query": staged_mutation, "variables": variables},
        headers=SHOPIFY_HEADERS
    ).json()

    staged_errors = res.get('data', {}).get('stagedUploadsCreate', {}).get('userErrors', [])
    if staged_errors:
        raise Exception(f"Staged Upload Errors: {staged_errors}")

    targets = res.get('data', {}).get('stagedUploadsCreate', {}).get('stagedTargets', [])
    if not targets:
        raise Exception("Staged upload target nahi mila.")

    print(f"    Staged Upload URL mila.")
    return targets[0]


def upload_to_bucket(target, drive_file_id, file_name):
    print(f"    Google Drive se Shopify bucket par upload ho rahi hai...")
    drive_download_url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media&key={GOOGLE_API_KEY}"
    form_data = {param['name']: param['value'] for param in target['parameters']}

    with requests.get(drive_download_url, stream=True) as drive_stream:
        if drive_stream.status_code != 200:
            raise Exception(f"Google Drive stream failed. Status: {drive_stream.status_code}")

        files = {'file': (file_name, drive_stream.raw, "video/mp4")}
        upload_res = requests.post(target['url'], data=form_data, files=files)

        if upload_res.status_code not in [200, 201, 204]:
            raise Exception(f"Bucket upload fail. Status: {upload_res.status_code} | {upload_res.text[:300]}")

    print(f"    Video Shopify bucket par upload ho gayi.")


def attach_video(product_gid, video_url, file_name):
    print(f"    Video ko product se attach kar rahe hain...")
    media_mutation = """
    mutation productCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
      productCreateMedia(productId: $productId, media: $media) {
        media {
          id
          mediaContentType
          status
        }
        userErrors { message }
      }
    }
    """
    media_variables = {
        "productId": product_gid,
        "media": [{
            "alt": f"Video for {file_name}",
            "mediaContentType": "VIDEO",
            "originalSource": video_url
        }]
    }
    d = graphql(media_mutation, media_variables)
    if d["productCreateMedia"]["userErrors"]:
        raise Exception(d["productCreateMedia"]["userErrors"])

    media_id = d["productCreateMedia"]["media"][0]["id"]
    print(f"    Video attach ho gayi. Media ID: {media_id}")
    return media_id


def wait_until_ready(media_id, timeout=180):
    print(f"    Video processing ka wait kar rahe hain (max {timeout}s)...")
    query = """
    query ($id: ID!) {
      node(id: $id) {
        ... on Media {
          status
        }
      }
    }
    """
    start = time.time()
    while True:
        status = graphql(query, {"id": media_id})["node"]["status"]
        print(f"   Media status: {status}")
        if status == "READY":
            print(f"    Video READY hai!")
            return
        if status == "FAILED":
            raise Exception("Video processing FAILED Shopify par.")
        if time.time() - start > timeout:
            raise Exception("Timeout: Video READY nahi huyi.")
        time.sleep(8)


def move_video_to_first(product_gid, media_id):
    print(f"    Video ko pehli position par move kar rahe hain...")
    query = """
    mutation ($id: ID!, $moves: [MoveInput!]!) {
      productReorderMedia(id: $id, moves: $moves) {
        userErrors { message }
      }
    }
    """
    variables = {
        "id": product_gid,
        "moves": [{"id": media_id, "newPosition": "1"}]
    }
    d = graphql(query, variables)
    if d["productReorderMedia"]["userErrors"]:
        raise Exception(d["productReorderMedia"]["userErrors"])
    print(f"    Video pehli position par set ho gayi!")


def get_drive_file_size(drive_file_id):
    url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}"
    params = {"fields": "size", "key": GOOGLE_API_KEY}
    res = requests.get(url, params=params)
    if res.status_code == 200:
        size = int(res.json().get('size', 0))
        print(f"   📦 File size: {size / (1024*1024):.2f} MB")
        return size
    print(f"   File size nahi mila.")
    return 0


def upload_and_attach_video(drive_file_id, file_name, product_id):
    file_size = get_drive_file_size(drive_file_id)
    target = create_staged_upload(file_name, file_size)
    upload_to_bucket(target, drive_file_id, file_name)
    media_id = attach_video(product_id, target['resourceUrl'], file_name)
    wait_until_ready(media_id)
    move_video_to_first(product_id, media_id)


def start_automation(drive_folder_id):
    print("=" * 60)
    print(" Shopify Video Uploader - Starting...")
    print("=" * 60)

    if not drive_folder_id or not str(drive_folder_id).strip():
        print(" Drive Folder ID nahi diya gaya. Video upload skip ho rha hai.")
        return

    drive_folder_id = str(drive_folder_id).strip()

    files = get_videos_from_drive(drive_folder_id)
    if not files:
        print(" Folder me koi video nahi mili. Video upload skip ho rha hai.")
        return

    print(f"\n Total {len(files)} videos process hongi.\n")

    success_count = 0
    skip_count = 0
    fail_count = 0

    for index, file in enumerate(files, start=1):
        file_name = file['name']
        file_id = file['id']

        print(f"[{index}/{len(files)}] 🔍 File: {file_name}")

        if not file_name.lower().endswith('.mp4'):
            print(f"   .mp4 nahi hai, skip kar rahe hain.")
            skip_count += 1
            print("-" * 50)
            continue

        sku = os.path.splitext(file_name)[0]
        print(f"     SKU: {sku}")

        product_id, product_handle = get_product_id_by_sku(sku)

        if product_id and product_handle:
            print(f"    Product mila! Handle: {product_handle}")
            try:
                upload_and_attach_video(file_id, file_name, product_id)
                print(f"    Poora process complete!")
                success_count += 1
            except Exception as e:
                print(f"   X ERROR: {e}")
                fail_count += 1
        else:
            print(f"   SKU '{sku}' Shopify par nahi mila. Skipping...")
            skip_count += 1

        print("-" * 50)

    print("\n" + "=" * 60)
    print(" FINAL VIDEO SUMMARY")
    print("=" * 60)
    print(f" Successfully processed : {success_count}")
    print(f"  Skipped               : {skip_count}")
    print(f"X Failed                 : {fail_count}")
    print("=" * 60)


if __name__ == "__main__":
    start_automation("")
