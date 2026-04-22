import os
import re
import tempfile
import logging
import threading
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
# Instagram Credentials — Render Environment Variables
# Sirf ek baar set karo, cookies kabhi manually nahi badlni!
# ============================================================
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")

# ============================================================
# Telegram Bot Token — Render Environment Variable
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ============================================================
# instagrapi Client — Auto Login Manager
# ============================================================
_ig_client = None
_ig_lock = threading.Lock()

def get_ig_client():
    """
    instagrapi client return karo.
    Agar logged in nahi hai toh auto-login karo.
    Thread-safe hai.
    """
    global _ig_client
    with _ig_lock:
        if _ig_client is not None:
            return _ig_client

        if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
            logger.warning("⚠️ INSTAGRAM_USERNAME/PASSWORD not set — instagrapi unavailable")
            return None

        try:
            from instagrapi import Client
            from instagrapi.exceptions import LoginRequired, ChallengeRequired

            cl = Client()
            # Mobile app jaisi settings — detection avoid karti hain
            cl.set_locale('en_US')
            cl.set_timezone_offset(19800)  # IST +5:30

            logger.info(f"🔐 Logging into Instagram as {INSTAGRAM_USERNAME}...")
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            logger.info("✅ instagrapi login successful!")
            _ig_client = cl
            return _ig_client

        except Exception as e:
            logger.error(f"❌ instagrapi login failed: {e}")
            return None


def reset_ig_client():
    """Session expire hone par client reset karo — next call pe re-login hoga"""
    global _ig_client
    with _ig_lock:
        _ig_client = None
    logger.info("🔄 instagrapi client reset — will re-login on next request")


# ============================================================
# PRIMARY METHOD: instagrapi — Auto Login, No Cookie Headache!
# Handles: Single Image, Carousel, Video/Reel — ALL types!
# ============================================================
def fetch_via_instagrapi(ig_url):
    """
    instagrapi se media fetch karo.
    Auto-login handle karta hai — cookies manually kabhi nahi badlni!
    Supports: Single image, Carousel, Reel, Video
    """
    from instagrapi.exceptions import LoginRequired, MediaNotFound, PrivateError

    cl = get_ig_client()
    if not cl:
        return None

    try:
        # URL se media PK nikalo
        media_pk = cl.media_pk_from_url(ig_url)
        logger.info(f"📍 instagrapi media PK: {media_pk}")

        media_info = cl.media_info(media_pk)
        logger.info(f"📦 Media type: {media_info.media_type}, Product type: {media_info.product_type}")

        media_urls = []
        caption = media_info.caption_text or ''

        # Carousel (album) — multiple images/videos
        if media_info.media_type == 8:  # GraphSidecar = 8
            logger.info(f"📚 Carousel: {len(media_info.resources)} items")
            for resource in media_info.resources:
                if resource.video_url:
                    media_urls.append({'type': 'video', 'url': str(resource.video_url)})
                elif resource.thumbnail_url:
                    media_urls.append({'type': 'image', 'url': str(resource.thumbnail_url)})

        # Video / Reel
        elif media_info.media_type == 2:  # Video = 2
            if media_info.video_url:
                media_urls.append({'type': 'video', 'url': str(media_info.video_url)})

        # Single Image
        else:  # Photo = 1
            if media_info.thumbnail_url:
                media_urls.append({'type': 'image', 'url': str(media_info.thumbnail_url)})

        logger.info(f"✅ instagrapi: {len(media_urls)} media items found")
        return {'media_urls': media_urls, 'caption': caption} if media_urls else None

    except LoginRequired:
        logger.warning("🔄 Session expired — resetting and will retry on next request")
        reset_ig_client()
        # Ek baar retry
        try:
            cl2 = get_ig_client()
            if cl2:
                media_pk = cl2.media_pk_from_url(ig_url)
                media_info = cl2.media_info(media_pk)
                media_urls = []
                caption = media_info.caption_text or ''
                if media_info.media_type == 8:
                    for resource in media_info.resources:
                        if resource.video_url:
                            media_urls.append({'type': 'video', 'url': str(resource.video_url)})
                        elif resource.thumbnail_url:
                            media_urls.append({'type': 'image', 'url': str(resource.thumbnail_url)})
                elif media_info.media_type == 2:
                    if media_info.video_url:
                        media_urls.append({'type': 'video', 'url': str(media_info.video_url)})
                else:
                    if media_info.thumbnail_url:
                        media_urls.append({'type': 'image', 'url': str(media_info.thumbnail_url)})
                return {'media_urls': media_urls, 'caption': caption} if media_urls else None
        except Exception as retry_err:
            logger.error(f"❌ Retry after re-login also failed: {retry_err}")
            return None

    except PrivateError:
        logger.warning("🔒 Post is private or inaccessible")
        return None
    except MediaNotFound:
        logger.warning("🚫 Media not found")
        return None
    except Exception as e:
        logger.error(f"❌ instagrapi fetch failed: {e}")
        return None


# ============================================================
# FALLBACK METHOD: yt-dlp (for edge cases / public posts)
# ============================================================
def create_cookie_file_from_instagrapi():
    """
    instagrapi ke active session se cookies nikaalo aur
    yt-dlp ke liye Netscape format file banao.
    Manually cookies set karne ki zaroorat NAHI!
    """
    cl = get_ig_client()
    if not cl:
        return None

    try:
        # instagrapi ke session cookies use karo
        cookies = cl.cookie_dict
        if not cookies.get('sessionid'):
            return None

        cookie_content = "# Netscape HTTP Cookie File\n"
        for name, value in cookies.items():
            cookie_content += f".instagram.com\tTRUE\t/\tTRUE\t2099999999\t{name}\t{value}\n"

        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(cookie_content)
        tmp.close()
        logger.info(f"🍪 Cookie file from instagrapi session: {tmp.name}")
        return tmp.name
    except Exception as e:
        logger.error(f"❌ Cookie file creation failed: {e}")
        return None


def extract_url_from_entry(entry):
    """yt-dlp entry se best media URL nikaalo"""
    if not entry:
        return None, 'image'

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
        cookie_file = create_cookie_file_from_instagrapi()
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
# Telegram Helper Functions
# ============================================================
def tg_send_message(chat_id, text, parse_mode="HTML"):
    """Telegram mein plain text message bhejo"""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ tg_send_message failed: {e}")


def tg_send_photo(chat_id, url, caption=""):
    """Telegram mein single photo bhejo URL se"""
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendPhoto",
            json={"chat_id": chat_id, "photo": url, "caption": caption},
            timeout=20
        )
        return resp.ok
    except Exception as e:
        logger.error(f"❌ tg_send_photo failed: {e}")
        return False


def tg_send_video(chat_id, url, caption=""):
    """Telegram mein single video bhejo URL se"""
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendVideo",
            json={"chat_id": chat_id, "video": url, "caption": caption},
            timeout=30
        )
        return resp.ok
    except Exception as e:
        logger.error(f"❌ tg_send_video failed: {e}")
        return False


def tg_send_media_group(chat_id, media_items, caption=""):
    """
    Telegram mein carousel/album bhejo (max 10 items).
    caption sirf pehle item pe lagti hai.
    """
    media_group = []
    for i, item in enumerate(media_items[:10]):
        entry = {
            "type": "photo" if item['type'] == 'image' else "video",
            "media": item['url'],
        }
        if i == 0 and caption:
            entry["caption"] = caption[:1024]  # Telegram caption limit
        media_group.append(entry)

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMediaGroup",
            json={"chat_id": chat_id, "media": media_group},
            timeout=30
        )
        return resp.ok
    except Exception as e:
        logger.error(f"❌ tg_send_media_group failed: {e}")
        return False


def send_instagram_to_telegram(chat_id, ig_url):
    """
    Instagram URL se media fetch karke Telegram pe bhejo.
    Single image/video → direct send
    Carousel → media group (album)
    """
    # Shortcode extract
    sc_match = re.search(r'/(?:p|reel|tv|reels)/([^/?#&]+)', ig_url)
    if not sc_match:
        tg_send_message(chat_id, "❌ Ye valid Instagram post URL nahi lagti.\n\nFormat: <code>https://www.instagram.com/p/XXXX/</code>")
        return

    shortcode = sc_match.group(1)
    logger.info(f"📥 Telegram request | Shortcode: {shortcode}")

    # Processing message
    tg_send_message(chat_id, "⏳ Downloading...")

    # Fetch media — instagrapi primary, yt-dlp fallback
    result = fetch_via_instagrapi(ig_url)
    if not result or not result.get('media_urls'):
        result = fetch_via_ytdlp(ig_url)

    if not result or not result.get('media_urls'):
        tg_send_message(chat_id, "❌ Media nahi mila! Possible reasons:\n• Post private hai\n• Cookie expire ho gayi\n• Invalid URL")
        return

    media_urls = result['media_urls']
    caption = (result.get('caption', '') or '')[:1024]

    count = len(media_urls)
    logger.info(f"📤 Sending {count} item(s) to Telegram chat {chat_id}")

    if count == 1:
        item = media_urls[0]
        if item['type'] == 'video':
            ok = tg_send_video(chat_id, item['url'], caption)
        else:
            ok = tg_send_photo(chat_id, item['url'], caption)
        if not ok:
            tg_send_message(chat_id, "⚠️ Media send nahi hua. URL expire ho sakti hai, dobara try karo.")
    else:
        # Carousel — album as media group
        ok = tg_send_media_group(chat_id, media_urls, caption)
        if not ok:
            tg_send_message(chat_id, f"⚠️ Album send nahi hua ({count} items). Dobara try karo.")


# ============================================================
# Flask Routes — Existing (unchanged)
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

    try:
        parsed = urlparse(url_input)
        if parsed.scheme not in ('http', 'https') or 'instagram.com' not in parsed.netloc:
            return jsonify({"success": False, "error": "Only Instagram URLs allowed"}), 400
    except Exception:
        return jsonify({"success": False, "error": "Invalid URL format"}), 400

    sc_match = re.search(r'/(?:p|reel|tv|reels)/([^/?#&]+)', url_input)
    if not sc_match:
        return jsonify({"success": False, "error": "Instagram post URL format galat hai"}), 400

    shortcode = sc_match.group(1)
    logger.info(f"📥 Request: {url_input} | Shortcode: {shortcode}")

    result = None

    # Method 1: instagrapi (auto-login, no cookies needed)
    result = fetch_via_instagrapi(url_input)

    if not result or not result.get('media_urls'):
        logger.warning("⚠️ instagrapi failed — switching to yt-dlp fallback")
        result = fetch_via_ytdlp(url_input)

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


# ============================================================
# Flask Routes — Telegram Webhook (NEW)
# ============================================================
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    """
    POST /telegram
    Telegram webhook — bot messages yahan receive hote hain.
    Render pe set karo:  https://YOUR-RENDER-URL/telegram

    Supported commands:
      /start  → Welcome message + usage guide
      Instagram URL → Media download karke Telegram pe bhejo
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set")
        return jsonify({"ok": False}), 200  # 200 return karo warna Telegram retry karega

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False}), 200

    message = data.get('message') or data.get('edited_message')
    if not message:
        return jsonify({"ok": True}), 200  # callback_query etc. ignore karo

    chat_id = message.get('chat', {}).get('id')
    text = (message.get('text') or '').strip()

    if not chat_id or not text:
        return jsonify({"ok": True}), 200

    logger.info(f"💬 Telegram message from {chat_id}: {text[:80]}")

    # /start command
    if text.startswith('/start'):
        welcome = (
            "👋 <b>Assalamu Alaikum! Instagram Downloader Bot mein aapka swagat hai.</b>\n\n"
            "📥 <b>Kaise use karein:</b>\n"
            "Koi bhi Instagram post, reel ya carousel ka link yahan paste karo — "
            "main uska media directly yahan bhej dunga.\n\n"
            "✅ <b>Supported Types:</b>\n"
            "• Single Image\n"
            "• Carousel (multiple photos)\n"
            "• Reel / Video\n\n"
            "📋 <b>Format:</b>\n"
            "<code>https://www.instagram.com/p/XXXXXXX/</code>\n"
            "<code>https://www.instagram.com/reel/XXXXXXX/</code>\n\n"
            "⚠️ <b>Note:</b> Sirf public posts ka kaam karega. Private posts ke liye valid cookies zaroori hain."
        )
        tg_send_message(chat_id, welcome)
        return jsonify({"ok": True}), 200

    # Instagram URL check
    if 'instagram.com' in text:
        # URL extract karo message se (user ne extra text bhi likha ho sakta hai)
        url_match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if url_match:
            ig_url = url_match.group(0)
            send_instagram_to_telegram(chat_id, ig_url)
        else:
            tg_send_message(chat_id, "❌ Instagram URL theek se nahi mili. Poora link paste karo.")
        return jsonify({"ok": True}), 200

    # Unknown message
    tg_send_message(
        chat_id,
        "❓ Samajh nahi aaya.\n\nInstagram post/reel ka link bhejo, main media download karke dunga.\n\nExample:\n<code>https://www.instagram.com/p/XXXXXXX/</code>"
    )
    return jsonify({"ok": True}), 200


# ============================================================
# Webhook Register Helper Route (one-time use)
# ============================================================
@app.route('/setup-webhook', methods=['GET'])
def setup_webhook():
    """
    GET /setup-webhook?url=https://YOUR-RENDER-URL
    Ek baar call karo — Telegram webhook register ho jaayega.
    """
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"success": False, "error": "TELEGRAM_BOT_TOKEN not set"}), 400

    base_url = request.args.get('url', '').strip().rstrip('/')
    if not base_url:
        return jsonify({"success": False, "error": "url parameter required. Example: /setup-webhook?url=https://your-app.onrender.com"}), 400

    webhook_url = f"{base_url}/telegram"
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": webhook_url},
            timeout=10
        )
        result = resp.json()
        logger.info(f"🔗 Webhook set: {result}")
        return jsonify({"success": result.get('ok', False), "telegram_response": result, "webhook_url": webhook_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv("PORT", 10000))
    logger.info(f"🚀 Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
