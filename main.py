import os
import re
import json as json_module
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

# ============================================================
# Telegram Bot Token — Render Environment Variable
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ============================================================
# Render API — Cookie Auto-Update ke liye
# ============================================================
RENDER_API_KEY       = os.getenv("RENDER_API_KEY", "")
RENDER_SERVICE_ID    = os.getenv("RENDER_SERVICE_ID", "")
COOKIE_UPDATE_SECRET = os.getenv("COOKIE_UPDATE_SECRET", "")

# Comma-separated chat IDs allowed to run /updatecookie command.
TELEGRAM_ADMIN_CHAT_IDS = {
    chat_id.strip()
    for chat_id in os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").split(",")
    if chat_id.strip()
}

PENDING_COOKIE_UPDATE_CHATS = set()

# Instagram Base64 encoding table (same as yt-dlp source)
_ENCODING_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

REQUIRED_COOKIE_ENV_KEYS = (
    "INSTAGRAM_SESSIONID",
    "INSTAGRAM_CSRFTOKEN",
    "INSTAGRAM_DS_USER_ID",
)


# ============================================================
# Helper: Shortcode → Numeric PK (same logic as yt-dlp source)
# ============================================================
def shortcode_to_pk(shortcode):
    """Instagram shortcode ko numeric media PK mein convert karo"""
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
# ============================================================
def extract_media_from_api_item(media_item):
    """Instagram API media item se URL extract karo (image ya video)"""
    video_versions = media_item.get('video_versions', [])
    if video_versions:
        best = max(
            (v for v in video_versions if v.get('url')),
            key=lambda v: (v.get('width', 0) * v.get('height', 0)),
            default=None
        )
        if best:
            return best['url'], 'video'

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

    caption_obj = item.get('caption') or {}
    caption = caption_obj.get('text', '') if isinstance(caption_obj, dict) else ''

    carousel_media = item.get('carousel_media', [])
    if carousel_media:
        logger.info(f"📚 Carousel: {len(carousel_media)} items")
        for media in carousel_media:
            url, media_type = extract_media_from_api_item(media)
            if url:
                media_urls.append({'type': media_type, 'url': url})
    else:
        url, media_type = extract_media_from_api_item(item)
        if url:
            media_urls.append({'type': media_type, 'url': url})

    logger.info(f"✅ Direct API: {len(media_urls)} media items found")
    return {'media_urls': media_urls, 'caption': caption}


# ============================================================
# FALLBACK METHOD: yt-dlp
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
# Cookie Update Helpers (Render + Telegram)
# ============================================================
def extract_cookie_env_values(cookies_raw):
    """Cookie JSON array se required 3 env values nikaalo."""
    if not isinstance(cookies_raw, list):
        return None, "JSON array expected"

    extracted = {}
    for item in cookies_raw:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        if not value:
            continue

        if name == "sessionid":
            extracted["INSTAGRAM_SESSIONID"] = value
        elif name == "csrftoken":
            extracted["INSTAGRAM_CSRFTOKEN"] = value
        elif name == "ds_user_id":
            extracted["INSTAGRAM_DS_USER_ID"] = value

    missing = [key for key in REQUIRED_COOKIE_ENV_KEYS if key not in extracted]
    if missing:
        return None, f"Ye cookies JSON mein nahi mili: {missing}"

    return extracted, None


def update_render_cookie_env(cookies_raw):
    """Render env vars update karke service redeploy trigger karo."""
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return {
            "success": False,
            "error": "RENDER_API_KEY ya RENDER_SERVICE_ID env mein missing hai"
        }, 500

    extracted, error = extract_cookie_env_values(cookies_raw)
    if error:
        return {"success": False, "error": error}, 400

    logger.info(f"🍪 Cookies extracted: {list(extracted.keys())}")

    render_headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        env_resp = requests.get(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers=render_headers,
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ Render env fetch request failed: {e}")
        return {"success": False, "error": f"Render env fetch request failed: {e}"}, 500

    if not env_resp.ok:
        logger.error(f"❌ Render env fetch failed: {env_resp.status_code} {env_resp.text}")
        return {"success": False, "error": f"Render env fetch failed: {env_resp.status_code}"}, 500

    try:
        existing_env = env_resp.json()
    except Exception:
        return {"success": False, "error": "Render env response parse failed"}, 500

    if not isinstance(existing_env, list):
        return {"success": False, "error": "Unexpected Render env response format"}, 500

    updated_keys = []
    for item in existing_env:
        key = item.get("key")
        if key in extracted:
            item["value"] = extracted[key]
            updated_keys.append(key)

    for key, value in extracted.items():
        if key not in updated_keys:
            existing_env.append({"key": key, "value": value})
            updated_keys.append(key)

    try:
        put_resp = requests.put(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
            headers=render_headers,
            json=existing_env,
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ Render env update request failed: {e}")
        return {"success": False, "error": f"Render env update request failed: {e}"}, 500

    if not put_resp.ok:
        logger.error(f"❌ Render env update failed: {put_resp.status_code} {put_resp.text}")
        return {"success": False, "error": f"Render env update failed: {put_resp.status_code}"}, 500

    logger.info(f"✅ Env vars updated: {updated_keys}")

    try:
        deploy_resp = requests.post(
            f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys",
            headers=render_headers,
            json={"clearCache": "do_not_clear"},
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ Deploy trigger request failed: {e}")
        return {
            "success": False,
            "error": f"Env update ho gayi, lekin deploy request fail hua: {e}",
            "updated_keys": updated_keys,
            "deploy_triggered": False
        }, 502

    if not deploy_resp.ok:
        logger.error(f"❌ Deploy trigger failed: {deploy_resp.status_code} {deploy_resp.text}")
        return {
            "success": False,
            "error": f"Env update ho gayi, lekin deploy trigger fail hua: {deploy_resp.status_code}",
            "updated_keys": updated_keys,
            "deploy_triggered": False
        }, 502

    logger.info(f"🚀 Redeploy triggered | Status: {deploy_resp.status_code}")
    return {
        "success": True,
        "message": "✅ Cookies update ho gayi! Service redeploying... ~1-2 min mein ready ho jayega.",
        "updated_keys": updated_keys,
        "deploy_triggered": True
    }, 200


def parse_cookie_json_payload(raw_text):
    """Telegram text payload se JSON parse karo (code fences supported)."""
    payload = (raw_text or "").strip()
    if not payload:
        raise ValueError("Cookie JSON empty hai")

    if payload.startswith("```"):
        payload = re.sub(r'^```(?:json)?\s*', '', payload, flags=re.IGNORECASE)
        payload = re.sub(r'\s*```$', '', payload)

    return json_module.loads(payload)


def is_cookie_update_authorized(chat_id):
    """Sirf allowlisted chat IDs ko cookie update allow karo."""
    if not TELEGRAM_ADMIN_CHAT_IDS:
        return False
    return str(chat_id) in TELEGRAM_ADMIN_CHAT_IDS


# ============================================================
# Telegram Helper Functions
# ============================================================
def tg_send_message(chat_id, text, parse_mode="HTML"):
    try:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
            timeout=10
        )
    except Exception as e:
        logger.error(f"❌ tg_send_message failed: {e}")


def process_cookie_update_from_telegram(chat_id, raw_payload):
    """Telegram command payload se cookies update + redeploy trigger karo."""
    try:
        cookies_raw = parse_cookie_json_payload(raw_payload)
    except Exception as e:
        tg_send_message(
            chat_id,
            f"❌ Invalid JSON: {e}\n\nDobara poora cookie JSON bhejo, ya /cancel likho.",
            parse_mode=None
        )
        return False

    result, status_code = update_render_cookie_env(cookies_raw)
    if status_code == 200:
        tg_send_message(
            chat_id,
            "✅ Cookies update ho gayi.\n🚀 Redeploy trigger ho gaya hai. 1-2 min baad bot fresh cookies par aa jayega.",
            parse_mode=None
        )
        return True

    tg_send_message(
        chat_id,
        f"❌ Cookie update failed ({status_code}): {result.get('error', 'Unknown error')}",
        parse_mode=None
    )
    return False


def tg_send_photo(chat_id, url, caption=""):
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
    media_group = []
    for i, item in enumerate(media_items[:10]):
        entry = {
            "type": "photo" if item['type'] == 'image' else "video",
            "media": item['url'],
        }
        if i == 0 and caption:
            entry["caption"] = caption[:1024]
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
    sc_match = re.search(r'/(?:p|reel|tv|reels)/([^/?#&]+)', ig_url)
    if not sc_match:
        tg_send_message(chat_id, "❌ Ye valid Instagram post URL nahi lagti.\n\nFormat: <code>https://www.instagram.com/p/XXXX/</code>")
        return

    shortcode = sc_match.group(1)
    logger.info(f"📥 Telegram request | Shortcode: {shortcode}")
    tg_send_message(chat_id, "⏳ Downloading...")

    result = fetch_via_instagram_api(shortcode)
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
        ok = tg_send_media_group(chat_id, media_urls, caption)
        if not ok:
            tg_send_message(chat_id, f"⚠️ Album send nahi hua ({count} items). Dobara try karo.")


# ============================================================
# Flask Routes
# ============================================================
@app.route('/')
def home():
    return "📸 Instagram Downloader v3.1 — Direct API + yt-dlp fallback + Auto Cookie Update!"


@app.route('/health')
def health():
    return "OK"


@app.route('/instagram', methods=['GET'])
def get_instagram_data():
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

    result = fetch_via_instagram_api(shortcode)

    if not result or not result.get('media_urls'):
        logger.warning("⚠️ Direct API failed — switching to yt-dlp fallback")
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
# NEW: Cookie Auto-Update Endpoint
# POST /update-cookies
# Header: X-Secret: <COOKIE_UPDATE_SECRET>
# Body: Cookie-Editor se copy kiya hua poora JSON array
# ============================================================
@app.route('/update-cookies', methods=['POST'])
def update_cookies():
    # 1. Secret check
    secret = request.headers.get("X-Secret", "") or request.args.get("secret", "")
    if not COOKIE_UPDATE_SECRET or secret != COOKIE_UPDATE_SECRET:
        logger.warning("❌ /update-cookies — unauthorized attempt")
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    # 2. JSON parse
    try:
        cookies_raw = request.get_json(force=True)
        if isinstance(cookies_raw, str):
            cookies_raw = json_module.loads(cookies_raw)
    except Exception as e:
        return jsonify({"success": False, "error": f"Invalid JSON: {e}"}), 400

    result, status_code = update_render_cookie_env(cookies_raw)
    return jsonify(result), status_code


# ============================================================
# Telegram Webhook
# ============================================================
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set")
        return jsonify({"ok": False}), 200

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False}), 200

    message = data.get('message') or data.get('edited_message')
    if not message:
        return jsonify({"ok": True}), 200

    chat_id = message.get('chat', {}).get('id')
    text = (message.get('text') or '').strip()

    if not chat_id or not text:
        return jsonify({"ok": True}), 200

    logger.info(f"💬 Telegram message from {chat_id}: {text[:80]}")
    chat_key = str(chat_id)

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
            "⚠️ <b>Note:</b> Sirf public posts ka kaam karega."
        )

        if is_cookie_update_authorized(chat_id):
            welcome += (
                "\n\n🔐 <b>Admin Command:</b>"
                "\n<code>/updatecookie</code>"
                "\n(Iske baad poora cookie JSON bhej do)"
            )

        tg_send_message(chat_id, welcome)
        return jsonify({"ok": True}), 200

    if text.startswith('/cancel'):
        if chat_key in PENDING_COOKIE_UPDATE_CHATS:
            PENDING_COOKIE_UPDATE_CHATS.discard(chat_key)
            tg_send_message(chat_id, "✅ Pending cookie update cancel ho gaya.", parse_mode=None)
        else:
            tg_send_message(chat_id, "ℹ️ Koi pending cookie update nahi hai.", parse_mode=None)
        return jsonify({"ok": True}), 200

    if text.startswith('/updatecookie') or text.startswith('/updatecookies'):
        if not TELEGRAM_ADMIN_CHAT_IDS:
            tg_send_message(
                chat_id,
                "❌ TELEGRAM_ADMIN_CHAT_ID env set nahi hai. Pehle Render env set karo.",
                parse_mode=None
            )
            return jsonify({"ok": True}), 200

        if not is_cookie_update_authorized(chat_id):
            tg_send_message(chat_id, "❌ Unauthorized command.", parse_mode=None)
            return jsonify({"ok": True}), 200

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            PENDING_COOKIE_UPDATE_CHATS.add(chat_key)
            tg_send_message(
                chat_id,
                "📥 Ab Cookie-Editor ka poora JSON array bhejo.\nTip: cancel ke liye /cancel likho.",
                parse_mode=None
            )
            return jsonify({"ok": True}), 200

        process_cookie_update_from_telegram(chat_id, parts[1])
        return jsonify({"ok": True}), 200

    if chat_key in PENDING_COOKIE_UPDATE_CHATS:
        if not is_cookie_update_authorized(chat_id):
            PENDING_COOKIE_UPDATE_CHATS.discard(chat_key)
            tg_send_message(chat_id, "❌ Unauthorized command.", parse_mode=None)
            return jsonify({"ok": True}), 200

        is_ok = process_cookie_update_from_telegram(chat_id, text)
        if is_ok:
            PENDING_COOKIE_UPDATE_CHATS.discard(chat_key)
        return jsonify({"ok": True}), 200

    if 'instagram.com' in text:
        url_match = re.search(r'https?://(?:www\.)?instagram\.com/[^\s]+', text)
        if url_match:
            ig_url = url_match.group(0)
            send_instagram_to_telegram(chat_id, ig_url)
        else:
            tg_send_message(chat_id, "❌ Instagram URL theek se nahi mili. Poora link paste karo.")
        return jsonify({"ok": True}), 200

    tg_send_message(
        chat_id,
        "❓ Samajh nahi aaya.\n\nInstagram post/reel ka link bhejo, main media download karke dunga.\n\nExample:\n<code>https://www.instagram.com/p/XXXXXXX/</code>"
    )
    return jsonify({"ok": True}), 200


# ============================================================
# Webhook Register Helper (one-time use)
# ============================================================
@app.route('/setup-webhook', methods=['GET'])
def setup_webhook():
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"success": False, "error": "TELEGRAM_BOT_TOKEN not set"}), 400

    base_url = request.args.get('url', '').strip().rstrip('/')
    if not base_url:
        return jsonify({"success": False, "error": "url parameter required"}), 400

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
