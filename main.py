import os
import re
import tempfile
import logging
import requests
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

# Instagram Base64 encoding table (same as yt-dlp source)
_ENCODING_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'


# ============================================================
# Helper: Shortcode → Numeric PK (same logic as yt-dlp source)
# ============================================================
def shortcode_to_pk(shortcode):
    """Instagram shortcode ko numeric media PK mein convert karo"""
    # Handle long shortcodes (newer Instagram format appends extra chars)
    if len(shortcode) > 11:
        shortcode = shortcode[:11]
    table = {char: idx for idx, char in enumerate(_ENCODING_CHARS)}
    result = 0
    for char in shortcode:
        if char not in table:
            raise ValueError(f"Invalid character in shortcode: {char!r}")
        result = result * 64 + table[char]
    return result


# ============================================================
# PRIMARY METHOD: Direct Instagram Private API
# Handles: Single Image, Carousel, Video/Reel — ALL types!
# ============================================================
def extract_media_from_api_item(media_item):
    """Instagram API media item se URL extract karo (image ya video)"""

    # === VIDEO: video_versions se best quality ===
    video_versions = media_item.get('video_versions', [])
    if video_versions:
        best = max(
            (v for v in video_versions if v.get('url')),
            key=lambda v: (v.get('width', 0) * v.get('height', 0)),
            default=None
        )
        if best:
            return best['url'], 'video'

    # === IMAGE: image_versions2.candidates se original (biggest) ===
    candidates = media_item.get('image_versions2', {}).get('candidates', [])
    if candidates:
        best = max(
            (c for c in candidates if c.get('url')),
            key=lambda c: (c.get('width', 0) * c.get('height', 0)),
            default=None
        )
        if best:
            return best['url'], 'image'

    return None, 'image'


def fetch_via_instagram_api(shortcode):
    """
    Instagram ke private API se media URLs fetch karo.

    Endpoint: https://i.instagram.com/api/v1/media/{PK}/info/
    Uses: sessionid + csrftoken cookies for authentication
    Supports: Single image, Photo carousel, Reel, Video
    """
    if not all([INSTAGRAM_SESSIONID, INSTAGRAM_DS_USER_ID, INSTAGRAM_CSRFTOKEN]):
        logger.warning("⚠️ Instagram cookies missing — direct API unavailable")
        return None

    try:
        pk = shortcode_to_pk(shortcode)
        logger.info(f"📍 Shortcode '{shortcode}' → PK: {pk}")
    except ValueError as e:
        logger.error(f"❌ Shortcode conversion failed: {e}")
        return None

    session_id = INSTAGRAM_SESSIONID.replace('%3A', ':')

    headers = {
        'X-IG-App-ID': '936619743392459',
        'X-ASBD-ID': '198387',
        'X-IG-WWW-Claim': '0',
        'Origin': 'https://www.instagram.com',
        'Referer': f'https://www.instagram.com/p/{shortcode}/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
    }
    cookies = {
        'sessionid': session_id,
        'csrftoken': INSTAGRAM_CSRFTOKEN,
        'ds_user_id': INSTAGRAM_DS_USER_ID,
    }

    try:
        resp = requests.get(
            f'https://i.instagram.com/api/v1/media/{pk}/info/',
            headers=headers,
            cookies=cookies,
            timeout=15
        )
        logger.info(f"📡 Instagram API response: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ API HTTP error: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ API request failed: {e}")
        return None

    items = data.get('items', [])
    if not items:
        logger.error("❌ API returned no items")
        return None

    item = items[0]
    media_urls = []

    # Caption extract karo
    caption_obj = item.get('caption') or {}
    caption = caption_obj.get('text', '') if isinstance(caption_obj, dict) else ''

    # === CAROUSEL check ===
    carousel_media = item.get('carousel_media', [])
    if carousel_media:
        logger.info(f"📚 Carousel: {len(carousel_media)} items")
        for media in carousel_media:
            url, media_type = extract_media_from_api_item(media)
            if url:
                media_urls.append({'type': media_type, 'url': url})
    else:
        # === Single image ya video ===
        url, media_type = extract_media_from_api_item(item)
        if url:
            media_urls.append({'type': media_type, 'url': url})

    logger.info(f"✅ Direct API: {len(media_urls)} media items found")
    return {'media_urls': media_urls, 'caption': caption}


# ============================================================
# FALLBACK METHOD: yt-dlp (for edge cases)
# ============================================================
def create_cookie_file():
    """Netscape format cookie file create karo yt-dlp ke liye"""
    if not all([INSTAGRAM_SESSIONID, INSTAGRAM_DS_USER_ID, INSTAGRAM_CSRFTOKEN]):
        return None

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
    logger.info(f"🍪 Cookie file: {tmp.name}")
    return tmp.name


def extract_url_from_entry(entry):
    """yt-dlp entry se best media URL nikaalo"""
    if not entry:
        return None, 'image'

    # Video: formats se best quality
    formats = entry.get('formats', [])
    if formats:
        try:
            best_fmt = max(
                (f for f in formats if f.get('url')),
                key=lambda f: (f.get('tbr') or 0, f.get('height') or 0)
            )
            url = best_fmt.get('url', '')
            if url:
                return url, 'video'
        except ValueError:
            pass

    # Image: thumbnails se biggest (= original)
    thumbnails = entry.get('thumbnails', [])
    if thumbnails:
        sized = [
            (t.get('width', 0) * t.get('height', 0), t.get('url', ''))
            for t in thumbnails if t.get('url')
        ]
        if sized:
            sized.sort(reverse=True)
            if sized[0][1]:
                return sized[0][1], 'image'

    # Fallback: direct url
    url = entry.get('url', '')
    if url:
        ext = entry.get('ext', '')
        media_type = 'video' if ext in ['mp4', 'webm', 'mov', 'mkv'] else 'image'
        return url, media_type

    thumbnail = entry.get('thumbnail', '')
    if thumbnail:
        return thumbnail, 'image'

    return None, 'image'


def fetch_via_ytdlp(url_input):
    """yt-dlp se media fetch karo (fallback method)"""
    logger.info("🔄 Trying yt-dlp fallback...")

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'socket_timeout': 15,
        'extractor_retries': 1,
        'ignore_no_formats_error': True,
        'extractor_args': {
            'instagram': {'app_id': ['936619743392459']}
        },
    }

    cookie_file = None
    try:
        cookie_file = create_cookie_file()
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            info = ydl.sanitize_info(info)

        media_urls = []
        caption = info.get('description', '') or info.get('title', '') or ''

        if 'entries' in info and info['entries']:
            for entry in info['entries']:
                url, media_type = extract_url_from_entry(entry)
                if url:
                    media_urls.append({'type': media_type, 'url': url})
        else:
            url, media_type = extract_url_from_entry(info)
            if url:
                media_urls.append({'type': media_type, 'url': url})

        if media_urls:
            logger.info(f"✅ yt-dlp fallback: {len(media_urls)} items found")
            return {'media_urls': media_urls, 'caption': caption}

    except Exception as e:
        logger.exception(f"❌ yt-dlp fallback also failed")
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except Exception:
                pass

    return None


# ============================================================
# Flask Routes
# ============================================================
@app.route('/')
def home():
    return "📸 Instagram Downloader v3.0 — Direct API + yt-dlp fallback. All types supported!"


@app.route('/health')
def health():
    return "OK"


@app.route('/instagram', methods=['GET'])
def get_instagram_data():
    """
    GET /instagram?url=https://www.instagram.com/p/SHORTCODE/

    Supports ALL Instagram post types:
    ✅ Single image
    ✅ Photo carousel (multiple images)
    ✅ Reel / Video
    ✅ Mixed carousel (image + video)

    Method 1 (Primary):  Direct Instagram Private API — fast & reliable
    Method 2 (Fallback): yt-dlp — handles edge cases
    """
    url_input = request.args.get('url', '').strip()

    if not url_input:
        return jsonify({"success": False, "error": "url parameter required"}), 400

    # URL validation — SSRF prevention
    try:
        parsed = urlparse(url_input)
        if parsed.scheme not in ('http', 'https') or 'instagram.com' not in parsed.netloc:
            return jsonify({"success": False, "error": "Only Instagram URLs allowed"}), 400
    except Exception:
        return jsonify({"success": False, "error": "Invalid URL format"}), 400

    # Shortcode extract karo URL se
    sc_match = re.search(r'/(?:p|reel|tv|reels)/([^/?#&]+)', url_input)
    if not sc_match:
        return jsonify({"success": False, "error": "Instagram post URL format galat hai"}), 400

    shortcode = sc_match.group(1)
    logger.info(f"📥 Request: {url_input} | Shortcode: {shortcode}")

    result = None

    # ── Method 1: Direct Instagram Private API ──────────────────────
    result = fetch_via_instagram_api(shortcode)

    # ── Method 2: yt-dlp fallback (agar direct API fail ho) ─────────
    if not result or not result.get('media_urls'):
        logger.warning("⚠️ Direct API failed — switching to yt-dlp fallback")
        result = fetch_via_ytdlp(url_input)

    # ── Both methods failed ──────────────────────────────────────────
    if not result or not result.get('media_urls'):
        return jsonify({
            "success": False,
            "error": "Koi media URL nahi mila! Cookie expired ho sakti hai ya post private hai."
        }), 404

    media_urls = result['media_urls']
    caption = result.get('caption', '')

    return jsonify({
        "success": True,
        "caption": caption,
        "media_count": len(media_urls),
        "media_urls": media_urls,
        "first_media_url": media_urls[0]['url'] if media_urls else ''
    })


if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
