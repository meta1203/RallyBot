import os
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from events import MeetupEvent
from aws import upload_to_s3

def wrap_text(text, font, max_width):
	words = text.split()
	final_lines = []
	current_line = ""

	for word in words:
		# Test if word fits on current line (including a space if not first word)
		test_line = f"{current_line} {word}".strip() if current_line else word
		if font.getlength(test_line) <= max_width:
			current_line = test_line
		else:
			# Line is full, save current and start new with word
			if current_line:
				final_lines.append(current_line)
			current_line = word
	
	if current_line:
		final_lines.append(current_line)
		
	return final_lines

def create_instagram_image(event: MeetupEvent) -> Image.Image:
	# Canvas size 9:16
	width = 1080
	height = 1920
	# Use RGBA to support transparency during the transition blend
	canvas = Image.new('RGBA', (width, height), color=(0, 0, 0, 255))
	
	# Load event image
	img = event.image
	if img is None:
		img = Image.new('RGB', (width, width), color=(40, 40, 40))
	
	# Resize image to fit width
	img_w, img_h = img.size
	aspect_ratio = img_h / img_w
	new_h = int(width * aspect_ratio)
	img = img.resize((width, new_h), Image.Resampling.LANCZOS).convert('RGBA')
	
	# Paste image at top
	canvas.paste(img, (0, 0))
	
	# Create a blur transition
	blur_radius = 50
	transition_height = 300
	
	# Crop a slice of the image for blurring
	crop_top = max(0, new_h - transition_height)
	img_slice = img.crop((0, crop_top, width, new_h))
	blurred_slice = img_slice.filter(ImageFilter.GaussianBlur(blur_radius))
	
	# Create a gradient mask to fade from original -> blurred -> black
	mask = Image.new('L', (width, transition_height), 0)
	for y in range(transition_height):
		alpha = int((y / transition_height) * 255)
		for x in range(width):
			mask.putpixel((x, y), alpha)
	
	# Paste the blurred slice using the mask
	canvas.paste(blurred_slice, (0, crop_top), mask)
	
	# Add a black gradient fade over the blur to transition to solid black
	fade_mask = Image.new('L', (width, transition_height), 0)
	for y in range(transition_height):
		alpha = int((y / transition_height) * 255)
		for x in range(width):
			fade_mask.putpixel((x, y), alpha)
	
	black_overlay = Image.new('RGBA', (width, transition_height), (0, 0, 0, 255))
	canvas.paste(black_overlay, (0, crop_top), fade_mask)
	
	# Convert back to RGB for final output
	canvas = canvas.convert('RGB')
	draw = ImageDraw.Draw(canvas)
	
	# Text Settings
	try:
		font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 60)
		font_text = ImageFont.truetype("DejaVuSans.ttf", 40)
	except IOError:
		font_title = ImageFont.load_default()
		font_text = ImageFont.load_default()

	# 1. Draw Title (Wrapped)
	y_offset = new_h + 10
	title_text = event.title if event.title else "Untitled Event"
	wrapped_title = wrap_text(title_text, font_title, width - 100)
	for line in wrapped_title:
		line_w = draw.textlength(line, font=font_title)
		draw.text(((width - line_w) // 2, y_offset), line, font=font_title, fill=(255, 255, 255))
		y_offset += 65
	
	# 2. Separator Line
	y_offset += 20
	draw.line((round(width * 0.2), y_offset, round(width * 0.8), y_offset), (255, 255, 255), 2)
	y_offset += 20

	# 3. Date and Time
	date_text = event.start_time.strftime("%B %d, %Y | %I:%M %p") if event.start_time else "No date set"
	line_w = draw.textlength(date_text, font=font_text)
	draw.text(((width - line_w) // 2, y_offset), date_text, font=font_text, fill=(255, 255, 255))
	y_offset += 50

	# 4. Location
	location_text = event.location if not event.online else "Online - Discord"
	if " | " in location_text:
		l = location_text.split(" | ")
		# Priority to venue name if available (usually second part of the string)
		line_w = draw.textlength(l[1], font=font_text)
		draw.text(((width - line_w) // 2, y_offset), l[1], font=font_text, fill=(255, 255, 255))
		y_offset += 45
		line_w = draw.textlength(l[0], font=font_text)
		draw.text(((width - line_w) // 2, y_offset), l[0], font=font_text, fill=(255, 255, 255))
	else:
		line_w = draw.textlength(location_text, font=font_text)
		draw.text(((width - line_w) // 2, y_offset), location_text, font=font_text, fill=(255, 255, 255))
	y_offset += 60

	# 5. Separator Line
	draw.line((round(width * 0.2), y_offset, round(width * 0.8), y_offset), (255, 255, 255), 2)
	y_offset += 20

	# 6. Description (Wrapped)
	description_text = event.description if event.description else ""
	wrapped_lines = wrap_text(description_text, font_text, width - 100)
	
	for line in wrapped_lines:
		line_w = draw.textlength(line, font=font_text)
		draw.text(((width - line_w) // 2, y_offset), line, font=font_text, fill=(220, 220, 220))
		y_offset += 45
		if y_offset > height - 100:
			break
			
	return canvas

def upload_event_to_instagram(event: MeetupEvent):
	account_id = os.getenv('INSTAGRAM_BUSINESS_ACCOUNT_ID')
	access_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
	
	if not account_id or not access_token:
		print("ERROR: INSTAGRAM_BUSINESS_ACCOUNT_ID or INSTAGRAM_ACCESS_TOKEN not set.")
		return False
	
	try:
		# 1. Create the image
		img = create_instagram_image(event)
		
		# 2. Upload to S3 to get a public URL
		filename = f"insta_events/{event.sort}.webp"
		image_url = upload_to_s3(img, filename)
		
		# 3. Create Media Container
		# POST /{ig-user-id}/media
		container_url = f"https://graph.facebook.com/v18.0/{account_id}/media"
		payload = {
			'image_url': image_url,
			'caption': f"{event.title}\n\n{event.description}",
			'access_token': access_token
		}
		
		res = requests.post(container_url, data=payload)
		res.raise_for_status()
		creation_id = res.json().get('id')
		
		if not creation_id:
			print("ERROR: Failed to get creation_id from Instagram API")
			return False
			
		# 4. Publish the Media Container
		# POST /{ig-user-id}/media_publish
		publish_url = f"https://graph.facebook.com/v18.0/{account_id}/media_publish"
		publish_payload = {
			'creation_id': creation_id,
			'access_token': access_token
		}
		
		res = requests.post(publish_url, data=publish_payload)
		res.raise_for_status()
		
		print(f"Successfully published event {event.title} to Instagram Business account.")
		return True
		
	except Exception as e:
		print(f"Error uploading to Instagram Business API: {e}")
		return False

if __name__ == "__main__":
	import datetime
	from shared import shared

	for e in MeetupEvent.scan(
		index_name="timestamp-index",
		filter_condition=MeetupEvent.timestamp > int(datetime.datetime.now(shared.est).timestamp() * 1000),
		limit=1
	):
		if not e.image:
			print(f"Image value for {e.title} does not have an image attached, refetching...")
			import events
			events.check_existing_event(e)
		img = create_instagram_image(e)
		img.show(e.title)