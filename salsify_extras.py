import logging
import http.client
import config
import json
import aiohttp
import asyncio
import re

support_formats = ["jpeg", "jpg", "png", "gif", "heic", "heif", "svg", "webp"]
gallery_keys = {
    'Product Image - Package Front Center': 'product-image',
    'Product Image - Package Left': 'product-image',
    'Product Image - Package Back Center': 'product-image',
    'Product Image - Package Right': 'product-image',
    'Product Image - Lifestyle': ''
}


# def add_GTIN_to_WP_products(wordpress_prods,salisfy_prods):
#     '''
#     Assigns GTINs to Wordpress products
#     ### returns None
    
#     '''
#     logging.info('Salsify products: %s', len(salisfy_prods))
#     logging.info('WP products: %s', len(wordpress_prods))
#     for wp_prod in wordpress_prods[:]:
#         # Ucomment to test a single post
#         # if wp_id == 4365:
#         logging.info('found target')
#         wp_sku = wp_prod['acf'].get('product_sku') if not wp_prod['acf'].get('product_baz_id') else None
#         wp_id = wp_prod.get('id')
#         logging.info('sku: %s', wp_sku)
#         for sal_prod in salisfy_prods[:]:
#             sal_sku = str(sal_prod.get('SKU'))
#             logging.info('salisfy sku: %s', sal_sku)
#             if sal_sku and sal_sku == wp_sku:
#                 logging.info('matched!')
#                 # logging.info('Found salsify product')         
#                 gtin = sal_prod.get('GTIN')
#                 logging.info('WP beofre products: %s', len(wordpress_prods))
#                 salisfy_prods.remove(sal_prod)
#                 wordpress_prods.remove(wp_prod)
#                 logging.info('WP rem products: %s', len(wordpress_prods))
#                 if gtin:
#                     wp_prod['acf']['product_gtin'] = gtin
#                     status = updateProduct(wp_id,wp_prod)
#                     logging.info('Update status: %s', status)
#                     break  # Stop checking other salsify_prods once matched
#         # logging.info('!!! Couldnt find salsify product')     
#     logging.info('Left over Salsify products: %s', len(salisfy_prods))
#     logging.info('Left over WP products: %s', len(wordpress_prods))
#     for wp_prod in wordpress_prods:
#         logging.info('Left over WP products: %s',  wp_prod['title'])

async def uploadImageToWordpress(image_data, post_id, session):
    if image_data['salsify:asset_resource_type'] != 'image':
        logging.info("Not an image? No thanks")
        return

    etag = image_data["salsify:etag"]
    check_url = f"https://{config.base_url}/wp-json/custom/v1/mediacheck/?etag={etag}"
    headers = {
        'Authorization': f'Basic {config.auth_token}',
        'Content-Type': 'application/json',
    }

    async with session.get(check_url, headers=headers) as resp:
        res_json = await resp.json()
        if res_json.get('message') == 'Already exists':
            logging.info('Image already exists: %s', res_json['media_id'])
            return res_json['media_id']

    # Upload new image
    upload_url = f"https://{config.base_url}/wp-json/custom/v1/mediacheck/"
    try:
        headers['Attach-to-post'] = str(post_id)
        async with session.post(upload_url, headers=headers, json=image_data) as upload_resp:
            data = await upload_resp.json()
            if upload_resp.status == 201:
                logging.info("Image uploaded successfully. ID: %s", data)
                return data
            else:
                logging.error("Image upload failed. %s", data)
    except Exception as e:
        logging.error('Failed to upload unique media %s', e)
        logging.error('Image Data: %s', image_data)

async def handle_image_upload(asset, post_id, css_class, session):
    media_id = await uploadImageToWordpress(asset, post_id, session)
    if isinstance(media_id, int):
        if css_class:
            return {
                "product_gallery_choice": "image",
                "product_gallery_image": media_id,
                "css_class": css_class
            }
        else: 
            return {
                "product_gallery_choice": "image",
                "product_gallery_image": media_id,
            }
    return None

async def updateProductData_gallery(post_id, data, session,site_env):
    gallery_images = []

    if 'salsify:digital_assets' not in data:
        logging.error("No digital assets; set to 'Draft' :%s", post_id)
        return False

    gallery_assets = data['salsify:digital_assets']
    tasks = []
    for image_name, image_css_class in gallery_keys.items():
        if image_name in data:
            ids = data[image_name] if isinstance(data[image_name], list) else [data[image_name]]
            for asset_id in ids:
                asset_list = [obj for obj in gallery_assets if obj.get("salsify:id") == asset_id]
                if asset_list and asset_list[0]['salsify:format'].lower() in support_formats:
                    if site_env == 'pilgrims':
                        tasks.append(handle_image_upload(asset_list[0], post_id, None, session))
                    if site_env == 'justbare':
                        tasks.append(handle_image_upload(asset_list[0], post_id, image_css_class, session))
                else:
                    logging.error('Media asset not useable: %s', asset_list)

    results = await asyncio.gather(*tasks)

    for result in results:
        if result:
            gallery_images.append(result)

    return gallery_images

def compileNutritionalData(data):
    nutrition = {}
    
    if "Number of Servings Per Package" in data: 
        nutrition['servings'] = data.get("Number of Servings Per Package")

    if 'Alternate Serving Size' in data and 'Alternate Serving Size UOM' in data:
        nutrition['servings_size'] = data['Alternate Serving Size'] + ' ' + data['Alternate Serving Size UOM'].lower() 
        if 'Serving Size' in data and 'Serving Size UOM' in data:
            nutrition['servings_size'] += " (" + data.get("Serving Size") + data.get("Serving Size UOM").lower() + ")"
    elif 'Serving Size' in data and 'Serving Size UOM' in data :
        nutrition['serving-size'] = data['Serving Size'] + ' ' + data['Serving Size UOM'].lower()

    #calories
    if 'Calories Quantity' in data:
        nutrition['calories'] = data.get('Calories Quantity')
    #fat
    if 'Total Fat Quantity' in data:
        nutrition['fat'] = data.get("Total Fat Quantity")
        if 'Total Fat Daily Value Intake %' in data:
            nutrition['fat_dv'] = data.get("Total Fat Daily Value Intake %")
    #saturated_far
    if 'Saturated Fat Quantity' in data:
        nutrition['saturated_fat'] = data.get("Saturated Fat Quantity")
        if 'Saturated Fat Daily Value Intake %' in data:
            nutrition['saturated_fat_dv'] = data.get("Saturated Fat Daily Value Intake %")
    # the rest of the nutritional fact info
    
    if 'Trans Fat Quantity' in data:
        nutrition['trans_fat'] = data.get('Trans Fat Quantity')

    # Polyunsaturated Fat
    if 'Polyunsaturated Fat Quantity' in data:
        nutrition['polyunsaturated_fat'] = data.get('Polyunsaturated Fat Quantity')

    # Monounsaturated Fat
    if 'Monounsaturated Fat Quantity' in data:
        nutrition['monounsaturated_fat'] = data.get('Monounsaturated Fat Quantity')

    # Cholesterol
    if 'Cholesterol Quantity' in data:
        nutrition['cholesterol'] = data.get('Cholesterol Quantity')
        if 'Cholesterol Daily Value Intake %' in data:
            nutrition['cholesterol_dv'] = data.get('Cholesterol Daily Value Intake %')

    # Sodium
    if 'Sodium Quantity' in data:
        nutrition['sodium'] = data.get('Sodium Quantity')
        if 'Sodium Daily Value Intake %' in data:
            nutrition['sodium_dv'] = data.get('Sodium Daily Value Intake %')

    # Carbohydrates
    if 'Total Carbohydrate Quantity' in data:
        nutrition['carbohydrate'] = data.get('Total Carbohydrate Quantity')
        if 'Total Carbohydrated Daily Value Intake %' in data:
            nutrition['carbohydrate_dv'] = data.get('Total Carbohydrated Daily Value Intake %')

    # Dietary Fiber
    if 'Dietary Fiber Quantity' in data:
        nutrition['dietary_fiber'] = data.get('Dietary Fiber Quantity')
        if 'Dietary Fiber Daily Value Intake %' in data:
            nutrition['dietary_fiber_dv'] = data.get('Dietary Fiber Daily Value Intake %')

    # Sugars
    if 'Total Sugars Quantity' in data:
        nutrition['sugars'] = data.get('Total Sugars Quantity')
    if 'Added Sugars Quantity' in data:
        nutrition['added_sugars'] = data.get('Added Sugars Quantity')
        if 'Added Sugars Daily Value Intake %' in data:
            nutrition['added_sugars_dv'] = data.get('Added Sugars Daily Value Intake %')

    # Protein
    if 'Protein Quantity' in data:
        nutrition['protein'] = data.get('Protein Quantity')
        if 'Protein Daily Value Intake %' in data:
            nutrition['protein_dv'] = data.get('Protein Daily Value Intake %')

    # Vitamins and Minerals
    vitamin_fields = {
        'Vitamin A': 'vitamin_a',
        'Vitamin C': 'vitamin_c',
        'Vitamin D': 'vitamin_d',
        'Calcium': 'calcium',
        'Iron': 'iron',
        'Potassium': 'potassium'
    }

    for key_base, field_name in vitamin_fields.items():
        quantity_key = f'{key_base} Quantity'
        dv_key = f'{key_base} Daily Value Intake %'
        if quantity_key in data:
            nutrition[field_name] = data.get(quantity_key)
        if dv_key in data:
            nutrition[f'{field_name}_dv'] = data.get(dv_key)

    return nutrition

def convert_to_ol(text):
    import re

    # Split based on the numbered steps using regex
    steps = re.split(r'\s*\d+\.\s*', text.strip())
    steps = [step.strip() for step in steps if step]  # remove empty and trim

    # Wrap each step in <li> tags and build the <ol>
    ol = "<ol>\n"
    for step in steps:
        ol += f"  <li>{step}</li>\n"
    ol += "</ol>"

    return ol

def compileCookingInstructions(data):
    cooking_instr = ""
    if 'Conventional Oven Cooking Instructions' in data:
        cooking_instr += "<strong>Conventional Oven</strong>"
        cooking_instr += convert_to_ol(data.get('Conventional Oven Cooking Instructions'))
    if 'Microwave Cooking Instructions' in data:   
        cooking_instr += "<strong>Microwave</strong>"
        cooking_instr += convert_to_ol(data.get('Microwave Cooking Instructions'))
    if 'Skillet Cooking Instructions' in data:
        cooking_instr += "<strong>Skillet</strong>"
        cooking_instr += convert_to_ol(data.get('Skillet Cooking Instructions'))
    if 'Air Fryer Cooking Instructions' in data:
        cooking_instr += "<strong>Air Fryer</strong>"
        cooking_instr += convert_to_ol(data.get('Air Fryer Cooking Instructions'))
    if 'Deep Fry Cooking Instructions' in data:
        cooking_instr += "<strong>Deep Fry</strong>"
        cooking_instr += convert_to_ol(data.get('Deep Fry Cooking Instructions'))
    if 'Gas Grill Cooking Instructions' in data:
        cooking_instr += "<strong>Gas Grill</strong>"
        cooking_instr += convert_to_ol(data.get('Gas Grill Cooking Instructions'))
    return cooking_instr

async def updateProductData(wp_prod, data, session, site_env):
    new_post = {}
    
    post_id = wp_prod.get('id')
    new_post['id'] = post_id
    
    acf = {}
    
    meta = {}
    meta["salsify_last_updated_time_stamp"] = data.get("salsify:updated_at")
    
    # Post Title
    if 'Title' in config.salsify_fields:
        if(data.get("Functional Name")):
            new_post['title'] = data.get("Functional Name").title()
        elif(data.get("Regulated Product Name")):
            new_post['title'] = data.get("Regulated Product Name").title()
        elif data.get("PRODUCT NAME"):
            new_post['title'] = data.get("PRODUCT NAME").title()
    
    # Post Copy
    if 'Copy' in config.salsify_fields:
        if(data.get("Short Description - Product Copy")):
            new_post['content'] = data.get("Short Description - Product Copy")
            new_post['excerpt'] = data.get("Short Description - Product Copy")
        elif data.get("Extended Marketing Message - Brand Copy"):
            new_post['content'] = data.get("Extended Marketing Message - Brand Copy")
            new_post['excerpt'] = data.get("Extended Marketing Message - Brand Copy")
        ### cleanup 
        if new_post.get('content') and new_post['content'].startswith('"') and new_post['content'].endswith('"'):
            new_post['content'] = new_post['content'][1:-1]
        if new_post.get('excerpt') and new_post['excerpt'].startswith('"') and new_post['excerpt'].endswith('"'):
            new_post['excerpt'] = new_post['excerpt'][1:-1]
    
    # Retail Info
    if 'Identifiers' in config.salsify_fields:
        # acf['product_baz_id'] = data['SKU']
        acf['product_sku'] = data['SKU']
        acf['product_upc'] = data['UPC']
        acf['product_gtin'] = data['GTIN']
        
    #Product Size
    if 'Size' in config.salsify_fields:
        if data.get('Inner Pack Target Weight'):
            acf["product_size"] = data.get('Inner Pack Target Weight')
        elif data.get('Net Weight'):
            acf["product_size"] = data.get('Net Weight')
        ###  cleanup
        if acf.get("product_size") and 'lb' in acf.get("product_size"):
            temp_prod_size = float(acf["product_size"].replace("lb", ""))
            acf["product_size"] = str(round(temp_prod_size * 16)) + "oz"
    
    # Benefits + Disclaimers
    if 'Benefits' in config.salsify_fields:

        benefits_list = {}
        if "Bullet 1 - Computed": 
            benefits_list.append(data.get('Bullet 1 - Computed'))
        if "Bullet 2 - Computed": 
            benefits_list.append(data.get('Bullet 2 - Computed'))
        if "Bullet 3 - Computed": 
            benefits_list.append(data.get('Bullet 3 - Computed'))
        if "Bullet 4 - Computed": 
            benefits_list.append(data.get('Bullet 4 - Computed'))
        if "Bullet 5 - Computed": 
            benefits_list.append(data.get('Bullet 5 - Computed'))
        #Fallback
        if len(benefits_list) == 0 and data.get('Feature Benefits'):
            benefits_list = data.get('Feature Benefits').split(";")

        html_output = "<ul>\n"
        for benefit in benefits_list:
            html_output += f"  <li>{benefit}</li>\n"
        html_output += "</ul>"
        temp_callouts = html_output
        
        ### cleanup 
        temp_disclaimers = "<ul>"
        if "antibiotics" in temp_callouts.lower():
            temp_callouts = re.sub(r'\bAntibiotics\b', 'Antibiotics*', temp_callouts, flags=re.IGNORECASE)
            temp_disclaimers += "<li>* Chicken used is raised with no antibiotics ever.</li>"

        if "steroids" in temp_callouts.lower():
            temp_callouts = re.sub(r'\bSteroids\b', 'Steroids†', temp_callouts, flags=re.IGNORECASE)
            temp_disclaimers += "<li>† Federal regulations prohibit the use of hormones or steroids in poultry.</li>"

        if "family farms" in temp_callouts.lower():
            temp_callouts = re.sub(r'\bfamily farms\b', 'Family Farms‡', temp_callouts, flags=re.IGNORECASE)
            temp_disclaimers += "<li>‡ “Family farm or ranch” is any farm or ranch organized as a sole proprietorship, partnership, or family corporation where the majority of the business is owned and controlled by the person and his or her relatives.</li>"

        if temp_disclaimers:
            temp_disclaimers = "<ul>" + temp_disclaimers + "</ul>"
    
        acf['callouts'] = temp_callouts
        acf['disclaimers'] = temp_disclaimers
        
    # Ingredients
    if 'Ingredients' in config.salsify_fields:
        if data.get("Ingredient Statement"):
            acf["product_ingredients"] = data.get("Ingredient Statement")
      
    # Cooking Instructions
    if 'Instructions' in config.salsify_fields:
        acf['product_cooking-instructions'] = compileCookingInstructions(data)
        
    # Gallery  
    if 'Gallery' in config.salsify_fields:
        acf['product_gallery'] = await updateProductData_gallery(post_id,data,session,site_env)
        if(acf['product_gallery']):
            new_post['featured_media'] = acf['product_gallery'][0].get("product_gallery_image")
        else:
            new_post['post_status'] = 'draft'
    
    # Nutrition
    if 'Nutrition' in config.salsify_fields:
        acf['nutritional_label'] = compileNutritionalData(data)
    
    if 'Taxonomy' in config.salsify_fields:
        if 'Consumer Storage Instructions' in data:
            if "refrigerated" in data.get("Consumer Storage Instructions").lower():
                new_post['protein_type'] = [ 8 ] 
                # Frozen
                # HARD CODED TERM ID
            else: 
                new_post['protein_type'] = [ 9 ]
                # Fresh
                # HARD CODED TERM ID
    
    new_post['acf'] = acf
    new_post['meta'] = meta
    return new_post
    
    # will call more functions later to hydrate new_post, acf, and meta