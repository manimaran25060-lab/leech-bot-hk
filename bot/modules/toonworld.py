#!/usr/bin/env python3
"""
ToonWorld4All Anime Scraper Module
Command: /toon <post URL>

Flow:
  1. User pastes a toonworld4all.me post URL
  2. Bot scrapes the post for download links
  3. User picks resolution (720p / 1080p / 480p …)
  4. User picks Full Season or Individual Episode
  5. Bot bypasses URL shorteners → gets direct link
  6. Bot leeches via aria2 → delivers as Telegram file
"""

import re
from asyncio import sleep
from time import time
from html import unescape
from urllib.parse import urlparse, parse_qs, unquote

from cloudscraper import create_scraper
from bs4 import BeautifulSoup
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.filters import command, regex

from bot import bot, LOGGER, bot_cache, DOWNLOAD_DIR
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.message_utils import sendMessage, editMessage
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.ext_utils.bot_utils import new_task, sync_to_async
from bot.helper.mirror_utils.download_utils.aria2_download import add_aria2c_download
from bot.helper.listeners.tasks_listener import MirrorLeechListener

BASE_URL     = "https://toonworld4all.me"
CACHE_TTL    = 300
RES_KEYWORDS = ["2160p", "1080p", "720p", "480p", "360p"]

SHORTENER_DOMAINS = [
    "shrinkme.io","shrinkforearn.in","shrink.pe",
    "droplink.co","drop.download","instasave.in",
    "inshorturl.com","short.ink","shortink.fun",
    "earnl.com","earn4clicks.in","cutpaid.com",
    "cuturl.in","link1s.com","ouo.io","exe.io",
    "exey.io","adfoc.us","linkvertise.com",
    "techywebtech.com","pahewin.com",
    "driveleech.net","driveleech.org",
    "filepress.in","filepress.cc",
    "gdflix.pro","gdflix.top",
]

HELP_MSG = """🎌 <b>ToonWorld4All Downloader</b>

<b>Usage:</b>
<code>/toon https://toonworld4all.me/your-anime-post/</code>

<b>How to get the URL:</b>
1. Open <a href="https://toonworld4all.me">toonworld4all.me</a> in your browser
2. Search for your anime
3. Open the post
4. Copy the URL from address bar
5. Send it with /toon

<b>Example:</b>
<code>/toon https://toonworld4all.me/naruto-shippuden-hindi/</code>"""

# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _scraper():
    return create_scraper(browser={"browser":"chrome","platform":"windows","mobile":False})

def _headers():
    return {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

def _is_toonworld_url(url):
    try:
        host = urlparse(url).hostname or ""
        return host in ("toonworld4all.me","www.toonworld4all.me")
    except Exception:
        return False

def _is_shortener(url):
    host = urlparse(url).hostname or ""
    return any(s in host for s in SHORTENER_DOMAINS)

def _is_noise_url(url):
    noise = ["facebook","twitter","telegram.me","t.me","instagram",
             "whatsapp","youtube.com/channel","toonworld4all.me/category",
             "toonworld4all.me/tag","toonworld4all.me/page",
             "toonworld4all.me/#","toonworld4all.me/?"]
    return any(n in url.lower() for n in noise)

def _res_from_text(text):
    for res in RES_KEYWORDS:
        if res.lower() in text.lower():
            return res
    return None

def _key(user_id, msg_id):
    return f"tw_{user_id}_{msg_id}"

def _get_cache(user_id, msg_id):
    data = bot_cache.get(_key(user_id, msg_id))
    if not data or time() - data.get("ts",0) > CACHE_TTL:
        return None
    return data

# ──────────────────────────────────────────────────────
# Post scraper
# ──────────────────────────────────────────────────────

def _fetch_post(url):
    try:
        r = _scraper().get(url, headers=_headers(), timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser"), r.url
    except Exception as e:
        LOGGER.error(f"[ToonWorld] Fetch error: {e}")
        return None, url

def _extract_title(soup):
    for sel in ["h1.entry-title","h1.post-title","h1","title"]:
        tag = soup.select_one(sel)
        if tag:
            t = tag.get_text(strip=True)
            t = re.sub(r"\s*[-–|].*?(toonworld|download).*$","",t,flags=re.I).strip()
            if t:
                return t
    return "Anime"

def _extract_links(soup):
    groups = {}
    current_res = "Unknown"
    for tag in soup.find_all(["h2","h3","h4","p","strong","span","div","a"]):
        text = tag.get_text(" ", strip=True)
        d = _res_from_text(text)
        if d:
            current_res = d
        if tag.name != "a":
            continue
        href = tag.get("href","").strip()
        if not href or not href.startswith("http"):
            continue
        if _is_noise_url(href) or _is_toonworld_url(href):
            continue
        bucket = _res_from_text(text) or _res_from_text(href) or current_res
        groups.setdefault(bucket,[])
        if href not in groups[bucket]:
            groups[bucket].append(href)
    return {k:v for k,v in groups.items() if v}

# ──────────────────────────────────────────────────────
# URL Shortener Bypass
# ──────────────────────────────────────────────────────

def _bypass_shortener(url):
    scraper = _scraper()
    h = {**_headers(), "Referer": BASE_URL}
    current = url
    for attempt in range(6):
        try:
            r = scraper.get(current, headers=h, timeout=20, allow_redirects=True)
            final_url = r.url
            if final_url != current and not _is_shortener(final_url):
                return final_url
            soup = BeautifulSoup(r.text, "html.parser")
            # meta refresh
            meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh",re.I)})
            if meta:
                m = re.search(r"url=(.+)", meta.get("content",""), re.I)
                if m:
                    nx = m.group(1).strip().strip("'\"")
                    if nx and nx != current:
                        current = nx
                        continue
            # hidden form POST
            form = soup.find("form")
            if form:
                action  = form.get("action", current)
                payload = {i.get("name"):i.get("value","")
                           for i in form.find_all("input") if i.get("name")}
                if payload:
                    r2 = scraper.post(action, data=payload,
                                      headers={**h,"Referer":current},
                                      timeout=20, allow_redirects=True)
                    if r2.url and r2.url != current and not _is_shortener(r2.url):
                        return r2.url
                    soup    = BeautifulSoup(r2.text, "html.parser")
                    current = r2.url
            # "Get Link" anchor
            for a in soup.select("a[href]"):
                href  = a.get("href","")
                label = a.get_text(strip=True).lower()
                if not href.startswith("http") or _is_noise_url(href):
                    continue
                if any(kw in label for kw in ["get link","download","direct","click here","proceed"]):
                    if not _is_shortener(href):
                        return href
                    current = href
                    break
            else:
                return final_url
        except Exception as e:
            LOGGER.warning(f"[ToonWorld] Bypass step {attempt+1}: {e}")
            break
    return current

def _resolve_link(url):
    resolved = _bypass_shortener(url) if _is_shortener(url) else url
    try:
        from bot.helper.mirror_utils.download_utils.direct_link_generator import direct_link_generator
        from bot.helper.ext_utils.exceptions import DirectDownloadLinkException
        result = direct_link_generator(resolved)
        return result[0] if isinstance(result, tuple) else result
    except Exception:
        pass
    return resolved

def _group_by_season(links):
    seasons = {}
    for url in links:
        m   = re.search(r'[Ss](?:eason[\s_-]?)?(\d{1,2})', url)
        key = f"Season {int(m.group(1))}" if m else "All Episodes"
        seasons.setdefault(key,[])
        seasons[key].append(url)
    return seasons

# ──────────────────────────────────────────────────────
# Step 1 – /toon command
# ──────────────────────────────────────────────────────

@new_task
async def toon_cmd(_, message):
    parts = message.text.split(maxsplit=1)

    # No argument — show help
    if len(parts) < 2:
        return await sendMessage(message, HELP_MSG)

    arg = parts[1].strip()

    # Must be a toonworld4all.me URL
    if not arg.startswith("http") or not _is_toonworld_url(arg):
        return await sendMessage(message,
            f"⚠️ Please send a valid <b>toonworld4all.me</b> URL.\n\n{HELP_MSG}")

    status = await sendMessage(message, f"⏳ Fetching post…\n<code>{arg}</code>")
    soup, final_url = await sync_to_async(_fetch_post, arg)

    if not soup:
        return await editMessage(status,
            "❌ <b>Failed to load the post.</b>\n"
            "The site may be down or blocking the request.\n"
            f"<a href='{arg}'>Try opening manually</a>")

    link_groups = await sync_to_async(_extract_links, soup)
    if not link_groups:
        return await editMessage(status,
            f"⚠️ <b>No download links found in this post.</b>\n"
            f"<a href='{final_url}'>Open post manually</a>")

    title = await sync_to_async(_extract_title, soup)

    user_id = message.from_user.id
    msg_id  = status.id
    bot_cache[_key(user_id, msg_id)] = {
        "ts": time(), "post_url": final_url,
        "post_title": title, "link_groups": link_groups,
        "step": "select_res",
    }

    ordered  = [r for r in RES_KEYWORDS if r in link_groups]
    ordered += [r for r in link_groups  if r not in RES_KEYWORDS]

    buttons = ButtonMaker()
    for res in ordered:
        n = len(link_groups[res])
        buttons.ibutton(f"📺 {res}  ({n} link{'s' if n>1 else ''})",
                        f"tw {user_id} {msg_id} res {res}")
    buttons.ibutton("❌ Cancel", f"tw {user_id} {msg_id} cancel")

    await editMessage(status,
        f"🎌 <b>{title}</b>\n"
        f"🔗 <a href='{final_url}'>View Post</a>\n\n"
        "📺 <b>Select Resolution:</b>",
        buttons.build_menu(2))

# ──────────────────────────────────────────────────────
# Step 2 – resolution selected
# ──────────────────────────────────────────────────────

async def _handle_res_select(cb, user_id, msg_id, res):
    data = _get_cache(user_id, msg_id)
    if not data:
        return await cb.message.edit("⏰ Session expired. Send the URL again.")
    links = data["link_groups"].get(res,[])
    if not links:
        return await cb.message.edit("❌ No links for this resolution.")
    seasons = _group_by_season(links)
    data.update({"chosen_res":res,"seasons":seasons,"step":"select_mode","ts":time()})

    buttons = ButtonMaker()
    for sname, slinks in seasons.items():
        buttons.ibutton(f"📦 {sname}  ({len(slinks)} ep)",
                        f"tw {user_id} {msg_id} season {sname.replace(' ','_')}")
    buttons.ibutton("🎞 Pick Individual Episode", f"tw {user_id} {msg_id} individual")
    buttons.ibutton("🔙 Back", f"tw {user_id} {msg_id} back_res")
    buttons.ibutton("❌ Cancel", f"tw {user_id} {msg_id} cancel")

    lines = "\n".join(f"  • <b>{s}</b> — {len(v)} ep" for s,v in seasons.items())
    await cb.message.edit(
        f"🎌 <b>{data['post_title']}</b>\n📺 Resolution: <b>{res}</b>\n\n"
        f"📋 Found:\n{lines}\n\nChoose download mode 👇",
        buttons.build_menu(1))

# ──────────────────────────────────────────────────────
# Step 3a – full season
# ──────────────────────────────────────────────────────

async def _handle_season(cb, user_id, msg_id, season_key):
    data = _get_cache(user_id, msg_id)
    if not data:
        return await cb.message.edit("⏰ Session expired.")
    season_name = season_key.replace("_"," ")
    links = data["seasons"].get(season_name,[])
    if not links:
        return await cb.message.edit(f"❌ No links for {season_name}.")
    await cb.message.edit(
        f"⏳ Bypassing & queuing <b>{len(links)}</b> file(s)…\n"
        f"🎌 {data['post_title']} | 📺 {data['chosen_res']} | 📦 {season_name}")
    await _start_leech(cb.message, user_id, links,
                       data["post_title"], data["chosen_res"], season_name)
    bot_cache.pop(_key(user_id, msg_id), None)

# ──────────────────────────────────────────────────────
# Step 3b – individual episode list
# ──────────────────────────────────────────────────────

async def _handle_individual(cb, user_id, msg_id):
    data = _get_cache(user_id, msg_id)
    if not data:
        return await cb.message.edit("⏰ Session expired.")
    episodes = []
    for sname, slinks in data["seasons"].items():
        for i, url in enumerate(slinks, 1):
            episodes.append((f"{sname} Ep {i}", url))
    data.update({"episodes":episodes,"step":"select_ep","ts":time()})

    buttons = ButtonMaker()
    for i, (label, _) in enumerate(episodes):
        buttons.ibutton(f"▶ {label[:35]}", f"tw {user_id} {msg_id} ep {i}")
    buttons.ibutton("🔙 Back",   f"tw {user_id} {msg_id} back_mode")
    buttons.ibutton("❌ Cancel", f"tw {user_id} {msg_id} cancel")
    await cb.message.edit(
        f"🎌 <b>{data['post_title']}</b> | 📺 <b>{data['chosen_res']}</b>\n\n"
        "🎞 Select an episode 👇", buttons.build_menu(2))

# ──────────────────────────────────────────────────────
# Step 3c – single episode
# ──────────────────────────────────────────────────────

async def _handle_ep(cb, user_id, msg_id, ep_idx):
    data = _get_cache(user_id, msg_id)
    if not data:
        return await cb.message.edit("⏰ Session expired.")
    episodes = data.get("episodes",[])
    idx = int(ep_idx)
    if idx >= len(episodes):
        return await cb.message.edit("❌ Invalid episode.")
    label, url = episodes[idx]
    await cb.message.edit(f"⏳ Bypassing link for <b>{label}</b>…")
    await _start_leech(cb.message, user_id, [url],
                       data["post_title"], data["chosen_res"], label)
    bot_cache.pop(_key(user_id, msg_id), None)

# ──────────────────────────────────────────────────────
# Core leech launcher
# ──────────────────────────────────────────────────────

async def _start_leech(message, user_id, raw_links, title, res, label):
    tag    = f"@{message.chat.username or user_id}"
    failed = []
    for i, raw_url in enumerate(raw_links, 1):
        try:
            await message.edit(
                f"⏳ <b>[{i}/{len(raw_links)}] Bypassing shortener…</b>\n"
                f"🎌 {title} | 📺 {res} | {label}")
        except Exception:
            pass
        direct = await sync_to_async(_resolve_link, raw_url)
        LOGGER.info(f"[ToonWorld] {raw_url} → {direct}")
        safe = re.sub(r"[^\w\-.]","_",f"{title}_{res}_{label}_ep{i}")
        path = f"{DOWNLOAD_DIR}{user_id}/toon/"
        try:
            listener = MirrorLeechListener(message, isLeech=True, tag=tag, sameDir={})
            await add_aria2c_download(direct, path, listener, safe,
                                      header=None, ratio=None, seed_time=None)
        except Exception as e:
            LOGGER.error(f"[ToonWorld] Leech failed: {e}")
            failed.append(raw_url)
        await sleep(1)
    if failed:
        fail_lines = "\n".join(f"• <code>{u}</code>" for u in failed[:5])
        await sendMessage(message,
            f"⚠️ <b>{len(failed)} link(s) failed:</b>\n{fail_lines}")

# ──────────────────────────────────────────────────────
# Callback router
# ──────────────────────────────────────────────────────

@new_task
async def toon_callback(_, cb):
    parts = cb.data.split()
    if len(parts) < 4:
        return await cb.answer("Invalid callback.", show_alert=True)
    _, cb_uid, cb_mid, action, *params = parts
    cb_uid = int(cb_uid)
    cb_mid = int(cb_mid)
    if cb.from_user.id != cb_uid:
        return await cb.answer("⛔ Not your session!", show_alert=True)
    await cb.answer()

    if action == "cancel":
        bot_cache.pop(_key(cb_uid, cb_mid), None)
        return await cb.message.edit("❌ <b>Cancelled.</b>")

    elif action == "res":
        await _handle_res_select(cb, cb_uid, cb_mid, params[0])

    elif action == "season":
        await _handle_season(cb, cb_uid, cb_mid, params[0])

    elif action == "individual":
        await _handle_individual(cb, cb_uid, cb_mid)

    elif action == "ep":
        await _handle_ep(cb, cb_uid, cb_mid, params[0])

    elif action == "back_res":
        data = _get_cache(cb_uid, cb_mid)
        if not data:
            return await cb.message.edit("⏰ Session expired.")
        lg      = data.get("link_groups",{})
        ordered  = [r for r in RES_KEYWORDS if r in lg]
        ordered += [r for r in lg if r not in RES_KEYWORDS]
        buttons  = ButtonMaker()
        for res in ordered:
            n = len(lg[res])
            buttons.ibutton(f"📺 {res}  ({n} link{'s' if n>1 else ''})",
                            f"tw {cb_uid} {cb_mid} res {res}")
        buttons.ibutton("❌ Cancel", f"tw {cb_uid} {cb_mid} cancel")
        await cb.message.edit(
            f"🎌 <b>{data['post_title']}</b>\n"
            f"🔗 <a href='{data['post_url']}'>View Post</a>\n\n📺 <b>Select Resolution:</b>",
            buttons.build_menu(2))

    elif action == "back_mode":
        data = _get_cache(cb_uid, cb_mid)
        if not data:
            return await cb.message.edit("⏰ Session expired.")
        seasons = data.get("seasons",{})
        res     = data.get("chosen_res","?")
        buttons = ButtonMaker()
        for sname, slinks in seasons.items():
            buttons.ibutton(f"📦 {sname}  ({len(slinks)} ep)",
                            f"tw {cb_uid} {cb_mid} season {sname.replace(' ','_')}")
        buttons.ibutton("🎞 Pick Individual Episode", f"tw {cb_uid} {cb_mid} individual")
        buttons.ibutton("🔙 Back",   f"tw {cb_uid} {cb_mid} back_res")
        buttons.ibutton("❌ Cancel", f"tw {cb_uid} {cb_mid} cancel")
        lines = "\n".join(f"  • <b>{s}</b> — {len(v)} ep" for s,v in seasons.items())
        await cb.message.edit(
            f"🎌 <b>{data['post_title']}</b>\n📺 Resolution: <b>{res}</b>\n\n"
            f"📋 Found:\n{lines}\n\nChoose download mode 👇",
            buttons.build_menu(1))

# ──────────────────────────────────────────────────────
# Register handlers
# ──────────────────────────────────────────────────────

bot.add_handler(MessageHandler(
    toon_cmd,
    filters=command(BotCommands.ToonCommand) & CustomFilters.authorized))

bot.add_handler(CallbackQueryHandler(
    toon_callback,
    filters=regex(r"^tw\s")))
