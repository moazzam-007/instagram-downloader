import os
import tempfile
import logging
from urllib.parse import urlparse
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

    # Fix (Review 2): Teeno zaroori cookies check karo — sirf SESSIONID nahi
    if not all([INSTAGRAM_SESSIONID, INSTAGRAM_DS_USER_ID, INSTAGRAM_CSRFTOKEN]):
        logger.warning("⚠️ One or more Instagram cookies missing! (SESSIONID, DS_USER_ID, CSRFTOKEN)")
        return None

    # URL-encoded characters decode karo (%3A → :)
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


def extract_url_from_entry(entry):
    """
    yt-dlp entry se media URL extract karo.

    Source code analysis:
    - Video posts  → 'formats' list mein URL + quality info hoti hai
    - Image posts  → 'thumbnails' list mein URL hota hai
                     (image_versions2.candidates from Instagram private API)
                     Sabse bada thumbnail = original full-size image
    """
    if not entry:
        return None, 'image'

    # === STEP 1: VIDEO — formats list se BEST quality URL ===
    formats = entry.get('formats', [])
    if formats:
        # Fix (Review 1 BUG #2): reversed() wrong tha — best format explicitly select karo
        # tbr = total bitrate (quality indicator), height = resolution
        # max() by (tbr, height) ensures we get the actual best quality
        try:
            best_fmt = max(
                (f for f in formats if f.get('url')),
                key=lambda f: (f.get('tbr') or 0, f.get('height') or 0)
            )
            url = best_fmt.get('url', '')
            if url:
                return url, 'video'
        except ValueError:
            pass  # No valid formats with URL

    # === STEP 2: IMAGE — thumbnails se BIGGEST URL ===
    # yt-dlp extractor line 131-135: image_versions2.candidates → thumbnails
    # Biggest thumbnail (by resolution) = original full-size image
    thumbnails = entry.get('thumbnails', [])
    if thumbnails:
        sized = [
            (t.get('width', 0) * t.get('height', 0), t.get('url', ''))
            for t in thumbnails if t.get('url')
        ]
        if sized:
            sized.sort(reverse=True)
            best_url = sized[0][1]
            if best_url:
                return best_url, 'image'

    # === STEP 3: FALLBACK — direct url field ===
    url = entry.get('url', '')
    if url:
        ext = entry.get('ext', '')
        media_type = 'video' if ext in ['mp4', 'webm', 'mov', 'mkv'] else 'image'
        return url, media_type

    # === STEP 4: LAST RESORT — thumbnail field ===
    thumbnail = entry.get('thumbnail', '')
    if thumbnail:
        return thumbnail, 'image'

    return None, 'image'


# ============================================================
# Flask Routes
# ============================================================
@app.route('/')
def home():
    return "📸 Instagram Downloader v2.1 (yt-dlp + cookies) — Single, Carousel & Reels!"


@app.route('/health')
def health():
    return "OK"


@app.route('/instagram', methods=['GET'])
def get_instagram_data():
    """
    GET /instagram?url=https://www.instagram.com/p/SHORTCODE/

    Supports:
    ✅ Single image post
    ✅ Photo carousel (multiple images)
    ✅ Reel / Video
    ✅ Mixed carousel (image + video)
    """
    url_input = request.args.get('url', '').strip()

    if not url_input:
        return jsonify({"success": False, "error": "url parameter required"}), 400

    # Fix (Review 1 WARNING #2): URL validation — SSRF attack prevent karo
    # Sirf Instagram URLs allow karo
    try:
        parsed = urlparse(url_input)
        if parsed.scheme not in ('http', 'https') or 'instagram.com' not in parsed.netloc:
            return jsonify({"success": False, "error": "Only Instagram URLs allowed"}), 400
    except Exception:
        return jsonify({"success": False, "error": "Invalid URL format"}), 400

    logger.info(f"📥 Fetching: {url_input}")

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        # extract_flat: False is default — redundant line removed (Review 1 BUG #1)

        # Fix (Review 1 WARNING #3): Socket timeout — indefinite hang prevent karo
        'socket_timeout': 15,
        'extractor_retries': 2,

        # KEY FIX: Image posts pe "No video formats found!" error ignore karo
        # Source code: _extract_product_media() returns {} for image posts
        'ignore_no_formats_error': True,

        # Instagram private API App ID (source code line 53)
        'extractor_args': {
            'instagram': {'app_id': ['936619743392459']}
        },
    }

    # Fix (Review 1 BUG #3): Cookie file creation bhi try ke andar — leak prevent karo
    cookie_file = None
    try:
        cookie_file = create_cookie_file()
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            # JSON serializable output (yt-dlp docs recommendation)
            info = ydl.sanitize_info(info)

        media_urls = []
        caption = (
            info.get('description', '')
            or info.get('title', '')
            or ''
        )

        # === CAROUSEL — multiple images/videos ===
        if 'entries' in info and info['entries']:
            logger.info(f"📚 Carousel: {len(info['entries'])} items")
            for entry in info['entries']:
                if not entry:
                    continue
                url, media_type = extract_url_from_entry(entry)
                if url:
                    media_urls.append({'type': media_type, 'url': url})

        # === SINGLE POST — image ya video ===
        else:
            url, media_type = extract_url_from_entry(info)
            if url:
                media_urls.append({'type': media_type, 'url': url})

        if not media_urls:
            logger.error("❌ No media URLs found")
            return jsonify({"success": False, "error": "Koi media URL nahi mila!"}), 404

        logger.info(f"✅ {len(media_urls)} media items found")

        return jsonify({
            "success": True,
            "caption": caption,
            "media_count": len(media_urls),
            "media_urls": media_urls,
            # Fix (Review 2): first_image_url → first_media_url (video bhi ho sakta hai)
            "first_media_url": media_urls[0]['url'] if media_urls else ''
        })

    except Exception as e:
        # Fix (Review 2): logger.exception() → poora traceback logs mein aata hai
        logger.exception("❌ Error while fetching Instagram data")
        return jsonify({"success": False, "error": str(e)}), 500

    finally:
        # Cookie file cleanup — hamesha hoga (exception ho ya na ho)
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass


if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
