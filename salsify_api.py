from dotenv import load_dotenv
import aiohttp
import asyncio
import argparse
import http.client
import json
import base64
import os
import logging
import sys
import traceback
import config

from salsify_extras import updateProductData 
load_dotenv()

#  logs setuo
open('api.log', 'w').close()
open('errors.log', 'w').close()
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

all_handler = logging.FileHandler('api.log')
all_handler.setLevel(logging.DEBUG)
all_handler.setFormatter(formatter)

error_handler = logging.FileHandler('errors.log')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(formatter)

logging.basicConfig(level=logging.DEBUG, handlers=[all_handler, error_handler])

# Args setup
parser=argparse.ArgumentParser(
    description='''Salsify to Wordpress API. Pulls product data from Salsify and imports it to its respective Wordpress product. Assumes your site already has a custom post type Product.''',
    epilog="""Developed by @fluffybacon-steam""")
parser.add_argument('--site', type=str, help='Define environment on which to run (required).')
parser.add_argument('--force', action='store_true', help='Force synchronization; disregards salsify_updated_last check')
parser.add_argument('--single', type=str, help='Resync a singular product using wordpress post id')
parser.add_argument('--ignore-list', action='store_true', help='Ignores white/black list from wordpress (old)|')
args=parser.parse_args()

if 'just' in args.site:
    config.user = 'robo'
    if args.site == 'justbare':
        config.base_url = 'justbarefoods.com'
    else: 
        config.base_url = args.site
    config.app_password = os.getenv('JUST_BARE_AP')
    config.filter = "='Brand Name':{'Just Bare','Just Bare Brand'}"
elif 'pilgrims' in args.site:
    config.user = 'robot'
    if args.site == 'pilgrims':
        config.base_url = 'pilgrimsusa.com'
    else: 
        config.base_url = args.site
    config.app_password = os.getenv('PILGRIMS_AP')
    config.filter = "='Brand Name':{'Pilgrim\'s'}"
else:
    print("Need --site")
    quit()
    
config.auth_token = base64.b64encode(f"{config.user}:{config.app_password}".encode()).decode()
org_id = os.getenv('ORG_ID')
salsify_headers = {'Authorization': os.getenv('SALSIFY_AUTH')}

def fetchProducts_fromSalsifyList(list_id):
    sal_conn = http.client.HTTPSConnection('app.salsify.com')
    sal_conn.request("GET", f'/api/v1/orgs/{org_id}/products/?filter=%3Dlist%3A{list_id}', headers=salsify_headers)
    response = sal_conn.getresponse()
    if response.status != 200:
        raise Exception(f"Fetch Salisfy products from list failed: {response.status}")
    data = json.loads(response.read())
    return [product["salsify:id"] for product in data.get('data')]

async def fetchProducts_fromWordpress(lists, single_post_id=None):
    '''
    Fetch products from WordPress. Returns list of matching products.
    Will create new products for any GTINs/SKUs missing from WordPress.
    '''
    product_list_id = lists.get('product_list')
    if product_list_id:
        # Pull products only from Salsify list
        product_list = fetchProducts_fromSalsifyList(product_list_id)
        white_skus  = []
        white_gtins = []
        black_skus  = []
        black_gtins = []
    else:
        # Use old method
        product_list = []
        white_skus  = [item.strip() for item in lists.get('sku_list', '').split(',') if item.strip()]
        white_gtins = [item.strip() for item in lists.get('gtin_list', '').split(',') if item.strip()]
        black_skus  = [item.strip() for item in lists.get('sku_blacklist', '').split(',') if item.strip()]
        black_gtins = [item.strip() for item in lists.get('gtin_blacklist', '').split(',') if item.strip()]

    wp_prods = []
    page = 1
    per_page = 10
    fields = 'id,acf.product_gtin,meta.salsify_last_updated_time_stamp,title,acf.product_baz_id,acf.product_sku'

    if 'localhost' in config.base_url:
        conn = http.client.HTTPConnection(config.base_url)
    else:
        conn = http.client.HTTPSConnection(config.base_url)
    headers = {
        'Authorization': f'Basic {config.auth_token}'
    }
    try: 
        async with aiohttp.ClientSession() as session:
            if single_post_id is not None:
                params = f'/{single_post_id}?_fields={fields}'
                conn.request("GET", f"/wp-json/wp/v2/product{params}", headers=headers)
                response = conn.getresponse()
                if response.status == 200:
                    data = response.read()
                    wp_prods.append(json.loads(data))
            else :
                while page >= 1:
                    params = f'?page={page}&per_page={per_page}&_fields={fields}'
                    conn.request("GET", f"/wp-json/wp/v2/product{params}", headers=headers)
                    logging.info('Requesting page #%s : %s', page, f"/wp-json/wp/v2/product{params}")
                    response = conn.getresponse()
                    logging.info(response.status)
                    if response.status == 200:
                        data = response.read()
                        logging.info(data)
                        wp_prods_data = json.loads(data)
                        for wp_prod in wp_prods_data:
                            logging.info('post: %s', wp_prod)
                            wp_prod_gtin = wp_prod['acf'].get('product_gtin')
                            wp_prod_sku = wp_prod['acf'].get('product_sku') or wp_prod['acf'].get('product_baz_id')

                            # Filter blacklists
                            if wp_prod_gtin not in black_gtins and wp_prod_sku not in black_skus or wp_prod_gtin in product_list:
                                wp_prods.append(wp_prod)
                            else:
                                await updateWordPressProduct(wp_prod.get('id'), {'status': 'draft'}, session)

                            # Remove known ones from whitelist
                            if wp_prod_gtin in white_gtins:
                                white_gtins.remove(wp_prod_gtin)
                            if wp_prod_sku in white_skus:
                                white_skus.remove(wp_prod_sku)
                            if wp_prod_gtin in product_list:
                                product_list.remove(wp_prod_gtin)

                        if len(wp_prods_data) == per_page and page < 50:
                            page += 1
                        else:
                            page = 0
                    else:
                        logging.error(f"Failed to fetch data. Status code: {response.status}")
                        break

                conn.close()

                # Log missing whitelist items
                if white_gtins or white_skus or product_list:
                    logging.warning("Missing products detected!")
                    logging.warning("   Remaining white GTINs: %s", white_gtins)
                    logging.warning("   Remaining white SKUs: %s", white_skus)
                    logging.warning("   Remaining product_list GTIN: %s", product_list)

                    # Create placeholder post id for missing items
                    for gtin in white_gtins:
                        new_data = {'acf': {'product_gtin': gtin}}
                        create_post = await updateWordPressProduct(None, new_data, session)
                        if create_post is not None:
                            if type(create_post) == list:
                                wp_prods.append(create_post[0])
                            else:
                                wp_prods.append(create_post)

                    for sku in white_skus:
                        new_data = {'acf': {'product_sku': sku}}
                        create_post = await updateWordPressProduct(None, new_data, session)
                        if create_post is not None:
                            if type(create_post) == list:
                                wp_prods.append(create_post[0])
                            else:
                                wp_prods.append(create_post)
                    
                    for gtin in product_list:
                        new_data = {'acf': {'product_gtin': gtin}}
                        create_post = await updateWordPressProduct(None, new_data, session)
                        if create_post is not None:
                            if type(create_post) == list:
                                wp_prods.append(create_post[0])
                            else:
                                wp_prods.append(create_post)

        return wp_prods
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching Wordpress product: {e}")
        sys.exit(1)

async def synchronize_with_Salsify(wp_prods):
    '''
    Retrieves new products from Salsify that are not in post_gtins already.
    ### returns [ new_post_gtins ]
    'sku_list' , 'gtin_list', 'sku_blacklist', 'gtin_blacklist'
    '''
    
    async with aiohttp.ClientSession() as session:
        requested_salsify_prods = await asyncio.gather(*[
            fetchProduct_fromSalsify(wp_prod, session) for wp_prod in wp_prods
        ])
        tasks = []
        for item in requested_salsify_prods:
            if item is None:
                continue
            wp_prod = item[0]
            salsify_data = item[1]
            if salsify_data is None:
                continue
            logging.info('Syncing product %s with %s', wp_prod['id'], salsify_data['salsify:id'])
            
            logging.info(wp_prod)
            if args.force:  
                tasks.append(updateProductData(wp_prod, salsify_data, session, args.site))
            elif wp_prod.get('meta') and wp_prod['meta'].get('salsify_last_updated_time_stamp') != salsify_data.get('salsify:updated_at'):
                tasks.append(updateProductData(wp_prod, salsify_data, session, args.site))
            else:
                logging.error("Failed to sync %s",wp_prod)

        new_post_data = await asyncio.gather(*tasks)
        for new_post in new_post_data:
            await updateWordPressProduct(new_post.get('id'), new_post, session)

async def updateWordPressProduct(post_id, data, session):
    '''
    Update or create a WordPress product post using async HTTP requests.
    Returns:
        - new post ID if created
        - status code if updated
    '''
    logging.info('updateProduct()')
    logging.info('data: %s',data)
    
    protocol = 'https://'
    if 'localhost' in config.base_url:
        protocol = 'http://'
    url_base = f"{protocol}{config.base_url}/wp-json/wp/v2/product"
    headers = {
        'Authorization': f'Basic {config.auth_token}',
        'Content-type': 'application/json'
    }

    if post_id: 
        url = f"{url_base}/{post_id}"
    else: 
        url = url_base
        data['title'] = 'new salsify product'
        data['status'] = 'draft'
        data['meta'] = {'salsify_last_updated_time_stamp' : ''}

    async with session.post(url, headers=headers, json=data) as response:
        status = response.status
        body = await response.text()

        if status == 200:
            logging.info("Product post updated successfully")
        elif status == 201:
            logging.info("Product post created successfully")
        else:
            logging.error(f"Failed to update product post. Status code: {status}, Reason: {response.reason}")
            logging.error(body)
            return None
        try:
            return await response.json()
        except Exception as e:
            logging.error("Failed to parse post response: %s", e)
            return None

            # # fallback: attempt partial updates one field at a time
            # for point, value in data.items():
            #     await asyncio.sleep(1)
            #     payload = {}

            #     if isinstance(value, dict):
            #         for entry, entry_value in value.items():
            #             await asyncio.sleep(1)
            #             payload = {point: {entry: entry_value}}
            #             async with session.post(f"{url_base}/{post_id}", headers=headers, json=payload) as retry_resp:
            #                 await retry_resp.text()
            #                 if retry_resp.status >= 400:
            #                     logging.warning("Bad entry during fallback POST: %s", payload)
            #     else:
            #         payload = {point: value}
            #         async with session.post(f"{url_base}/{post_id}", headers=headers, json=payload) as retry_resp:
            #             await retry_resp.text()
            #             if retry_resp.status >= 400:
            #                 logging.warning("Bad entry during fallback POST: %s", payload)
    return None

async def deleteWordPressProduct(post_id, session):
    """
    Delete a WooCommerce product via the REST API.

    Args:
        post_id (int): The ID of the product to delete.
        session (aiohttp.ClientSession): An active aiohttp session.

    Returns:
        dict or None: JSON response from the API if successful; None otherwise.
    """
    logging.info('Attempting to delete product with ID: %s', post_id)

    protocol = 'https://'
    if 'localhost' in config.base_url:
        protocol = 'http://'
    url = f"{protocol}{config.base_url}/wp-json/wp/v2/product/{post_id}?force=true"
    headers = {
        'Authorization': f'Basic {config.auth_token}',
        'Content-Type': 'application/json'
    }

    try:
        async with session.delete(url, headers=headers) as response:
            status = response.status
            body = await response.text()

            if status in (200, 202):
                logging.info("Product deleted successfully.")
                try:
                    return await response.json()
                except Exception as e:
                    logging.error("Failed to parse JSON response: %s", e)
                    return None
            else:
                logging.error(f"Failed to delete product. Status code: {status}, Reason: {response.reason}")
                logging.error("Response body: %s", body)
                return None
    except Exception as e:
        logging.error("Exception occurred while deleting product: %s", e)
        return None

async def fetchProduct_fromSalsify(wp_prod,session):
    '''
    Fetches salsify product data
    can use GTIN (default) or filter by SKU
    # returns [wp_prod, salsify_prod || None]'''
    
    logging.info("started fetchProduct_fromSalsify: %s",wp_prod)
    global org_id, salsify_headers
    
    gtin = wp_prod['acf'].get('product_gtin')
    sku = sku = wp_prod['acf'].get('product_sku') or wp_prod['acf'].get('product_baz_id')
    using_sku = False
    if(gtin):
        url = f'https://app.salsify.com/api/v1/orgs/{org_id}/products/{gtin}'
    elif(sku):
        url = f'https://app.salsify.com/api/v1/orgs/{org_id}/products/?filter=%3D%27SKU%27%3A%27{sku}%27'
        using_sku = True
    else:
        logging.error("Invalid wp_prod handled by fetchProduct_fromSalsify(): %s", wp_prod)
        return None
    try:
        async with session.get(url, headers=salsify_headers) as response:
            if response.status == 200:
                data = await response.json()
                
                if using_sku and len(data) == 2:
                    data = data.get('data')
                    logging.info("Using filter to find data...")
                    for salsify_prod in data:
                        if str(salsify_prod['salsify:id'] ).startswith("0"):
                            return [wp_prod, salsify_prod] 
                    logging.error(f"[{response.status}] Failed to fetch product with Filter (gtin/sku) : {gtin} / {sku}")
                    # if salsify placeholder prod, delete
                    if wp_prod['title'].get('rendered') == "new salsify product":
                        await deleteWordPressProduct(wp_prod['id'], session)
                    return None
                else:
                    return [wp_prod, data]
            else:
                logging.error(f"[{response.status}] Failed to fetch product (gtin/sku) : {gtin} / {sku}")
                return None
    except aiohttp.ClientError as e:
        logging.error(f"Error fetching GTIN {gtin}: {e}")
        return None

def fetchLists():
    '''
    Fetch white and blacklists for Salsify API from WordPress site
    ### returns [ 'sku_list' , 'gtin_list', 'sku_blacklist', 'gtin_blacklist' ] keyed
    
    '''
    try:
        wp_conn = http.client.HTTPSConnection(config.base_url)
        headers = {
            'Authorization': f'Basic {config.auth_token}',
        }
        wp_conn.request("GET", '/wp-json/custom/v1/salsify-lists/', headers=headers)
        response = wp_conn.getresponse()
        if response.status != 200:
            raise Exception(f"List fetch failed: {response.status}")
        return json.loads(response.read())
    except Exception as e:
        logging.error(f"Failed to fetch lists from WordPress: {e}")
        sys.exit(1)

async def main(lists = None):
    if args.single:
        wordpress_prods = await fetchProducts_fromWordpress(lists,args.single)
    else:
        wordpress_prods = await fetchProducts_fromWordpress(lists)
    logging.info("wordpress_prods: %s",wordpress_prods)
    await synchronize_with_Salsify(wordpress_prods)
    
if __name__ == "__main__":
    try:
        if args.ignore_list:
             asyncio.run(main())
        else:
            lists = fetchLists()
            asyncio.run(main(lists))
    except Exception:
        logging.error("Uncaught error in main flow:\n%s", traceback.format_exc())
    finally:
        logging.shutdown()
