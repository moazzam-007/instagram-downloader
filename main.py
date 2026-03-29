import os
import re
import logging
from flask import Flask, request, jsonify
import instaloader

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Instaloader (no login for public posts)
L = instaloader.Instaloader(
    download_pictures=False,
    download_videos=False,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    quiet=True
)

# Optional: Login with Instagram account for better reliability
# (Set env vars INSTAGRAM_USER and INSTAGRAM_PASS on Render)
INSTA_USER = os.getenv("INSTAGRAM_USER", "")
INSTA_PASS = os.getenv("INSTAGRAM_PASS", "")

if INSTA_USER and INSTA_PASS:
    try:
        L.login(INSTA_USER, INSTA_PASS)
        logger.info(f"Logged in as: {INSTA_USER}")
    except Exception as e:
        logger.warning(f"Login failed, using guest mode: {e}")


def extract_shortcode(url_or_code):
    """Extract shortcode from Instagram URL or return as-is"""
    # Match /p/SHORTCODE or /reel/SHORTCODE or /reels/SHORTCODE
    match = re.search(r'/(?:p|reel|reels)/([A-Za-z0-9_-]+)', url_or_code)
    if match:
        return match.group(1)
    # If no URL pattern, assume it's already a shortcode
    if re.match(r'^[A-Za-z0-9_-]+$', url_or_code):
        return url_or_code
    return None


@app.route('/')
def home():
    return "📸 Instagram Downloader API is running!"


@app.route('/health')
def health():
    return "OK"


@app.route('/instagram', methods=['GET'])
def get_instagram_data():
    """
    GET /instagram?url=https://www.instagram.com/p/SHORTCODE/
    OR
    GET /instagram?url=SHORTCODE
    
    Returns JSON with image URLs and caption
    """
    url_input = request.args.get('url', '').strip()

    if not url_input:
        return jsonify({"success": False, "error": "url parameter required"}), 400

    shortcode = extract_shortcode(url_input)
    if not shortcode:
        return jsonify({"success": False, "error": f"Invalid URL or shortcode: {url_input}"}), 400

    logger.info(f"Fetching Instagram post: {shortcode}")

    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)

        media_urls = []

        if post.typename == 'GraphSidecar':
            # Carousel - multiple images/videos
            for node in post.get_sidecar_nodes():
                if node.is_video:
                    media_urls.append({
                        "type": "video",
                        "url": node.video_url,
                        "thumbnail": node.display_url
                    })
                else:
                    media_urls.append({
                        "type": "image",
                        "url": node.display_url
                    })
        elif post.is_video:
            media_urls.append({
                "type": "video",
                "url": post.video_url,
                "thumbnail": post.url
            })
        else:
            media_urls.append({
                "type": "image",
                "url": post.url
            })

        result = {
            "success": True,
            "shortcode": shortcode,
            "caption": post.caption or "",
            "post_type": "reel" if post.is_video else ("carousel" if post.typename == 'GraphSidecar' else "post"),
            "owner_username": post.owner_username,
            "owner_fullname": post.owner_profile.full_name if post.owner_profile else "",
            "media_count": len(media_urls),
            "media_urls": media_urls,
            # First image URL directly (for easy access in n8n)
            "first_image_url": media_urls[0]["url"] if media_urls else "",
            "first_thumbnail_url": media_urls[0].get("thumbnail", media_urls[0]["url"]) if media_urls else ""
        }

        logger.info(f"Success: {shortcode} — {len(media_urls)} media items")
        return jsonify(result)

    except instaloader.exceptions.InstaloaderException as e:
        logger.error(f"Instaloader error for {shortcode}: {e}")
        return jsonify({"success": False, "error": f"Instagram fetch failed: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Unexpected error for {shortcode}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    logger.info(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
