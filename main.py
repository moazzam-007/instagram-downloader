import os
import tempfile
import logging
from flask import Flask, request, jsonify
import yt_dlp

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============================================================
# Instagram Cookies — Render Environment Variables se aate hain
# ============================================================
INSTAGRAM_SESSIONID  = os.getenv("INSTAGRAM_SESSIONID", "")
INSTAGRAM_CSRFTOKEN  = os.getenv("INSTAGRAM_CSRFTOKEN", "")
INSTAGRAM_DS_USER_ID = os.getenv("INSTAGRAM_DS_USER_ID", "")


def create_cookie_file():
    """Netscape format cookie file create karo yt-dlp ke liye"""
    if not INSTAGRAM_SESSIONID:
        logger.warning("⚠️ INSTAGRAM_SESSIONID env var missing!")
        return None

    # URL-encoded characters decode karo
    sessionid = INSTAGRAM_SESSIONID.replace('%3A', ':')

    cookie_content = (
        "# Netscape HTTP Cookie File\n"
        f".instagram.com\tTRUE\t/\tTRUE\t2099999999\tds_user_id\t{INSTAGRAM_DS_USER_ID}\n"
        f".instagram.com\tTRUE\t/\tTRUE\t2099999999\tcsrftoken\t{INSTAGRAM_CSRFTOKEN}\n"
        f".instagram.com\tTRUE\t/\tTRUE\t2099999999\tsessionid\t{sessionid}\n"
    )

    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
    tmp.write(cookie_content)
    tmp.close()
    logger.info(f"✅ Cookie file ready: {tmp.name}")
    return tmp.name


# ============================================================
# Flask Routes
# ============================================================
@app.route('/')
def home():
    return "📸 Instagram Downloader (yt-dlp + cookies) is running!"


@app.route('/health')
def health():
    return "OK"


@app.route('/instagram', methods=['GET'])
def get_instagram_data():
    """
    GET /instagram?url=https://www.instagram.com/p/SHORTCODE/
    Returns all images/videos from the post (carousel support included!)
    """
    url_input = request.args.get('url', '').strip()

    if not url_input:
        return jsonify({"success": False, "error": "url parameter required"}), 400

    logger.info(f"📥 Fetching: {url_input}")
    cookie_file = create_cookie_file()

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)

        media_urls = []
        caption = info.get('description', '') or info.get('title', '') or ''

        if 'entries' in info and info['entries']:
            # ✅ Carousel — multiple images/videos
            for entry in info['entries']:
                if not entry:
                    continue
                url = entry.get('url', '')
                ext = entry.get('ext', '')
                if url:
                    media_urls.append({
                        'type': 'video' if ext in ['mp4', 'webm', 'mov'] else 'image',
                        'url': url
                    })
        else:
            # ✅ Single image or video
            url = info.get('url', '')
            ext = info.get('ext', '')
            if url:
                media_urls.append({
                    'type': 'video' if ext in ['mp4', 'webm', 'mov'] else 'image',
                    'url': url
                })

        if not media_urls:
            return jsonify({"success": False, "error": "Koi media nahi mila!"}), 404

        logger.info(f"✅ {len(media_urls)} media items found")

        return jsonify({
            "success": True,
            "caption": caption,
            "media_count": len(media_urls),
            "media_urls": media_urls,
            "first_image_url": media_urls[0]['url']
        })

    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass


if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
