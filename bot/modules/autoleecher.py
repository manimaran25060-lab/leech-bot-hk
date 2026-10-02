# Module Made By @F2_Bots

from bot import bot, user, config_dict, OWNER_ID
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command
from pyrogram import *
import os
import re
import time
import asyncio
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
from asyncio import sleep
from re import sub
from cloudscraper import create_scraper
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.message_utils import clear_chat_info_cache
from bot.modules.mirror_leech import *
from bot.helper.ext_utils.bot_utils import new_task, sync_to_async
import pymongo
from pymongo import MongoClient
from bot import DATABASE_URL, DATABASE_NAME, bot_id

# Shared scraper instance — created once, reused everywhere
_scraper = create_scraper()

# Proxy chain — bypasses Oracle/datacenter IP blocks on TamilMV
# Layer 1: Cloudflare Worker (your own, most reliable)
FETCH_PROXY = "https://divine-waterfall-b0af.ragnar717890.workers.dev"
# Layer 2 & 3: Public free proxies (fallback if Worker fails or hits limit)
FETCH_PROXY_BACKUPS = [
    "https://api.allorigins.win/raw?url=",   # allorigins.win
    "https://corsproxy.io/?",                 # corsproxy.io
]

def _looks_like_xml_feed(text):
    """Quick check that a response body is actually an RSS/XML feed and not
    a Cloudflare challenge page / empty body / HTML error page returned with
    a misleading 200 status."""
    if not text:
        return False
    head = text[:1000].lower().lstrip()
    return head.startswith('<?xml') or '<rss' in head or '<feed' in head

def fetch_with_fallback(url, timeout=30, validate=None):
    """
    Try fetching URL through proxy chain:
    1. Direct request (fast — works on Heroku; fails on Oracle datacenter IPs)
    2. Cloudflare Worker proxy (FETCH_PROXY) — catches Oracle IP blocks
    3. allorigins.win
    4. corsproxy.io
    Returns requests.Response or raises Exception if all fail.

    validate: optional callable(response_text) -> bool. If provided, a 200
    response that fails validation is NOT accepted and the chain falls
    through to the next layer instead of silently returning bad content
    (e.g. a Cloudflare challenge page returned with status 200).
    """
    def _ok(r):
        return r.status_code == 200 and (validate is None or validate(r.text))

    # Layer 1: Direct — Heroku uses this, Oracle will fail and fall through to proxy
    try:
        r = _scraper.get(url, allow_redirects=True, timeout=timeout)
        if _ok(r):
            print(f"✅ Direct fetch OK: {url[:60]}")
            return r
        print(f"⚠️ Direct fetch status {r.status_code} (or failed validation), trying proxy...")
    except Exception as e:
        print(f"⚠️ Direct fetch error: {e}, trying proxy...")

    # Layer 2: Cloudflare Worker — Oracle datacenter IPs get through here
    try:
        proxy_url = f"{FETCH_PROXY}?url={requests.utils.quote(url, safe='')}"
        r = requests.get(proxy_url, timeout=timeout)
        if r.status_code == 200 and len(r.text) > 500 and (validate is None or validate(r.text)):
            print(f"✅ Cloudflare Worker fetch OK: {url[:60]}")
            return r
        print(f"⚠️ Cloudflare Worker status {r.status_code} (or failed validation), trying backup proxies...")
    except Exception as e:
        print(f"⚠️ Cloudflare Worker error: {e}, trying backup proxies...")

    # Layer 3 & 4: Public backup proxies
    for backup in FETCH_PROXY_BACKUPS:
        try:
            proxy_url = f"{backup}{requests.utils.quote(url, safe='')}"
            r = requests.get(proxy_url, timeout=timeout)
            if _ok(r):
                print(f"✅ Backup proxy fetch OK ({backup[:30]}): {url[:60]}")
                return r
            print(f"⚠️ Backup proxy {backup[:30]} status {r.status_code} (or failed validation), trying next...")
        except Exception as e:
            print(f"⚠️ Backup proxy {backup[:30]} error: {e}, trying next...")

    raise Exception(f"All proxies failed for: {url}")

temp_urls = {}  # pls dont remove this
is_auto_leecher        = True
AA_DELAY               = 5     # delay between full loop cycles (seconds)
BB_DELAY               = 7     # delay between each feed / leech dispatch (seconds)
PENDING_RETRY_INTERVAL = 120   # retry pending posts every 2 minutes
PENDING_MAX_AGE        = 3600  # give up on pending post after 1 hour
STABLE_MINUTES         = 60    # remove from queue after links stable for 60 mins

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
IMGBB_API_KEY = "7c555e92974a9049ac25c7e6f2afa652"

# ── MongoDB ────────────────────────────────────────────────────────
client      = MongoClient(DATABASE_URL)
db          = client[DATABASE_NAME]
rss_domains = db['rss_domains']
# Bot-specific RSS pointer collection — each bot tracks its own last_seen_url
# so Heroku and Oracle process feeds independently without stepping on each other
collection  = db[f'rss_col_data_{bot_id}']
thumbs      = db['thumbnails']
leeched_col = db['leeched_urls']   # SHARED — prevents both bots leeching the same movie

# ── Global state ───────────────────────────────────────────────────

# pending_posts: posts detected but not yet fully leeched
# { url: { title, keyword, added_at, link_count, leeched, stable_since } }
pending_posts = {}

# leeched_urls: loaded from DB on startup — survives restarts
# prevents duplicate leeching of cross-posted movies
leeched_urls = set(doc['url'] for doc in leeched_col.find({}, {'url': 1}))
print(f"♻️ Loaded {len(leeched_urls)} leeched URLs from DB")

# Base-title dedup: prevents language variants of the same movie (Tamil / Telugu /
# Multi-Audio) from all being processed in the same session.
# TamilMV posts each language in a separate forum, so each hits a different RSS feed
# as a "new" URL — but they share the same "Movie Name (Year)" base title.
# { normalized_base_title: first_seen_timestamp }
_title_seen: dict = {}
_TITLE_TTL = 86400  # 24 hours — forget after this so rereleases aren't blocked

def _extract_base_title(raw: str) -> str:
    """'Ustaad Bhagat Singh (2026) Tamil TRUE WEB-DL...' → 'ustaad bhagat singh (2026)'"""
    m = re.match(r'^(.+?\(\d{4}\))', raw.strip())
    return m.group(1).strip().lower() if m else raw.strip().lower()[:60]

def _is_title_seen(base: str) -> bool:
    ts = _title_seen.get(base)
    if ts is None:
        return False
    if time.time() - ts > _TITLE_TTL:
        _title_seen.pop(base, None)
        return False
    return True

def _mark_title_seen(base: str):
    if base and base not in _title_seen:
        _title_seen[base] = time.time()

# Feed health tracking — updated every cycle
# { rss_url: { 'last_check': float, 'last_success': float, 'fail_count': int, 'status': str } }
feed_health = {}

# Domain failure tracking — for auto domain update detection
domain_fail_count = {'tmv': 0, 'tbl': 0}
DOMAIN_FAIL_THRESHOLD = 3  # notify after 3 consecutive failures

# Admin chat for error reports — uses AUTO_LEECH_GRP_ID by default
# Set to a different chat ID in config if needed
def get_admin_chat():
    return config_dict.get('AUTO_LEECH_GRP_ID')


# ── Domain helpers ─────────────────────────────────────────────────

def get_link(name):
    tk = rss_domains.find_one({"name": name})
    return tk['url'] if tk is not None else None

Tamilmv_domain      = get_link("tmv")
Tamilblaster_domain = get_link("tbl")

TAMILMV_HW            = f"{Tamilmv_domain}/index.php?/forums/forum/17-hollywood-movies-in-multi-audios/all.xml"
TAMILMV_TAMIL_HD      = f"{Tamilmv_domain}/index.php?/forums/forum/11-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_TAMIL_CAM     = f"{Tamilmv_domain}/index.php?/forums/forum/10-predvd-dvdscr-cam-tc/all.xml"
TAMILMV_TAMIL_WEB_SERIES = f"{Tamilmv_domain}/index.php?/forums/forum/19-web-series-tv-shows/all.xml"  # OLD — kept for backward compat
TAMILMV_WEB_SERIES       = f"{Tamilmv_domain}/index.php?/forums/forum/55-web-series-tv-shows/all.xml"  # REAL current web series forum
TAMILMV_TELEGU_HDRIP  = f"{Tamilmv_domain}/index.php?/forums/forum/25-hd-rips-dvd-rips-br-rips/all.xml"
TAMILMV_TELEGU_WEBHD  = f"{Tamilmv_domain}/index.php?/forums/forum/24-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_HINDI_WEBHD   = f"{Tamilmv_domain}/index.php?/forums/forum/58-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_MALAY_WEBHD   = f"{Tamilmv_domain}/index.php?/forums/forum/36-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_MALAY_PreDVD  = f"{Tamilmv_domain}/index.php?/forums/forum/35-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_ENGLISH_WEBHD = f"{Tamilmv_domain}/index.php?/forums/forum/49-web-hd-itunes-hd-bluray/all.xml"
TAMILMV_ENGLISH_LANG  = f"{Tamilmv_domain}/index.php?/forums/forum/45-english-language/all.xml"
TAMILBLASTER_TAMIL    = f"{Tamilblaster_domain}/index.php?/forums/forum/7-tamil-new-movies-hdrips-bdrips-dvdrips-hdtv/all.xml"
TAMILBLASTER_HW       = f"{Tamilblaster_domain}/index.php?/forums/forum/9-tamil-dubbed-movies-bdrips-hdrips-dvdscr-hdcam-in-multi-audios/all.xml"

rss_urls = [
    TAMILMV_HW, TAMILMV_TAMIL_HD, TAMILMV_TAMIL_CAM,
    TAMILMV_TELEGU_HDRIP, TAMILMV_TELEGU_WEBHD, TAMILMV_HINDI_WEBHD,
    TAMILMV_MALAY_WEBHD, TAMILMV_MALAY_PreDVD, TAMILMV_ENGLISH_WEBHD,
    TAMILBLASTER_TAMIL, TAMILBLASTER_HW, TAMILMV_TAMIL_WEB_SERIES,
    TAMILMV_WEB_SERIES, TAMILMV_ENGLISH_LANG,
]


# ── Domain commands ────────────────────────────────────────────────

@new_task
async def setdomain(client, message):
    if '|' in message.text:
        name = message.text.split('|')[0].split(' ')[1].strip()
        link = message.text.split('|')[1].strip()
        tk = insert_or_update_link(name, {"url": link, "title": name})
        # BUG FIX: Rebuild domain variables and rss_urls immediately so the
        # new domain takes effect without needing a bot restart.
        global Tamilmv_domain, Tamilblaster_domain, rss_urls
        global TAMILMV_HW, TAMILMV_TAMIL_HD, TAMILMV_TAMIL_CAM, TAMILMV_TAMIL_WEB_SERIES
        global TAMILMV_TELEGU_HDRIP, TAMILMV_TELEGU_WEBHD, TAMILMV_HINDI_WEBHD
        global TAMILMV_MALAY_WEBHD, TAMILMV_MALAY_PreDVD, TAMILMV_ENGLISH_WEBHD
        global TAMILMV_ENGLISH_LANG, TAMILMV_WEB_SERIES
        global TAMILBLASTER_TAMIL, TAMILBLASTER_HW
        Tamilmv_domain      = get_link("tmv")
        Tamilblaster_domain = get_link("tbl")
        TAMILMV_HW            = f"{Tamilmv_domain}/index.php?/forums/forum/17-hollywood-movies-in-multi-audios/all.xml"
        TAMILMV_TAMIL_HD      = f"{Tamilmv_domain}/index.php?/forums/forum/11-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_TAMIL_CAM     = f"{Tamilmv_domain}/index.php?/forums/forum/10-predvd-dvdscr-cam-tc/all.xml"
        TAMILMV_TAMIL_WEB_SERIES = f"{Tamilmv_domain}/index.php?/forums/forum/19-web-series-tv-shows/all.xml"
        TAMILMV_TELEGU_HDRIP  = f"{Tamilmv_domain}/index.php?/forums/forum/25-hd-rips-dvd-rips-br-rips/all.xml"
        TAMILMV_TELEGU_WEBHD  = f"{Tamilmv_domain}/index.php?/forums/forum/24-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_HINDI_WEBHD   = f"{Tamilmv_domain}/index.php?/forums/forum/58-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_MALAY_WEBHD   = f"{Tamilmv_domain}/index.php?/forums/forum/36-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_MALAY_PreDVD  = f"{Tamilmv_domain}/index.php?/forums/forum/35-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_ENGLISH_WEBHD = f"{Tamilmv_domain}/index.php?/forums/forum/49-web-hd-itunes-hd-bluray/all.xml"
        TAMILMV_ENGLISH_LANG  = f"{Tamilmv_domain}/index.php?/forums/forum/45-english-language/all.xml"
        TAMILMV_WEB_SERIES    = f"{Tamilmv_domain}/index.php?/forums/forum/55-web-series-tv-shows/all.xml"
        TAMILBLASTER_TAMIL    = f"{Tamilblaster_domain}/index.php?/forums/forum/7-tamil-new-movies-hdrips-bdrips-dvdrips-hdtv/all.xml"
        TAMILBLASTER_HW       = f"{Tamilblaster_domain}/index.php?/forums/forum/9-tamil-dubbed-movies-bdrips-hdrips-dvdscr-hdcam-in-multi-audios/all.xml"
        rss_urls = [
            TAMILMV_HW, TAMILMV_TAMIL_HD, TAMILMV_TAMIL_CAM,
            TAMILMV_TELEGU_HDRIP, TAMILMV_TELEGU_WEBHD, TAMILMV_HINDI_WEBHD,
            TAMILMV_MALAY_WEBHD, TAMILMV_MALAY_PreDVD, TAMILMV_ENGLISH_WEBHD,
            TAMILBLASTER_TAMIL, TAMILBLASTER_HW, TAMILMV_TAMIL_WEB_SERIES,
            TAMILMV_WEB_SERIES, TAMILMV_ENGLISH_LANG,
        ]
        clear_chat_info_cache()  # clear dump channel peer cache so new domain resolves fresh
        await message.reply_text(f"{tk}\n✅ RSS feeds updated in-memory. No restart needed.")
    else:
        await message.reply_text(
            "Wrong format!\n\nHow To Use: /setd [keyword] | [Domain]\n\n"
            "KeyWords:\n  tmv - for 1tamilmv\n  tbl - For tamilblasters"
        )

@new_task
async def getdomains(client, message):
    d_list = "".join(f"{l['url']}\n" for l in get_all_links())
    await message.reply_text(f"The Domains Are:\n\n{d_list or 'None set.'}")

def insert_or_update_link(name, link_data):
    result = rss_domains.update_one({"name": name}, {"$set": link_data}, upsert=True)
    return f"Link '{name}' {'inserted' if result.matched_count == 0 else 'updated'}."

def get_all_links():
    return rss_domains.find({})


# ── Utility functions ──────────────────────────────────────────────

def post_to_dpaste(content):
    # Try dpaste.org first
    try:
        r = requests.post("https://dpaste.org/api/",
                          data={"content": content, "syntax": "text", "expiry_days": "360"}, timeout=30)
        if r.status_code == 200:
            return r.text.strip()
    except Exception:
        pass
    # Fallback: try hastebin
    try:
        r = requests.post("https://hastebin.com/documents",
                          data=content.encode('utf-8'), timeout=30)
        if r.status_code == 200:
            key = r.json().get('key', '')
            if key:
                return f"https://hastebin.com/{key}"
    except Exception:
        pass
    # Last fallback: return content truncated inline
    return content[:300] + "..." if len(content) > 300 else content

def upload_to_imgbb(image_path):
    with open(image_path, "rb") as f:
        r = requests.post("https://api.imgbb.com/1/upload",
                          params={"key": IMGBB_API_KEY}, files={"image": f})
    return r.json()['data']['url'] if r.status_code == 200 else None

def download_torrent(url, file_name):
    """Download .torrent file through proxy fallback chain."""
    try:
        r = fetch_with_fallback(url)
        if b"announce" in r.content[:500]:
            with open(file_name, "wb") as f:
                f.write(r.content)
            print(f"✅ Torrent downloaded: {file_name}")
            return file_name
        print("❌ Not a valid torrent file.")
        return None
    except Exception as e:
        print(f"❌ download_torrent error: {e}")
        return None

def scrape_links(post_url):
    """Fetch post page through proxy fallback chain to bypass IP blocks."""
    try:
        r     = fetch_with_fallback(post_url)
        soup  = BeautifulSoup(r.text, 'html.parser')
        mag   = soup.select('a[href^="magnet:?xt=urn:btih:"]')
        tor   = soup.select('a[data-fileext="torrent"]')
        title = soup.title.string if soup.title else post_url
        return tor, mag, title
    except Exception as e:
        print(f"❌ scrape_links error for {post_url}: {e}")
        return None, None, None

def mark_leeched_sync(url):
    """Save URL to persistent leeched_col DB (sync, run via sync_to_async)."""
    leeched_col.update_one(
        {"url": url},
        {"$set": {"url": url, "added_at": datetime.datetime.utcnow()}},
        upsert=True
    )

def update_keyword_db_sync(keyword, url):
    """Update rss_col_data keyword URL (sync, run via sync_to_async)."""
    collection.update_one({"keyword": keyword}, {"$set": {"url": url}}, upsert=True)

async def report_error(msg):
    """Send error message to admin chat."""
    try:
        await bot.send_message(chat_id=get_admin_chat(), text=f"⚠️ Auto Leech Error:\n{msg}")
    except Exception as e:
        print(f"❌ Failed to send error report: {e}")


# ── MT_list builder ────────────────────────────────────────────────

def build_mt_list(tor, mag, skip_hrefs=None):
    """
    Build leech list from torrent and magnet links independently.
    skip_hrefs: hrefs already leeched — only return new ones.
    """
    skip_hrefs = skip_hrefs or set()
    MT_list = []

    # BUG FIX: use a separate mag_index counter instead of raw tor index i.
    # When earlier tor entries are skipped via skip_hrefs, index i drifts and
    # the wrong magnet gets paired with the torrent (or IndexError if i >= len(mag)).
    mag_index = 0
    for i, t in enumerate(tor):
        if t['href'] in skip_hrefs:
            mag_index += 1  # keep mag_index in sync even for skipped entries
            continue
        filename = sub(r"www\S+|\- |\.torrent", '', t.string or '').strip()
        if not filename:
            mag_index += 1
            continue
        if mag_index < len(mag):
            m = mag[mag_index]
            p_text = (f'🧲 Magnet Name: {filename} -->\n\n'
                      f'<code>{m["href"]}</code>\n\n'
                      f'🗒️ Torrent --> <a href="{t["href"]}"><b>Link</b></a>.')
            # keep the paired magnet href so leech_mt_list can fall back to it
            # if the .torrent file download fails (source sites often block/expire
            # direct .torrent downloads while the magnet keeps working)
            MT_list.append([filename, t['href'], p_text, 'torrent', m['href']])
        else:
            p_text = f'🗒️ Torrent: {filename} --> <a href="{t["href"]}"><b>Link</b></a>.'
            MT_list.append([filename, t['href'], p_text, 'torrent', None])
        mag_index += 1

    for i, m in enumerate(mag):
        if i < len(tor):
            continue
        if m['href'] in skip_hrefs:
            continue
        raw  = m['href'].split('dn=')[-1].split('&')[0]
        name = requests.utils.unquote(raw).replace('+', ' ').strip() or f"magnet_{i+1}"
        p_text = f'🧲 Magnet: {name}\n\n<code>{m["href"]}</code>'
        MT_list.append([name, m['href'], p_text, 'magnet'])

    return MT_list


# ── Leech dispatcher ───────────────────────────────────────────────

async def leech_mt_list(MT_list):
    # BUG FIX: moved get_users outside the for-loop — was making one Telegram
    # API call per torrent file, causing unnecessary rate-limit pressure.
    owner_user = await bot.get_users(OWNER_ID)
    for atl in MT_list:
        file_name  = os.path.basename(atl[0]) + ".torrent"
        file_link  = atl[1]
        link_type  = atl[3]
        paste_link = await sync_to_async(post_to_dpaste, atl[2])
        caption    = f"🗒️ Name: {atl[0]}\n\n🔗 Links:\n{paste_link}"
        try:
            if link_type == 'magnet':
                leech_msg = await bot.send_message(
                    chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                    text=f"/qbleech {file_link}\nTag: @{owner_user.username} {OWNER_ID}"
                )
                leech_msg.from_user = owner_user
                await qb_leech(bot, leech_msg)
                await asyncio.sleep(BB_DELAY)
                await leech_msg.delete()
            else:
                if await sync_to_async(download_torrent, file_link, file_name):
                    try:
                        filee = await bot.send_document(
                            chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                            document=file_name, caption=caption
                        )
                    finally:
                        # BUG FIX: always delete the .torrent file, even if send_document raises
                        if os.path.exists(file_name):
                            os.remove(file_name)
                    leech_msg = await filee.reply_text(
                        f"/qbleech\nTag: @{owner_user.username} {OWNER_ID}"
                    )
                    leech_msg.from_user = owner_user
                    await qb_leech(bot, leech_msg)
                    await asyncio.sleep(BB_DELAY)
                    await leech_msg.delete()
                elif len(atl) > 4 and atl[4]:
                    # .torrent file download failed but a paired magnet exists — use it instead
                    fallback_magnet = atl[4]
                    leech_msg = await bot.send_message(
                        chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                        text=f"/qbleech {fallback_magnet}\nTag: @{owner_user.username} {OWNER_ID}"
                    )
                    leech_msg.from_user = owner_user
                    await qb_leech(bot, leech_msg)
                    await asyncio.sleep(BB_DELAY)
                    await leech_msg.delete()
                else:
                    await bot.send_message(
                        chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                        text=f"❌ Failed to download torrent.\n\n{caption}"
                    )
        except Exception as e:
            await bot.send_message(
                chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                text=f"❌ Error leeching {atl[0]}: {e}"
            )
        await asyncio.sleep(BB_DELAY)


# ── Pending queue worker ───────────────────────────────────────────

@new_task
async def pending_queue_worker():
    """
    Watches posts that had 0 or partial links when first detected.
    - Retries every PENDING_RETRY_INTERVAL seconds
    - Only leeches NEW links (skip_hrefs tracks already-leeched ones)
    - Removes post after links stable for STABLE_MINUTES (60 mins)
    - Gives up after PENDING_MAX_AGE (1 hour) if still 0 links
    """
    print("⏳ Pending queue worker started")
    # BUG FIX: track when we last ran the DB cleanup so it runs daily,
    # not just once at startup. Previously the cleanup never ran again after the
    # first bot start, so leeched_col grew forever.
    last_cleanup = time.time()

    while True:
        await sleep(PENDING_RETRY_INTERVAL)
        # Cleanup leeched_col entries older than 7 days — run once per day
        if time.time() - last_cleanup > 86400:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
            await sync_to_async(leeched_col.delete_many, {"added_at": {"$lt": cutoff}})
            last_cleanup = time.time()
            print("🗑️ Ran daily leeched_col cleanup")
        if not pending_posts:
            continue

        now       = time.time()
        to_delete = []

        for post_url, meta in list(pending_posts.items()):
            age = now - meta['added_at']

            # Give up after max age with 0 links
            if age > PENDING_MAX_AGE and meta['link_count'] == 0:
                print(f"🗑️ Giving up on 0-link post ({int(age//60)}m): {post_url}")
                to_delete.append(post_url)
                # Mark leeched so it's not picked up again
                # DB was already updated when first queued — no update needed
                leeched_urls.add(post_url)
                await sync_to_async(mark_leeched_sync, post_url)
                await bot.send_message(
                    chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                    text=(f"⚠️ Gave up — no torrent links after {PENDING_MAX_AGE//60} mins:\n"
                          f"<b>{meta['title']}</b>\n\n{post_url}")
                )
                continue

            # Re-scrape the page
            tor, mag, title = await sync_to_async(scrape_links, post_url)
            if tor is None:
                continue  # network error — retry next cycle

            current_count = len(tor) + len(mag)
            leeched_hrefs = set(meta['leeched'])
            new_MT_list   = build_mt_list(tor, mag, skip_hrefs=leeched_hrefs)

            if not new_MT_list:
                if current_count == 0:
                    print(f"⏳ Still 0 links for '{meta['title']}' ({int(age//60)}m)")
                    continue

                # All current links already leeched — check stability
                stable_since = meta.get('stable_since', now)
                stable_mins  = (now - stable_since) / 60
                if stable_mins >= STABLE_MINUTES:
                    print(f"✅ Stable {int(stable_mins)}m — done with '{meta['title']}'")
                    to_delete.append(post_url)
                else:
                    print(f"⏳ Stable {int(stable_mins)}m/{STABLE_MINUTES}m — still watching '{meta['title']}'")
                continue

            # New links found — mark immediately before leeching
            print(f"✅ {len(new_MT_list)} new link(s) for '{meta['title']}' ({int(age//60)}m)")
            leeched_urls.add(post_url)
            await sync_to_async(mark_leeched_sync, post_url)
            # Note: DB was already updated when this entry was first queued
            # No need to update again here — avoids overwriting newer entries

            try:
                if meta['link_count'] == 0:
                    post_title = await bot.send_message(
                        chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                        text=(f"✅ Torrent links ready (pending {int(age//60)} mins)!\n"
                              f"Movie Name: <b><u>{title}</u></b>\n\nMade By @F2_Bots.")
                    )
                    try:
                        await bot.pin_chat_message(config_dict['AUTO_LEECH_GRP_ID'], post_title.id)
                    except Exception:
                        pass
                else:
                    await bot.send_message(
                        chat_id=config_dict['AUTO_LEECH_GRP_ID'],
                        text=(f"🔄 {len(new_MT_list)} new link(s) added:\n"
                              f"<b><u>{title}</u></b>\nLeeching now...")
                    )

                await leech_mt_list(new_MT_list)

                new_leeched = leeched_hrefs | {atl[1] for atl in new_MT_list}
                pending_posts[post_url]['leeched']      = list(new_leeched)
                pending_posts[post_url]['link_count']   = current_count
                pending_posts[post_url]['stable_since'] = time.time()

                end_sticker = "CAACAgUAAxkBAAIjxGY75nsXUSCCFO6LB-KiGRPC5kiuAAJzBgACJggpVXKB2uxzC9oxHgQ"
                await bot.send_sticker(config_dict['AUTO_LEECH_GRP_ID'], end_sticker)

            except Exception as e:
                print(f"❌ Error leeching pending post {post_url}: {e}")

        for url in to_delete:
            pending_posts.pop(url, None)


# ── Main RSS feed processor ────────────────────────────────────────

async def _process_entry(entry_link, keyword):
    """Process a single RSS entry link. Called for each new entry in the feed."""

    # Already being watched by pending worker — update this keyword's DB and skip
    if entry_link in pending_posts:
        await sync_to_async(update_keyword_db_sync, keyword, entry_link)
        return

    # Already leeched (cross-post or previous run) — update DB and skip
    if entry_link in leeched_urls:
        await sync_to_async(update_keyword_db_sync, keyword, entry_link)
        return

    # New post — scrape it
    tor, mag, title = await sync_to_async(scrape_links, entry_link)
    if tor is None:
        # Scrape failed — could be IP block or domain change
        # Queue for retry instead of silently dropping (Oracle datacenter IPs often blocked)
        print(f"❌ Scrape failed for: {entry_link} — queuing for retry")
        domain_key = 'tmv' if (Tamilmv_domain and Tamilmv_domain in entry_link) else 'tbl'
        domain_fail_count[domain_key] = domain_fail_count.get(domain_key, 0) + 1
        if domain_fail_count[domain_key] == DOMAIN_FAIL_THRESHOLD:
            await report_error(
                f"🌐 Domain may have changed or IP is blocked!\n"
                f"Scraping failed {DOMAIN_FAIL_THRESHOLD} times in a row.\n"
                f"URL: <code>{entry_link}</code>\n\n"
                f"Update domain using: /setd {'tmv' if domain_key == 'tmv' else 'tbl'} | [new domain]"
            )
        # Queue for pending_queue_worker to retry via proxy
        if entry_link not in pending_posts:
            slug = entry_link.split('/')[-2] if entry_link.endswith('/') else entry_link.split('/')[-1]
            fallback_title = slug.replace('-', ' ').title()[:80]
            pending_posts[entry_link] = {
                'title':        fallback_title,
                'keyword':      keyword,
                'added_at':     time.time(),
                'link_count':   0,
                'leeched':      [],
                'stable_since': time.time(),
            }
            leeched_urls.add(entry_link)
            await sync_to_async(mark_leeched_sync, entry_link)
            await sync_to_async(update_keyword_db_sync, keyword, entry_link)
        return

    MT_list    = build_mt_list(tor, mag)
    link_count = len(tor) + len(mag)

    if not MT_list:
        # No links yet — queue, notify, and update DB
        # DB update ensures _process_feed won't re-queue this entry next cycle
        print(f"⏳ 0 links for '{title}', queuing")
        await sync_to_async(update_keyword_db_sync, keyword, entry_link)
        pending_posts[entry_link] = {
            'title':        title,
            'keyword':      keyword,
            'added_at':     time.time(),
            'link_count':   0,
            'leeched':      [],
            'stable_since': time.time(),
        }
        await bot.send_message(
            chat_id=config_dict['AUTO_LEECH_GRP_ID'],
            text=(f"⏳ New post detected but no torrent links yet.\n"
                  f"Movie Name: <b><u>{title}</u></b>\n\n"
                  f"Will auto-leech when links appear (checking every {PENDING_RETRY_INTERVAL // 60} mins).")
        )
        return

    # Links found — mark leeched IMMEDIATELY before anything else
    leeched_urls.add(entry_link)
    await sync_to_async(mark_leeched_sync, entry_link)
    await sync_to_async(update_keyword_db_sync, keyword, entry_link)

    post_title = await bot.send_message(
        config_dict['AUTO_LEECH_GRP_ID'],
        f"Movie Name: <b><u>{title}</u></b>\n\n - Say Jai BYNF\n Made By @F2_Bots."
    )
    try:
        await bot.pin_chat_message(config_dict['AUTO_LEECH_GRP_ID'], post_title.id)
    except Exception:
        pass

    await leech_mt_list(MT_list)

    end_sticker = "CAACAgUAAxkBAAIjxGY75nsXUSCCFO6LB-KiGRPC5kiuAAJzBgACJggpVXKB2uxzC9oxHgQ"
    await bot.send_sticker(config_dict['AUTO_LEECH_GRP_ID'], end_sticker)

    # Queue to watch for additional links added later
    leeched_hrefs = [atl[1] for atl in MT_list]
    pending_posts[entry_link] = {
        'title':        title,
        'keyword':      keyword,
        'added_at':     time.time(),
        'link_count':   link_count,
        'leeched':      leeched_hrefs,
        'stable_since': time.time(),
    }
    print(f"⏳ Watching '{title}' for more links ({link_count} leeched)")


async def _process_feed(rss_url, keyword):
    """Shared logic for all RSS feeds. Loops all new entries, not just entries[0]."""
    # Skip feed if domain not configured yet (e.g. tbl not set)
    if 'None/' in rss_url:
        return

    now_ts = time.time()

    # Init feed health entry if first time
    if rss_url not in feed_health:
        feed_health[rss_url] = {
            'last_check':   None,
            'last_success': None,
            'fail_count':   0,
            'status':       'unknown',
            'keyword':      keyword,
        }

    feed_health[rss_url]['last_check'] = now_ts

    def _fetch_feed(url):
        r = fetch_with_fallback(url, validate=_looks_like_xml_feed)
        return feedparser.parse(r.text)

    try:
        feed = await asyncio.wait_for(
            sync_to_async(_fetch_feed, rss_url), timeout=30
        )
    except asyncio.TimeoutError:
        feed_health[rss_url]['fail_count'] += 1
        feed_health[rss_url]['status'] = 'timeout'
        print(f"⏰ Feed timed out: {rss_url}")
        return

    if len(feed.entries) == 0:
        feed_health[rss_url]['fail_count'] += 1
        feed_health[rss_url]['status'] = 'empty'
        # Suppress warning if domain is not set (None in URL)
        if 'None/' not in rss_url:
            print(f"⚠️ No entries in feed: {rss_url}")
        # Check if domain may have changed
        domain_key = 'tmv' if 'tamilmv' in rss_url.lower() or (Tamilmv_domain and Tamilmv_domain in rss_url) else 'tbl'
        domain_fail_count[domain_key] = domain_fail_count.get(domain_key, 0) + 1
        if domain_fail_count[domain_key] == DOMAIN_FAIL_THRESHOLD:
            await report_error(
                f"🌐 Domain may have changed!\n"
                f"Feed returned 0 entries {DOMAIN_FAIL_THRESHOLD} times in a row.\n"
                f"Feed: <code>{rss_url}</code>\n\n"
                f"Update domain using: /setd {'tmv' if domain_key == 'tmv' else 'tbl'} | [new domain]"
            )
        return

    # Feed is healthy — reset counters
    feed_health[rss_url]['last_success'] = now_ts
    feed_health[rss_url]['fail_count']   = 0
    feed_health[rss_url]['status']       = 'ok'
    domain_key = 'tmv' if (Tamilmv_domain and Tamilmv_domain in rss_url) else 'tbl'
    domain_fail_count[domain_key]        = 0

    # Get last seen URL for this keyword from DB
    existing = await sync_to_async(collection.find_one, {"keyword": keyword})
    if existing is None or existing.get("url") is None:
        # First time this keyword is seen — save current newest entry as starting point
        # without leeching anything. Only future posts will be leeched.
        newest = feed.entries[0].link
        await sync_to_async(collection.insert_one, {"keyword": keyword, "url": newest})
        print(f"🔖 First run for '{keyword}' — saved starting point, no leech")
        return
    
    last_seen_url = existing["url"]

    # "Nhai-Illa" means keyword existed but was never properly initialized
    # Treat same as first run — save starting point without leeching
    if last_seen_url == "Nhai-Illa":
        newest = feed.entries[0].link
        await sync_to_async(update_keyword_db_sync, keyword, newest)
        print(f"🔖 Initializing '{keyword}' — saved starting point, no leech")
        return

    # Collect all new entries (stop when we hit the last seen URL)
    # entries are ordered newest first, so we iterate until we find last_seen_url
    new_entries = []
    found_last  = False
    for entry in feed.entries:
        if entry.link == last_seen_url:
            found_last = True
            break  # everything from here was already processed
        new_entries.append((entry.link, getattr(entry, 'title', '')))

    # If last_seen_url wasn't found in the feed it means it aged off (feed only
    # keeps ~20-50 entries).  Processing everything would re-leech old posts.
    # Safe fix: reset the pointer to the current newest entry and skip this cycle.
    if not found_last:
        newest = feed.entries[0].link
        await sync_to_async(update_keyword_db_sync, keyword, newest)
        print(f"⚠️ last_seen_url not in feed for '{keyword}' — resetting pointer to newest, skipping old entries")
        return

    if not new_entries:
        return  # nothing new

    # Process oldest-first so leeching happens in chronological order
    # DB is updated per-entry inside _process_entry after each is handled
    # This ensures a network error on one entry doesn't permanently lose others
    for entry_link, rss_title in reversed(new_entries):
        # Base-title dedup: TamilMV posts the same movie in separate language forums
        # (e.g. Multi-Audio, Tamil, Telugu) each with a unique URL but the same
        # "Movie Name (Year)" prefix. Skip language variants already handled in
        # this session to avoid redundant notifications and leech attempts.
        if rss_title:
            base = _extract_base_title(rss_title)
            if _is_title_seen(base):
                # Advance DB pointer so this keyword doesn't re-detect it next cycle
                await sync_to_async(update_keyword_db_sync, keyword, entry_link)
                print(f"🔁 Skipping duplicate title '{base}' ({keyword}): {entry_link}")
                continue
        await asyncio.sleep(BB_DELAY)
        await _process_entry(entry_link, keyword)
        # Mark title seen AFTER processing so the first variant always goes through.
        # Subsequent variants from other feeds will be skipped for 24 hours.
        if rss_title:
            _mark_title_seen(_extract_base_title(rss_title))

    # After all entries processed, ensure DB is at the newest entry
    await sync_to_async(update_keyword_db_sync, keyword, new_entries[0][0])


async def tamilmv(rss_url, keyword):
    await _process_feed(rss_url, keyword)

async def tamilblaster(rss_url, keyword):
    await _process_feed(rss_url, keyword)


# ── Main RSS loop ──────────────────────────────────────────────────

@new_task
async def RSS_auto_leecher():
    print("RSS Auto Leecher Started By @F2_Bots")
    while is_auto_leecher:
        await sleep(AA_DELAY)
        try:
            for rss_url in rss_urls:
                await sleep(BB_DELAY)
                if   rss_url == TAMILMV_HW:              await tamilmv(rss_url, 'hollywood_1tmv')
                elif rss_url == TAMILMV_TAMIL_HD:         await tamilmv(rss_url, 'tamilmv_tamil_hd')
                elif rss_url == TAMILMV_TAMIL_CAM:        await tamilmv(rss_url, 'tamilmv_tamil_cam_rip')
                elif rss_url == TAMILMV_TAMIL_WEB_SERIES: await tamilmv(rss_url, 'tamilmv_tamil_web_series')
                elif rss_url == TAMILMV_TELEGU_HDRIP:     await tamilmv(rss_url, 'tamilmv_telegu_hdrip')
                elif rss_url == TAMILMV_TELEGU_WEBHD:     await tamilmv(rss_url, 'tamilmv_telegu_webhd')
                elif rss_url == TAMILMV_HINDI_WEBHD:      await tamilmv(rss_url, 'tamilmv_hindi_webhd')
                elif rss_url == TAMILMV_MALAY_WEBHD:      await tamilmv(rss_url, 'tamilmv_maly_webhd')
                elif rss_url == TAMILMV_MALAY_PreDVD:     await tamilmv(rss_url, 'tamilmv_maly_predvd')
                elif rss_url == TAMILMV_ENGLISH_WEBHD:    await tamilmv(rss_url, 'tamilmv_english_webhd')
                elif rss_url == TAMILBLASTER_TAMIL:       await tamilblaster(rss_url, 'tamil_tbl')
                elif rss_url == TAMILBLASTER_HW:          await tamilblaster(rss_url, 'hollywood_tbl')
                elif rss_url == TAMILMV_WEB_SERIES:       await tamilmv(rss_url, 'tamilmv_web_series_55')
                elif rss_url == TAMILMV_ENGLISH_LANG:     await tamilmv(rss_url, 'tamilmv_english_lang')
        except Exception as e:
            print(f"Error in RSS loop: {e}")


# ── Clone settings ─────────────────────────────────────────────────

async def clone_ThiruEmpire(bot, message):
    bot_id  = bot.me.id
    col     = client['thiruml'][f'users.{bot_id}']
    user_id = message.from_user.id
    try:
        doc = col.find_one({'_id': user_id})
        if not doc:
            return await message.reply_text("Document not found.")
        doc.pop('_id', None)
        doc['_id'] = bot_id
        action = "updated" if col.find_one({'_id': bot_id}) else "cloned"
        if action == "updated":
            col.replace_one({'_id': bot_id}, doc)
        else:
            col.insert_one(doc)
        s = {
            'captian':  doc.get('lcaption', 'Not Available ❌'),
            'thumb':    'Thumbnail Saved ✅' if doc.get('thumb') else 'Not Available ❌',
            'lprefix':  doc.get('lprefix',  'Not Available ❌'),
            'lsuffix':  doc.get('lsuffix',  'Not Available ❌'),
            'as_doc':   'Document Type' if doc.get('as_doc', True) else 'Media Type',
            'lremname': doc.get('lremname', 'Not Available ❌'),
            'ldump':    doc.get('ldump',    'Not Available ❌'),
            'metadata': doc.get('metadata', 'Not Available ❌'),
        }
        await message.reply_text(
            f"Document {action} with _id: <code>{bot_id}</code>\n\n"
            f" Source: <code>{user_id}</code>\n"
            f" Media Type: <code>{s['as_doc']}</code>\n"
            f" Caption: <code>{s['captian']}</code>\n"
            f" Thumbnail: <code>{s['thumb']}</code>\n"
            f" Prefix: <code>{s['lprefix']}</code>\n"
            f" Suffix: <code>{s['lsuffix']}</code>\n"
            f" Rename: <code>{s['lremname']}</code>\n"
            f" Leech Dump: <code>{s['ldump']}</code>\n"
            f" Metadata: <code>{s['metadata']}</code>\n\n"
            f"Made By @F2_Bots"
        )
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")


# ── Thumbnail commands ─────────────────────────────────────────────

@new_task
async def add_thumbnail(client, message):
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.reply("⚠️ Reply to a photo to set as thumbnail.")
    bot_id    = bot.me.id
    file_path = await message.reply_to_message.download()
    url       = upload_to_imgbb(file_path)
    os.remove(file_path)
    if not url:
        return await message.reply("❌ Failed to upload thumbnail to imgbb.")
    thumbs.update_one({"_id": bot_id}, {"$set": {"url": url}}, upsert=True)
    await message.reply(f"✅ Thumbnail added!\nURL: {url}")

@new_task
async def show_thumbnail(client, message):
    data = thumbs.find_one({"_id": bot.me.id})
    if not data:
        return await message.reply("❌ No thumbnail found.")
    await message.reply_photo(photo=data['url'], caption=f"📸 Current thumbnail - {data['url']}")

@new_task
async def delete_thumbnail(client, message):
    result = thumbs.delete_one({"_id": bot.me.id})
    if result.deleted_count == 0:
        return await message.reply("❌ No thumbnail to delete.")
    await message.reply("🗑️ Thumbnail deleted.")


# ── Manual scrape ──────────────────────────────────────────────────

@new_task
async def mannual_scrape(client, message):
    if ' ' not in message.text:
        return await message.reply("add any url....")
    get_url = message.text.split(' ')[1]
    tor, mag, title = await sync_to_async(scrape_links, get_url)
    if tor is None:
        return await message.reply("❌ Failed to fetch the page.")
    await message.reply(f"Movie Name: <b><u>{title}</u></b>\n\n - Say Jai BYNF\n Made By @F2_Bots.")
    MT_list = build_mt_list(tor, mag)
    if not MT_list:
        return await message.reply("❌ No torrent or magnet links found on this page.")
    # BUG FIX: mark URL as leeched so the RSS loop doesn't re-leech this same
    # post automatically when it next appears in a feed poll.
    leeched_urls.add(get_url)
    await sync_to_async(mark_leeched_sync, get_url)
    # Also add to pending_posts so we keep watching for additional links added later
    leeched_hrefs = [atl[1] for atl in MT_list]
    link_count    = len(tor) + len(mag)
    pending_posts[get_url] = {
        'title':        title,
        'keyword':      'manual',
        'added_at':     time.time(),
        'link_count':   link_count,
        'leeched':      leeched_hrefs,
        'stable_since': time.time(),
    }
    await leech_mt_list(MT_list)


# ── Status commands ───────────────────────────────────────────────

@new_task
async def feedstatus(client, message):
    """Show health status of all RSS feeds."""
    if not feed_health:
        return await message.reply("No feed data yet — wait for first poll cycle.")

    lines = ["<b>📡 RSS Feed Status:</b>\n"]
    for rss_url, h in feed_health.items():
        keyword  = h.get('keyword', '?')
        status   = h.get('status', 'unknown')
        fails    = h.get('fail_count', 0)
        last_ok  = h.get('last_success')
        last_chk = h.get('last_check')

        if status == 'ok':
            icon = "✅"
        elif status in ('timeout', 'empty'):
            icon = "❌"
        else:
            icon = "⏳"

        last_ok_str  = f"{int((time.time() - last_ok) / 60)}m ago"  if last_ok  else "never"
        last_chk_str = f"{int((time.time() - last_chk) / 60)}m ago" if last_chk else "never"

        lines.append(
            f"{icon} <b>{keyword}</b>\n"
            f"   Status: {status} | Fails: {fails}\n"
            f"   Last OK: {last_ok_str} | Checked: {last_chk_str}\n"
        )

    await message.reply("\n".join(lines))


@new_task
async def queue_status(client, message):
    """Show what's currently in the pending queue."""
    if not pending_posts:
        return await message.reply("✅ Pending queue is empty.")

    now   = time.time()
    lines = [f"<b>⏳ Pending Queue ({len(pending_posts)} posts):</b>\n"]

    for url, meta in pending_posts.items():
        age         = int((now - meta['added_at']) / 60)
        link_count  = meta.get('link_count', 0)
        leeched_cnt = len(meta.get('leeched', []))
        stable_since = meta.get('stable_since', now)
        stable_mins  = int((now - stable_since) / 60)
        title        = meta.get('title', url)[:60]

        lines.append(
            f"📌 <b>{title}</b>\n"
            f"   ⏱ Waiting: {age}m | Links found: {link_count} | Leeched: {leeched_cnt}\n"
            f"   🔒 Stable for: {stable_mins}m / {STABLE_MINUTES}m\n"
            f"   🔗 <a href=\"{url}\">Post</a>\n"
        )

    await message.reply("\n".join(lines), disable_web_page_preview=True)


@new_task
async def leeched_recent(client, message):
    """Show recently leeched posts from DB."""
    try:
        # Get last 10 leeched entries sorted by most recent
        recent = list(leeched_col.find({}, {'url': 1, 'added_at': 1})
                      .sort('added_at', -1).limit(10))

        if not recent:
            return await message.reply("No leeched posts recorded yet.")

        lines = [f"<b>✅ Recently Leeched ({len(recent)} shown):</b>\n"]
        for doc in recent:
            url      = doc.get('url', '')
            added_at = doc.get('added_at')
            if added_at:
                ago = int((datetime.datetime.utcnow() - added_at).total_seconds() / 60)
                time_str = f"{ago}m ago" if ago < 60 else f"{ago // 60}h {ago % 60}m ago"
            else:
                time_str = "unknown"
            # Extract title from URL slug
            slug  = url.split('/')[-2] if url.endswith('/') else url.split('/')[-1]
            title = slug[:60].replace('-', ' ').title()
            lines.append(f"🎬 {title}\n   ⏱ {time_str} | <a href=\"{url}\">Link</a>\n")

        await message.reply("\n".join(lines), disable_web_page_preview=True)

    except Exception as e:
        await message.reply(f"❌ Error fetching leeched posts: {e}")


# ── Help ───────────────────────────────────────────────────────────

@new_task
async def refresh_dump(client, message):
    """Force re-resolve dump channel peer — fixes forwarding stopped without restart."""
    clear_chat_info_cache()
    await message.reply_text(
        "✅ Dump channel peer cache cleared!\n"
        "Next leech will re-resolve the dump channel automatically.\n"
        "No restart needed."
    )


@new_task
async def auto_leech_help(client, message):
    await message.reply_text("""<b>⌬ Auto Leech Commands:
Manual Leech Commands:
- /scrape <i>[url]</i> : to scrape magnet from 1TamilMv or 1TamilBlasters.

Steps To Activate Your Auto Leech:</b>
┠ <b>Step No 1 :</b> Add an <i>AUTO_LEECH_GRP_ID</i> in config file or add in bot settings /bs and then restart the bot.

┠ <b>Step No 2 :</b> Add supported website URL:
   - /setd <i>[keyword] | [Domain]</i>
   - /getd: <i>To get active domain list.</i>
   - Keywords: tmv (1TamilMv), tbl (1TamilBlasters)

┠ <b>Step No 3 :</b> Add caption, prefix, suffix and dump channel etc...
   - /us: <i>Set your leech setting.</i>

┠ <b>Step No 4 :</b> Apply settings to bot.
   - /setlbot: <i>After adding all data in user settings.</i>

┠ <b>Step No 5 :</b> Set thumbnail.
   - /add_thumb: <i>(reply to image) Add thumbnail.</i>
   - /show_thumb: <i>View thumbnail.</i>
   - /del_thumb: <i>Delete thumbnail.</i>

┖ <b>Finally :</b> Restart the bot and wait 5 mins.

<b>Made By @F2_Bots</b>""")


# ── Start workers and register handlers ───────────────────────────

RSS_auto_leecher()
pending_queue_worker()

bot.add_handler(MessageHandler(auto_leech_help,   filters=command("auto_leech")   & CustomFilters.sudo))
bot.add_handler(MessageHandler(clone_ThiruEmpire, filters=command("setlbot")      & CustomFilters.sudo))
bot.add_handler(MessageHandler(setdomain,         filters=command("setd")         & CustomFilters.sudo))
bot.add_handler(MessageHandler(getdomains,        filters=command("getd")         & CustomFilters.sudo))
bot.add_handler(MessageHandler(add_thumbnail,     filters=command("add_thumb")    & CustomFilters.sudo))
bot.add_handler(MessageHandler(show_thumbnail,    filters=command("show_thumb")   & CustomFilters.sudo))
bot.add_handler(MessageHandler(delete_thumbnail,  filters=command("del_thumb")    & CustomFilters.sudo))
bot.add_handler(MessageHandler(mannual_scrape,    filters=command("scrape")       & CustomFilters.sudo))
bot.add_handler(MessageHandler(feedstatus,        filters=command("feedstatus")   & CustomFilters.sudo))
bot.add_handler(MessageHandler(queue_status,      filters=command("queue")        & CustomFilters.sudo))
bot.add_handler(MessageHandler(leeched_recent,    filters=command("leeched")      & CustomFilters.sudo))
bot.add_handler(MessageHandler(refresh_dump,      filters=command("refreshdump")  & CustomFilters.sudo))
