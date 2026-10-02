# AnimeSalt.ac Scraper Module — v4
# Site: https://animesalt.ac
#
# HOW THE SITE WORKS (reverse-engineered from DevTools):
#
#   PRIMARY LINKS (quality-tagged):
#   - Download button: <a class="aa-mdl" data-mdl="mdl-download"> — JS modal toggle
#   - Modal div:       <div class="mdl-cn anm-b" id="mdl-download"> — IN PAGE HTML
#   - Download links:  <a href="https://animesalt.ac/?trdownload=1&t=ser&trid=XXXXX">
#   - Those URLs redirect to actual Mega/Drive/external link (HTTP 302)
#
#   FALLBACK LINKS (language-tagged, when Mega is blocked):
#   - Second iframe: <iframe src="https://animesalt.ac/multi-lang-plyr/player.php?data=<base64>">
#   - base64 decodes to: [{"language":"Hindi","link":"https://short.icu/XXXXX"}, ...]
#   - short.icu links redirect to Mega/GDrive/etc per language
#   - Used automatically when primary links fail or user picks language picker
#
# DOWNLOAD FLOW:
#   /animesalt <n>
#     → pick anime → pick mode (single ep / full season)
#     → pick season → pick episode (or full season)
#     → [links fetched] → pick quality (480p / 720p / 1080p / All)
#                      OR → 🌐 Language Downloads (fallback if Mega blocked)
#     → bot auto-triggers /leech <url>


import os
import re
import json
import base64
import asyncio
from asyncio import sleep
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

from pyrogram import Client
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.filters import command, regex
from pyrogram.types import Message

from bot import bot, LOGGER, CMD_SUFFIX
from bot.helper.telegram_helper.message_utils import sendMessage, editMessage
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL  = "https://animesalt.ac"
LEECH_CMD = os.environ.get("ANIMESALT_LEECH_CMD", "qbleech").strip("/")

# In-memory session state  {user_id: {...}}
_sessions: dict = {}

# ── Proxy ──────────────────────────────────────────────────────────────────────

_PROXY_URL = (
    os.environ.get("ANIMESALR_PROXY")
    or os.environ.get("WEBSHARE_PROXY")
    or None
)
_PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None

_scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)
if _PROXIES:
    _scraper.proxies.update(_PROXIES)
    LOGGER.info(f"[AnimeSalt] Proxy active: {_PROXY_URL[:40]}...")
else:
    LOGGER.warning("[AnimeSalt] No proxy set — Oracle IPs may be blocked by Cloudflare")


# ══════════════════════════════════════════════════════════════════════════════
# QUALITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# Display labels for quality keys
QUALITY_LABELS = {
    "480":  "📱 480p  (SD)",
    "720":  "💻 720p  (HD)",
    "1080": "🖥  1080p (FHD)",
    "2160": "📺 2160p (4K)",
    "all":  "📦 All Qualities",
}

# Source tags that imply a quality when no explicit resolution is given
_SOURCE_QUALITY_HINTS = {
    "4k": "2160", "uhd": "2160", "2160p": "2160",
    "1080p": "1080", "fhd": "1080",
    "bluray": "1080", "blu-ray": "1080", "bdrip": "1080", "brrip": "1080",
    "web-dl": "1080", "webdl": "1080", "webrip": "1080",
    "720p": "720", "hd": "720",
    "480p": "480", "sd": "480", "dvdrip": "480", "hdtv": "480",
    "360p": "480",
}


def parse_quality(text: str) -> str:
    """
    Extract a normalised quality key from ANY label / filename / description.

    Examples handled:
      "480p"                           → "480"
      "[1080p BDRip - x264 - Tamil]"  → "1080"
      "Multi-Audio 720p"               → "720"
      "BluRay"                         → "1080"
      "WEB-DL"                         → "1080"
      "4K HDR"                         → "2160"
      "HD"                             → "720"

    Returns one of: '480', '720', '1080', '2160', '' (unknown)
    """
    if not text:
        return ""
    t = text.lower()

    # Explicit resolution first (most reliable)
    m = re.search(r"\b(2160|1080|720|480|360|4320)\s*p\b", t)
    if m:
        r = int(m.group(1))
        if r >= 2160: return "2160"
        if r >= 1080: return "1080"
        if r >= 720:  return "720"
        return "480"

    # 4K shorthand
    if re.search(r"\b4k\b", t):
        return "2160"

    # Source/codec hints (longest match first)
    for token, quality in sorted(_SOURCE_QUALITY_HINTS.items(), key=lambda x: -len(x[0])):
        if token in t:
            return quality

    return ""


def _get_link_quality(lnk: dict) -> str:
    """
    Get the normalised quality key ("480", "720", "1080", "2160") from a link.

    Handles every storage format:
      "480"     → "480"  (already a key)
      "480p"    → "480"  (old format with 'p')
      "1080p BDRip" → "1080"  (verbose quality string)
      ""        → ""     (unknown — position fallback will handle it)
    """
    _KEYS = {"480", "720", "1080", "2160"}

    for field in ("quality", "audio"):
        v = lnk.get(field, "").strip()
        if not v:
            continue
        if v in _KEYS:           # already a bare key
            return v
        # Look for bare resolution number (covers "480p", "720p", "1080p BDRip")
        m = re.search(r"\b(2160|1080|720|480)\b", v)
        if m:
            return m.group(1)

    return ""


def filter_links_by_quality(links: list[dict], quality_key: str) -> list[dict]:
    """
    Filter a list of link dicts by quality key.
    'all' returns every link unchanged.
    Links with unknown quality are excluded from specific quality filters.
    """
    if quality_key == "all" or not quality_key:
        return links

    return [lnk for lnk in links if _get_link_quality(lnk) == quality_key]


def group_links_by_quality(links: list[dict]) -> dict[str, list]:
    """Group links into {quality_key: [link, ...]} buckets."""
    groups: dict[str, list] = {}
    for lnk in links:
        q = _get_link_quality(lnk)
        groups.setdefault(q or "", []).append(lnk)
    return groups


def _available_quality_keys(links: list[dict]) -> list[str]:
    """Return sorted quality keys present in the link list."""
    seen = {_get_link_quality(lnk) for lnk in links} - {""}
    return [k for k in ("480", "720", "1080", "2160") if k in seen]


def _quality_buttons(user_id: int, cb_prefix: str,
                     available: list[str] | None = None) -> ButtonMaker:
    """
    Build an inline keyboard with quality choices.
    available = list of quality keys actually present; None = show all.
    Always adds 'All Qualities'.
    """
    keys = available if available else ["480", "720", "1080", "2160"]
    buttons = ButtonMaker()
    for k in keys:
        label = QUALITY_LABELS.get(k, f"{k}p")
        buttons.ibutton(label, f"{cb_prefix}{user_id}_{k}")
    buttons.ibutton(QUALITY_LABELS["all"], f"{cb_prefix}{user_id}_all")
    buttons.ibutton("❌ Cancel", f"as_cancel_{user_id}")
    return buttons


# ══════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get(url: str, params=None, timeout: int = 30, allow_redirects: bool = True):
    try:
        r = _scraper.get(url, params=params, timeout=timeout,
                         allow_redirects=allow_redirects)
        LOGGER.info(f"[AnimeSalt] {r.status_code} {url[:90]}")
        return r
    except Exception as e:
        LOGGER.error(f"[AnimeSalt] GET failed {url[:80]}: {e}")
        return None


def _get_text(url: str, params=None, timeout: int = 30) -> str | None:
    r = _get(url, params=params, timeout=timeout)
    return r.text if r and r.status_code < 400 else None


async def _fetch(url: str, params=None, timeout: int = 30) -> str | None:
    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, lambda: _get_text(url, params, timeout)
            ),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        LOGGER.error(f"[AnimeSalt] Timeout: {url[:80]}")
        return None
    except Exception as e:
        LOGGER.error(f"[AnimeSalt] _fetch error: {e}")
        return None


async def _resolve_redirect(url: str, timeout: int = 15) -> str:
    """Follow a ?trdownload=1 URL → final real download URL."""
    def _do():
        try:
            r = _scraper.get(url, timeout=timeout, allow_redirects=True)
            final = r.url
            if final == url and r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                meta = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
                if meta:
                    m = re.search(r"url=(.+)", meta.get("content", ""), re.I)
                    if m:
                        return m.group(1).strip().strip("'\"")
                for script in soup.find_all("script"):
                    m = re.search(
                        r'(?:window\.location|location\.href)\s*=\s*["\']([^"\']+)["\']',
                        script.get_text()
                    )
                    if m:
                        return m.group(1)
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if any(x in href for x in ["mega.", "drive.google", "mediafire."]):
                        return href
            return final
        except Exception as e:
            LOGGER.error(f"[AnimeSalt] redirect resolve failed: {e}")
            return url

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _do),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        return url


# ══════════════════════════════════════════════════════════════════════════════
# LINK EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _server_from_url(url: str) -> str:
    if "mega."       in url: return "Mega"
    if "drive.goog"  in url: return "GDrive"
    if "mediafire."  in url: return "MediaFire"
    if "1fichier."   in url: return "1Fichier"
    if "gofile."     in url: return "GoFile"
    if "pixeldrain." in url: return "PixelDrain"
    if "buzzheavier."in url: return "BuzzHeavier"
    if "krakenfiles." in url: return "KrakenFiles"
    return urlparse(url).netloc.replace("www.", "") or "Direct"


def _is_mega(lnk: dict) -> bool:
    """Return True if this link resolves to Mega (always blocked on Oracle IPs)."""
    url = lnk.get("url", lnk.get("tracker_url", ""))
    return "mega." in url.lower()


def _filter_mega(links: list[dict]) -> list[dict]:
    """Remove Mega links. Used when multilang fallback is available."""
    return [l for l in links if not _is_mega(l)]


def _mega_is_blocked(url: str, timeout: int = 10) -> bool:
    """
    Quick pre-check: is this Mega link accessible from the server IP?
    Mega returns a JSON with 'e':-9 (EBLOCKED) for IP-blocked requests.
    Returns True if blocked/unreachable, False if accessible.
    """
    try:
        import re as _re
        # Extract the Mega file ID from the URL
        # Handles: mega.nz/file/XXXXX#KEY and mega.nz/#!XXXXX!KEY
        m = _re.search(r'mega\.nz/(?:file/|#!?)([A-Za-z0-9_-]{8,})', url)
        if not m:
            return False  # unknown format — let it try
        file_id = m.group(1).split('!')[0].split('#')[0]

        # Query Mega API cs endpoint
        import json as _json
        r = _scraper.post(
            "https://g.api.mega.co.nz/cs",
            json=[{"a": "g", "p": file_id}],
            timeout=timeout,
        )
        data = r.json()
        # data is a list; first element has 'e' key if error
        if isinstance(data, list) and data:
            err = data[0]
            if isinstance(err, dict) and err.get("e") in (-9, -16, -18):
                # -9 = EBLOCKED, -16 = ETEMPUNAVAIL, -18 = EOVERQUOTA
                LOGGER.warning(
                    f"[AnimeSalt] Mega pre-check BLOCKED (e={err.get('e')}): "
                    f"{url[:60]}"
                )
                return True
            if isinstance(err, int) and err in (-9, -16, -18):
                LOGGER.warning(
                    f"[AnimeSalt] Mega pre-check BLOCKED (code={err}): "
                    f"{url[:60]}"
                )
                return True
        LOGGER.info(f"[AnimeSalt] Mega pre-check OK: {url[:60]}")
        return False
    except Exception as e:
        LOGGER.warning(f"[AnimeSalt] Mega pre-check error: {e} — assuming accessible")
        return False  # on error, let Mega try anyway


async def _async_mega_is_blocked(url: str) -> bool:
    """Async wrapper for _mega_is_blocked."""
    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, lambda: _mega_is_blocked(url)
            ),
            timeout=15,
        )
    except asyncio.TimeoutError:
        return False


def _extract_audio(text: str) -> str:
    t = text.lower()
    if "multi"  in t: return "Multi-Audio"
    if "dual"   in t: return "Dual-Audio"
    if "hindi"  in t: return "Hindi"
    if "tamil"  in t: return "Tamil"
    if "telugu" in t: return "Telugu"
    if "dub"    in t: return "Dubbed"
    if "sub"    in t: return "Subbed"
    return ""


def _is_trdownload(href: str) -> bool:
    return bool(href and (
        "trdownload=1" in href
        or "t=ser" in href
        or re.search(r"[?&]trid=\d+", href)
        or re.search(r"[?&]data=[a-f0-9]{8,}", href)
    ))


def _is_external_dl(href: str) -> bool:
    return bool(href and any(x in href for x in [
        "mega.", "drive.google", "mediafire.", "1fichier.",
        "gofile.", "pixeldrain.", "buzzheavier.", "krakenfiles.",
        "hxfile.", "anonfiles.", "zippyshare.",
    ]))


def _row_context(tr) -> str:
    """
    Build quality/audio context for an <a> tag's row by collecting text from
    the row itself AND the preceding sibling rows (up to 5).

    AnimeSalt table structure (one pattern OR three separate tables):
      <tr><td>Multi-Audio</td></tr>   ← audio row
      <tr><td>480p</td></tr>          ← quality row
      <tr><td><a href="...">Download</a></td></tr>   ← link row  ← we are here

    Walking UP from the link row catches the quality row above it.
    """
    if tr is None:
        return ""
    parts = [tr.get_text(" ", strip=True)]
    for prev in tr.find_previous_siblings("tr", limit=5):
        parts.append(prev.get_text(" ", strip=True))
    return " ".join(parts)


def _assign_quality_by_position(links: list[dict]) -> list[dict]:
    """
    Position-based quality assignment for AnimeSalt.

    The site ALWAYS returns links in the order: 480p → 720p → 1080p.
    If ALL links have unknown quality (empty string), assign by index.
    This is the final fallback for when HTML parsing cannot extract quality.
    """
    _ORDER = ["480", "720", "1080", "2160"]
    all_unknown = all(not lnk.get("quality") for lnk in links)
    if all_unknown and 1 < len(links) <= len(_ORDER):
        for i, lnk in enumerate(links):
            lnk["quality"] = _ORDER[i]
        LOGGER.info(f"[AnimeSalt] Quality assigned by position: {[l['quality'] for l in links]}")
    return links


def _parse_download_links_from_html(html: str) -> list[dict]:
    """
    Parse AnimeSalt's hidden download modal from page HTML.

    AnimeSalt puts download links inside a JS-toggled modal that is ALREADY
    present in the page HTML (no XHR needed).  The modal can have either:

      Pattern A — 3 separate <table> elements (one per quality):
        <table>  ← 480p table
          <tr><td>Multi-Audio</td></tr>
          <tr><td>480p</td></tr>
          <tr><td><a href="/?trdownload=...">Download</a></td></tr>
        </table>
        ... (repeated for 720p, 1080p)

      Pattern B — 1 <table> with 3 link-rows, each preceded by quality rows:
        <tr><td>Multi-Audio</td><td>480p</td></tr>
        <tr><td><a href="...">Download</a></td></tr>
        <tr><td>Multi-Audio</td><td>720p</td></tr>
        <tr><td><a href="...">Download</a></td></tr>
        ...

    Strategy:
      1. Per-link: scan up to 5 sibling rows above the link's <tr> for quality
      2. Table-level fallback: use all <td> text in the table
      3. Position fallback: if quality still unknown, assign 480/720/1080 by order
    """
    soup    = BeautifulSoup(html, "html.parser")
    results = []

    # ── Primary: .download-links section ─────────────────────────────────────
    dl_sections = soup.select(
        "#mdl-download .download-links, "
        ".mdl-cn .download-links, "
        ".download-links"
    )

    for section in dl_sections:
        tables = section.find_all("table") or [section]
        for table in tables:
            # Table-level context (fallback if row-level gives nothing)
            all_td_text = " ".join(
                td.get_text(" ", strip=True)
                for td in table.find_all("td")
            )
            tbl_quality = parse_quality(all_td_text)
            tbl_audio   = _extract_audio(all_td_text)

            for a in table.find_all("a", href=True):
                href = a.get("href", "").strip()
                if not href:
                    continue
                if href.startswith("/") or not href.startswith("http"):
                    href = urljoin(BASE_URL, href)
                if not (_is_trdownload(href) or _is_external_dl(href)):
                    continue

                # Walk UP sibling rows to find this link's quality/audio
                tr      = a.find_parent("tr")
                row_ctx = _row_context(tr)
                q  = parse_quality(row_ctx) or tbl_quality
                au = _extract_audio(row_ctx) or tbl_audio

                results.append({
                    "tracker_url": href,
                    "quality": q,
                    "audio":   au,
                    "server":  _server_from_url(href) if _is_external_dl(href) else "?",
                })

    if results:
        return results

    # ── Fallback: any matching <a> anywhere on the page ──────────────────────
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if href.startswith("/") or not href.startswith("http"):
            href = urljoin(BASE_URL, href)
        if not (_is_trdownload(href) or _is_external_dl(href)):
            continue
        tr  = a.find_parent("tr")
        ctx = _row_context(tr) if tr else ""
        if not ctx:
            for parent in a.parents:
                if parent.name in ("tr", "li", "div", "td"):
                    ctx = parent.get_text(" ", strip=True)
                    break
        results.append({
            "tracker_url": href,
            "quality": parse_quality(ctx),
            "audio":   _extract_audio(ctx),
            "server":  _server_from_url(href) if _is_external_dl(href) else "?",
        })

    return results


def _extract_multilang_links(html: str) -> list[dict]:
    """
    Extract language-labelled download links from the multi-lang-plyr iframe.

    AnimeSalt embeds a SECOND iframe alongside the CDN player:
      <iframe src="https://animesalt.ac/multi-lang-plyr/player.php?data=<base64>">

    The base64 `data` param decodes to JSON like:
      [{"language":"Hindi",    "link":"https://short.icu/S8ZofkR9_"},
       {"language":"Tamil",    "link":"https://short.icu/6ylxO6qWC"},
       {"language":"Telugu",   "link":"https://short.icu/YOgUAbEIg"},
       {"language":"English",  "link":"https://short.icu/G7JJrbOhL"},
       {"language":"Japanese", "link":"https://short.icu/DhQ01O3JN"}]

    These short.icu URLs are shortlinks that redirect to actual Mega/GDrive/etc.
    Used as FALLBACK when the primary trdownload Mega links are blocked.
    """
    soup    = BeautifulSoup(html, "html.parser")
    results = []

    for iframe in soup.find_all("iframe", src=True):
        src = iframe.get("src", "")
        if "multi-lang-plyr" not in src and "player.php" not in src:
            continue
        m = re.search(r"[?&]data=([A-Za-z0-9+/=_-]+)", src)
        if not m:
            continue
        try:
            b64 = m.group(1)
            # Pad to multiple of 4
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64).decode("utf-8")
            items   = json.loads(decoded)
            if not isinstance(items, list):
                continue
            for item in items:
                lang = item.get("language", "").strip()
                link = item.get("link",     "").strip()
                if not link:
                    continue
                results.append({
                    "tracker_url": link,
                    "quality":     "",
                    "audio":       lang,
                    "server":      "?",
                    "source":      "multilang",
                })
            if results:
                LOGGER.info(
                    f"[AnimeSalt] multilang-plyr: {len(results)} links "
                    f"({[r['audio'] for r in results]})"
                )
        except Exception as e:
            LOGGER.warning(f"[AnimeSalt] multilang-plyr decode error: {e}")

    return results


def _extract_cdn_hash(html: str) -> str | None:
    """
    Extract the 32-char video hash from the as-cdn21.top iframe src in
    the AnimeSalt episode page HTML.

    The hash is used to call the CDN API to get the real m3u8 URL.
    Returns the hash string or None if not found.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Method 1: direct iframe src
    for iframe in soup.find_all("iframe", src=True):
        src = iframe.get("src", "")
        m = re.search(r"as-cdn\d+\.top/video/([a-f0-9]{32})", src)
        if m:
            LOGGER.info(f"[AnimeSalt] CDN hash found (iframe): {m.group(1)}")
            return m.group(1)

    # Method 2: data-src attribute
    for tag in soup.find_all(attrs={"data-src": True}):
        src = tag.get("data-src", "")
        m = re.search(r"as-cdn\d+\.top/video/([a-f0-9]{32})", src)
        if m:
            LOGGER.info(f"[AnimeSalt] CDN hash found (data-src): {m.group(1)}")
            return m.group(1)

    # Method 3: scan raw HTML (widest net — catches JS variables too)
    m = re.search(r"as-cdn\d+\.top/video/([a-f0-9]{32})", html)
    if m:
        LOGGER.info(f"[AnimeSalt] CDN hash found (raw HTML): {m.group(1)}")
        return m.group(1)

    LOGGER.warning("[AnimeSalt] CDN hash not found in page HTML")
    return None


async def _resolve_cdn_m3u8(cdn_hash: str, timeout: int = 20) -> str | None:
    """
    Resolve a CDN video hash to a signed master.m3u8 URL.

    Flow (reverse-engineered from Network DevTools):
      1. GET  https://as-cdn21.top/video/{hash}
             → sets session cookie: fireplayer_player=<token>
      2. POST https://as-cdn21.top/player/index.php?data={hash}&do=getVideo
             headers: X-Requested-With: XMLHttpRequest
             form:    hash={hash}&r=
             cookie:  fireplayer_player=<token>  (from step 1)
      3. JSON response contains:
             videoSource: "https://as-cdn21.top/cdn/hls/{token}/master.m3u8?md5=...&expires=..."

    Returns the signed m3u8 URL or None on failure.
    """
    CDN_BASE   = "https://as-cdn21.top"
    video_url  = f"{CDN_BASE}/video/{cdn_hash}"
    api_url    = f"{CDN_BASE}/player/index.php?data={cdn_hash}&do=getVideo"

    def _do() -> str | None:
        session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        try:
            # Step 1: visit player page to get session cookie
            r1 = session.get(
                video_url,
                timeout=timeout,
                headers={"Referer": BASE_URL},
            )
            LOGGER.info(
                f"[AnimeSalt] CDN page GET {r1.status_code} "
                f"cookies={list(session.cookies.keys())}"
            )

            # Step 2: POST to API
            r2 = session.post(
                api_url,
                data={"hash": cdn_hash, "r": ""},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer":          video_url,
                    "Origin":           CDN_BASE,
                },
                timeout=timeout,
            )
            LOGGER.info(
                f"[AnimeSalt] CDN API POST {r2.status_code} "
                f"body={r2.text[:200]}"
            )
            if r2.status_code != 200:
                return None

            data = r2.json()
            # videoSource and securedLink both contain the m3u8
            m3u8 = data.get("videoSource") or data.get("securedLink")
            if m3u8 and "m3u8" in m3u8:
                LOGGER.info(f"[AnimeSalt] CDN m3u8 resolved: {m3u8[:100]}")
                return m3u8

            LOGGER.warning(f"[AnimeSalt] CDN API response missing m3u8: {data}")
            return None

        except Exception as e:
            LOGGER.error(f"[AnimeSalt] CDN m3u8 resolve error: {e}")
            return None

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _do),
            timeout=timeout + 10,
        )
    except asyncio.TimeoutError:
        LOGGER.error(f"[AnimeSalt] CDN m3u8 resolve timeout for {cdn_hash}")
        return None


async def _resolve_all(raw_links: list[dict]) -> list[dict]:
    """Resolve all tracker-redirect URLs concurrently (max 6 in parallel)."""
    sem = asyncio.Semaphore(6)

    async def _resolve_one(link: dict) -> dict:
        async with sem:
            tracker = link["tracker_url"]
            if _is_external_dl(tracker):
                link["url"]    = tracker
                link["server"] = _server_from_url(tracker)
            else:
                final = await _resolve_redirect(tracker, timeout=15)
                link["url"]    = final
                link["server"] = _server_from_url(final)
            return link

    resolved = await asyncio.gather(*[_resolve_one(l) for l in raw_links])

    # Deduplicate by resolved URL
    seen, out = set(), []
    for l in resolved:
        key = l.get("url", l["tracker_url"])
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


async def _get_download_links(ep_slug: str) -> tuple[list[dict], list[dict], str | None]:
    """
    Fetch and resolve all download links for one episode.

    Returns (primary_links, multilang_links, cdn_url):
      primary_links   — trdownload → Mega/GDrive/etc. (quality-tagged)
      multilang_links — multi-lang-plyr → short.icu → per-language (language-tagged)
      cdn_url         — as-cdn21.top/video/{hash} for yt-dlp stream download (last resort)

    Priority: non-Mega primary > multilang > cdn stream (yt-dlp)
    Mega links are skipped entirely when any other option is available.
    """
    ep_url = f"{BASE_URL}/episode/{ep_slug}/"
    html   = await _fetch(ep_url, timeout=30)
    if not html:
        LOGGER.error(f"[AnimeSalt] Could not fetch {ep_url}")
        return [], [], None

    # ── Primary: trdownload links ─────────────────────────────────────────────
    raw      = _parse_download_links_from_html(html)
    resolved = []
    if raw:
        LOGGER.info(f"[AnimeSalt] {len(raw)} raw tracker links → resolving…")
        resolved = await _resolve_all(raw)
        resolved = _assign_quality_by_position(resolved)
        LOGGER.info(
            f"[AnimeSalt] {len(resolved)} resolved for {ep_slug} "
            f"qualities={[l.get('quality','?') for l in resolved]}"
        )

    # ── Fallback 1: multilang-plyr links ─────────────────────────────────────
    ml_raw      = _extract_multilang_links(html)
    ml_resolved = []
    if ml_raw:
        LOGGER.info(f"[AnimeSalt] {len(ml_raw)} multilang links → resolving…")
        ml_resolved = await _resolve_all(ml_raw)
        for l in ml_resolved:
            l["source"] = "multilang"
        LOGGER.info(
            f"[AnimeSalt] {len(ml_resolved)} multilang resolved "
            f"langs={[l.get('audio','?') for l in ml_resolved]}"
        )

    # ── Fallback 2: CDN stream URL (yt-dlp) ──────────────────────────────────
    cdn_url = _extract_cdn_hash(html)  # returns 32-char hash, not full URL

    if not resolved and not ml_resolved and not cdn_url:
        LOGGER.warning(f"[AnimeSalt] No download links at all for {ep_slug} (no primary, no multilang, no CDN hash)")

    return resolved, ml_resolved, cdn_url


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH + SERIES PARSING
# ══════════════════════════════════════════════════════════════════════════════

SEARCH_API = BASE_URL + "/wp-json/wp/v2/search"


def _search_anime(query: str) -> list[dict]:
    # WP REST search
    r = _get_text(SEARCH_API, params={
        "search": query, "type": "post",
        "per_page": 10, "_fields": "title,url,slug",
    })
    if r:
        try:
            data = json.loads(r)
            if isinstance(data, list) and data:
                results = []
                for item in data:
                    title = item.get("title", "")
                    url   = item.get("url", "")
                    # Use the 'slug' field from API directly — never extract from URL
                    # (URL may contain query params like ?gs-logo-slider=... which break slug)
                    slug  = item.get("slug", "").strip()
                    # Fallback: extract from URL only if slug field is empty
                    if not slug and url:
                        raw = url.rstrip("/").split("/")[-1]
                        slug = raw if raw and "?" not in raw else ""
                    # Skip results with invalid slugs
                    if not slug or "?" in slug or not title:
                        continue
                    # Skip if this slug is already in results
                    if any(x["slug"] == slug for x in results):
                        continue
                    results.append({"title": title, "slug": slug, "url": url})
                if results:
                    return results
        except Exception:
            pass

    # HTML search fallback
    r = _get_text(BASE_URL + "/", params={"s": query})
    if not r:
        return []
    soup    = BeautifulSoup(r, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not any(x in href for x in ["/series/", "/movies/", "/anime/"]):
            continue
        if any(x in href for x in ["?", "page/", "category/", "tag/"]):
            continue
        slug = href.rstrip("/").split("/")[-1]
        if not slug or any(x["slug"] == slug for x in results):
            continue
        title = (a.get("title") or a.get_text(strip=True)
                 or slug.replace("-", " ").title()).strip()
        if not title:
            continue
        results.append({"title": title, "slug": slug, "url": href})
        if len(results) >= 10:
            break
    return results


def _parse_series_page(slug: str) -> dict:
    base_url_for_slug = None
    html = None
    for prefix in ["series", "anime", "movies"]:
        url  = f"{BASE_URL}/{prefix}/{slug}/"
        html = _get_text(url)
        if html and len(html) > 500:
            base_url_for_slug = url
            break
    if not html:
        return {}

    soup  = BeautifulSoup(html, "html.parser")
    t     = soup.find("h1") or soup.find("h2")
    title = t.get_text(strip=True) if t else slug

    thumb = None
    og    = soup.find("meta", property="og:image")
    if og:
        thumb = og.get("content")

    seasons: dict[int, dict] = {}
    ep_pat = re.compile(
        r"/episode/" + re.escape(slug) + r"-(\d+)x(\d+)/?", re.IGNORECASE
    )

    # ── Season tab patterns ─────────────────────────────────────────────────
    # Matches "Season 2 • 33-53 (21)"  → groups: season, start_ep, end_ep, count
    season_range_pat = re.compile(
        r"season\s+(\d+)\s*[^0-9\n]{0,10}?(\d+)\s*[-–]\s*(\d+)\s*\(\s*(\d+)\s*\)",
        re.IGNORECASE
    )
    # Fallback: "Season 1 • 1-32 (32)" where start==1 OR "Season N (count)" style
    season_count_pat = re.compile(r"season\s+(\d+)[^(]*\((\d+)\)", re.IGNORECASE)

    def _add_episode(s_num: int, e_num: int):
        ep_slug_part = f"{slug}-{s_num}x{e_num}"
        if s_num not in seasons:
            seasons[s_num] = {"label": f"Season {s_num}", "episodes": []}
        if e_num not in {ep["num"] for ep in seasons[s_num]["episodes"]}:
            seasons[s_num]["episodes"].append({
                "num": e_num, "label": f"Episode {e_num}", "ep_slug": ep_slug_part,
            })

    def _extract_episodes_from_soup(s):
        for a in s.select("a[href*='/episode/']"):
            m = ep_pat.search(a.get("href", ""))
            if m:
                _add_episode(int(m.group(1)), int(m.group(2)))

    # ── Step 1: Parse static HTML pages ───────────────────────────────────────
    _extract_episodes_from_soup(soup)

    # ── Step 1.5: Parse season tab labels to get EXACT episode ranges ─────────
    #
    # AnimeSalt tabs show "Season 2 • 33-53 (21)" which tells us:
    #   Season 2 has episodes numbered 33 through 53 (21 total).
    # For anime like Naruto Shippuden, episodes use a global counter across
    # seasons so Season 2 starts at ep 33, not ep 1.
    # WP REST search only matches post *titles*, so it fails for anime whose
    # episodes have proper names ("Homecoming") instead of slug-style titles
    # ("Dan Da Dan 2x1"). Parsing tabs and adding episodes directly is the
    # only reliable method that works for ALL anime.
    #
    season_ranges:    dict[int, tuple[int, int]] = {}  # s_num → (start_ep, end_ep)
    season_ep_hints:  dict[int, int]             = {}  # s_num → max ep number

    for text in soup.stripped_strings:
        # Try full range pattern first: "Season 2 • 33-53 (21)"
        m = season_range_pat.search(text)
        if m:
            s_tab    = int(m.group(1))
            start_ep = int(m.group(2))
            end_ep   = int(m.group(3))
            if s_tab not in season_ranges:
                season_ranges[s_tab] = (start_ep, end_ep)
                season_ep_hints[s_tab] = end_ep
                LOGGER.info(
                    f"[AnimeSalt] Tab range: S{s_tab} eps {start_ep}-{end_ep}"
                )
            continue
        # Fallback count pattern: "Season 1 (32)" or "Season 1 • 1-32 (32)" etc.
        m2 = season_count_pat.search(text)
        if m2:
            s_tab = int(m2.group(1))
            count = int(m2.group(2))
            if s_tab not in season_ep_hints:
                season_ep_hints[s_tab] = count

    if season_ep_hints:
        LOGGER.info(f"[AnimeSalt] Season hints from HTML: {season_ep_hints}")

    # Directly add all episodes from tab ranges — no HTTP requests needed.
    # This is the primary fix for anime like Naruto where WP title search fails.
    for s_num, (start_ep, end_ep) in season_ranges.items():
        for e_num in range(start_ep, end_ep + 1):
            _add_episode(s_num, e_num)
        LOGGER.info(
            f"[AnimeSalt] Added S{s_num} eps {start_ep}-{end_ep} "
            f"({end_ep - start_ep + 1} eps) from tab"
        )

    page = 2
    while page <= 30:
        page_url  = f"{base_url_for_slug}page/{page}/"
        page_html = _get_text(page_url)
        if not page_html or len(page_html) < 500:
            break
        page_soup = BeautifulSoup(page_html, "html.parser")
        before    = sum(len(s["episodes"]) for s in seasons.values())
        _extract_episodes_from_soup(page_soup)
        # Also pick up any tab hints on paginated pages
        for text in page_soup.stripped_strings:
            m = season_range_pat.search(text)
            if m:
                s_tab = int(m.group(1))
                start_ep, end_ep = int(m.group(2)), int(m.group(3))
                if s_tab not in season_ranges:
                    season_ranges[s_tab] = (start_ep, end_ep)
                    season_ep_hints[s_tab] = end_ep
                    for e_num in range(start_ep, end_ep + 1):
                        _add_episode(s_tab, e_num)
        after = sum(len(s["episodes"]) for s in seasons.values())
        if after == before:
            break
        page += 1

    # ── Step 2: Per-season WP search (supplemental — works when episode titles
    #    contain the slug, e.g. "Dan Da Dan 2x1"; skipped for seasons already
    #    fully covered by tab ranges) ────────────────────────────────────────
    known_seasons    = set(seasons.keys()) | set(season_ep_hints.keys()) or {1}
    max_season       = max(known_seasons) if known_seasons else 1
    seasons_to_probe = set(range(1, max_season + 4))

    try:
        for s_num in sorted(seasons_to_probe):
            # Skip if tab already gave us the full range for this season
            if s_num in season_ranges:
                start, end = season_ranges[s_num]
                already    = len(seasons.get(s_num, {}).get("episodes", []))
                expected   = end - start + 1
                if already >= expected:
                    LOGGER.info(f"[AnimeSalt] S{s_num} fully covered by tab, skipping WP search")
                    continue

            search_term = f"{slug}-{s_num}x"
            api_page    = 1
            any_found   = False

            while api_page <= 20:
                ep_api_r = _get_text(
                    BASE_URL + "/wp-json/wp/v2/search",
                    params={
                        "search":   search_term,
                        "type":     "post",
                        "per_page": 100,
                        "page":     api_page,
                        "_fields":  "slug,url",
                    },
                )
                if not ep_api_r:
                    break
                try:
                    api_items = json.loads(ep_api_r)
                except Exception:
                    break
                if not isinstance(api_items, list) or not api_items:
                    break

                for item in api_items:
                    api_slug = item.get("slug", "") or ""
                    m = ep_pat.search(f"/episode/{api_slug}/")
                    if not m:
                        m = ep_pat.search(item.get("url", "") or "")
                    if not m:
                        continue
                    item_s = int(m.group(1))
                    item_e = int(m.group(2))
                    if item_s == s_num:
                        _add_episode(item_s, item_e)
                        any_found = True

                if len(api_items) < 100:
                    break
                api_page += 1
                LOGGER.info(f"[AnimeSalt] S{s_num} WP search page {api_page - 1} done, next…")

            LOGGER.info(
                f"[AnimeSalt] S{s_num} WP search → "
                f"{len(seasons.get(s_num, {}).get('episodes', []))} eps found"
            )
            if not any_found and s_num > max_season:
                LOGGER.info(f"[AnimeSalt] No episodes for S{s_num}, stopping season probe")
                break

    except Exception as e:
        LOGGER.warning(f"[AnimeSalt] Per-season WP search failed: {e}")

    # ── Step 3: Brute-force gap fill for seasons NOT covered by tab ranges ────
    # Only runs for seasons where we have a count hint but episodes may be
    # missing (e.g. season_ranges gave start=1 style), or new seasons beyond tabs.
    for s_num in sorted(seasons.keys()):
        # Seasons fully described by a tab range are already complete — skip
        if s_num in season_ranges:
            start, end = season_ranges[s_num]
            known      = {ep["num"] for ep in seasons[s_num]["episodes"]}
            if len(known) >= (end - start + 1):
                continue
            # Rare: tab said N eps but we're short — fill specific gaps only
            consecutive_misses = 0
            for e_num in range(start, end + 1):
                if e_num in known:
                    consecutive_misses = 0
                    continue
                ep_url = f"{BASE_URL}/episode/{slug}-{s_num}x{e_num}/"
                r = _get(ep_url, timeout=10, allow_redirects=False)
                if r and r.status_code == 200:
                    _add_episode(s_num, e_num)
                    known.add(e_num)
                    consecutive_misses = 0
                    LOGGER.info(f"[AnimeSalt] Gap-fill found S{s_num}E{e_num}")
                else:
                    consecutive_misses += 1
                    if consecutive_misses >= 3:
                        break
            continue

        # No tab range — fall back to count-based gap fill (original logic)
        eps       = seasons[s_num]["episodes"]
        known     = {e["num"] for e in eps}
        hint_max  = season_ep_hints.get(s_num, 0)
        found_max = max(known) if known else 0
        target    = max(hint_max, found_max)
        if target == 0 or len(known) >= target:
            continue

        consecutive_misses = 0
        for e_num in range(1, target + 1):
            if e_num in known:
                consecutive_misses = 0
                continue
            ep_url = f"{BASE_URL}/episode/{slug}-{s_num}x{e_num}/"
            r = _get(ep_url, timeout=10, allow_redirects=False)
            if r and r.status_code == 200:
                _add_episode(s_num, e_num)
                known.add(e_num)
                consecutive_misses = 0
                LOGGER.info(f"[AnimeSalt] Gap-fill found S{s_num}E{e_num}")
            else:
                consecutive_misses += 1
                if consecutive_misses >= 3:
                    break

    for s in seasons.values():
        s["episodes"].sort(key=lambda x: x["num"])

    return {"title": title, "thumb": thumb, "seasons": seasons}

# ══════════════════════════════════════════════════════════════════════════════
# MOVIE PAGE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_movie_page(slug: str) -> dict | None:
    """
    Movies on AnimeSalt don't have SxE episode slugs — download links live
    directly on /movies/{slug}/ (or /anime/{slug}/).
    Returns {title, thumb, raw_links, page_url} or None.
    """
    for prefix in ["movies", "anime", "series"]:
        url  = f"{BASE_URL}/{prefix}/{slug}/"
        html = _get_text(url)
        if not html or len(html) < 500:
            continue
        soup  = BeautifulSoup(html, "html.parser")
        t     = soup.find("h1") or soup.find("h2")
        title = t.get_text(strip=True) if t else slug
        og    = soup.find("meta", property="og:image")
        thumb = og.get("content") if og else None
        raw   = _parse_download_links_from_html(html)
        if raw:
            LOGGER.info(f"[AnimeSalt] Movie page found at {url} — {len(raw)} raw links")
            return {"title": title, "thumb": thumb, "raw_links": raw, "page_url": url}
    return None




async def _trigger_leech(client: Client, chat_id: int, url: str,
                          ep_label: str = "",
                          message=None) -> None:
    """
    Trigger a leech/download for the given URL.

    - Mega links: directly invoke add_mega_download (bots can't process their
      own chat messages, so sending /qbleech would be silently ignored).
    - Everything else: send /{LEECH_CMD} <url> into the chat as before.
    """
    if "mega." in url and message is not None:
        # ── Direct pipeline call for Mega links ──────────────────────────────
        try:
            from bot import DOWNLOAD_DIR
            from bot.helper.listeners.tasks_listener import MirrorLeechListener
            from bot.helper.mirror_utils.download_utils.mega_download import add_mega_download

            if message.from_user and message.from_user.username:
                tag = f"@{message.from_user.username}"
            elif message.from_user:
                tag = message.from_user.mention
            else:
                tag = ep_label or "AnimeSalt"

            listener = MirrorLeechListener(
                message,
                isLeech=True,
                tag=tag,
                source_url=url,
                leech_utils={'screenshots': 0, 'thumb': ''},
            )
            path = f"{DOWNLOAD_DIR}{message.id}/"
            if ep_label:
                await sendMessage(message, f"📥 Starting download: **{ep_label}**")
            await add_mega_download(url, path, listener, "")
        except Exception as e:
            LOGGER.error(f"[AnimeSalt] direct mega trigger failed: {e}")
            await client.send_message(
                chat_id,
                f"⬇️ Mega link ready — send manually:\n`/qbleech {url}`"
            )
        return

    # ── Non-Mega: send command text into chat (bot processes it normally) ────
    cmd = f"/{LEECH_CMD} {url}"
    try:
        if ep_label:
            await client.send_message(chat_id, f"📥 Starting download: **{ep_label}**")
        await client.send_message(chat_id, cmd)
    except Exception as e:
        LOGGER.error(f"[AnimeSalt] _trigger_leech failed: {e}")
        await client.send_message(
            chat_id,
            f"⬇️ Send this to download:\n`{cmd}`"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _link_label(lnk: dict) -> str:
    q = lnk.get("quality", "")
    # Display bare quality keys ("480", "720", "1080") with 'p' suffix
    if q and q.isdigit():
        q = q + "p"
    parts = [lnk.get("server", ""), lnk.get("audio", ""), q]
    label = " · ".join(p for p in parts if p).strip()
    return label or "Download"


def _ep_label(title: str, s: int, e: int) -> str:
    return f"{title} S{s:02d}E{e:02d}"


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMAND ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def animesalt_cmd(client: Client, message: Message):
    """/animesalt <anime name>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await sendMessage(message,
            "❌ Usage: `/animesalt <anime name>`\n"
            "Example: `/animesalt naruto shippuden`")
        return

    user_id  = message.from_user.id
    query    = args[1].strip()
    wait_msg = await sendMessage(message, f"🔍 Searching **{query}** on AnimeSalt…")
    results  = await asyncio.get_event_loop().run_in_executor(None, _search_anime, query)

    if not results:
        await editMessage(wait_msg, f"❌ No results found for **{query}**.")
        return

    _sessions[user_id] = {"results": results}
    buttons = ButtonMaker()
    for i, anime in enumerate(results):
        buttons.ibutton(anime["title"][:50], f"as_pick_{user_id}_{i}")
    buttons.ibutton("❌ Cancel", f"as_cancel_{user_id}")
    await editMessage(wait_msg,
        f"🎌 Found **{len(results)}** result(s) for **{query}**.\nChoose an anime:",
        buttons.build_menu(1)
    )


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ══════════════════════════════════════════════════════════════════════════════

async def animesalt_callback(client: Client, query):
    await query.answer()
    data    = query.data
    user_id = query.from_user.id
    msg     = query.message

    # ── Cancel ────────────────────────────────────────────────────────────────
    if data.startswith("as_cancel_"):
        _sessions.pop(user_id, None)
        await editMessage(msg, "❌ Cancelled.")
        return

    # ── Anime picked ──────────────────────────────────────────────────────────
    if data.startswith("as_pick_"):
        await _cb_pick_anime(client, msg, user_id, int(data.split("_")[-1]))
        return

    # ── Mode selection ────────────────────────────────────────────────────────
    if data.startswith("as_mode_ep_"):
        session = _sessions.get(user_id)
        if not session:
            await editMessage(msg, "⚠️ Session expired."); return
        await _show_season_picker(msg, session, user_id, mode="ep")
        return

    if data.startswith("as_mode_season_"):
        session = _sessions.get(user_id)
        if not session:
            await editMessage(msg, "⚠️ Session expired."); return
        await _show_season_picker(msg, session, user_id, mode="season")
        return

    # ── Season selected (for single-episode picker) ───────────────────────────
    if data.startswith("as_sep_s_"):
        parts      = data.split("_")
        season_num = int(parts[-1])
        session    = _sessions.get(user_id)
        if not session:
            await editMessage(msg, "⚠️ Session expired."); return
        session["chosen_season"] = season_num
        await _show_episode_picker(msg, session, user_id, season_num)
        return

    # ── Season selected (full-season download) → ask quality FIRST ───────────
    if data.startswith("as_ssn_"):
        parts      = data.split("_")
        season_num = int(parts[-1])
        session    = _sessions.get(user_id)
        if not session:
            await editMessage(msg, "⚠️ Session expired."); return
        session["pending_ssn"] = season_num
        title = session["series_data"]["title"]
        n_eps = len(session["series_data"]["seasons"][season_num]["episodes"])
        buttons = _quality_buttons(user_id, "as_sq_")
        await editMessage(msg,
            f"🎌 **{title}** — Season {season_num} ({n_eps} eps)\n\n"
            "🎬 Select quality to download:",
            buttons.build_menu(2)
        )
        return

    # ── Episode selected → fetch links → show quality picker ─────────────────
    if data.startswith("as_ep_"):
        await _cb_episode_selected(client, msg, user_id, data)
        return

    # ── Quality selected (single episode) ────────────────────────────────────
    if data.startswith("as_q_"):
        await _cb_episode_quality(client, msg, user_id, data)
        return

    # ── Quality selected (full season) ───────────────────────────────────────
    if data.startswith("as_sq_"):
        await _cb_season_quality(client, msg, user_id, data)
        return

    # ── Movie quality selected ────────────────────────────────────────────────
    if data.startswith("as_mq_"):
        await _cb_movie_quality(client, msg, user_id, data)
        return

    # ── Language fallback picker shown ───────────────────────────────────────
    if data == f"as_lang_{user_id}":
        session = _sessions.get(user_id)
        if not session or "pending_ep" not in session:
            await editMessage(msg, "⚠️ Session expired."); return
        ep    = session["pending_ep"]
        label = _ep_label(ep["title"], ep["s"], ep["e"])
        await _show_lang_picker(msg, user_id, label, ep.get("ml_links", []))
        return

    # ── Language link selected ────────────────────────────────────────────────
    if data.startswith("as_lang_") and data.count("_") >= 3:
        await _cb_lang_selected(client, msg, user_id, data)
        return

    # ── CDN stream button from quality picker → show language picker first ──
    if data == f"as_cdn_{user_id}":
        session = _sessions.get(user_id)
        if not session or "pending_ep" not in session:
            await editMessage(msg, "\u26a0\ufe0f Session expired."); return
        ep       = session["pending_ep"]   # keep in session for lang step
        cdn_hash = ep.get("cdn_url")
        label    = _ep_label(ep["title"], ep["s"], ep["e"])
        await _show_cdn_picker(msg, user_id, label, cdn_hash)
        return

    # ── CDN resolution selected → show language picker ───────────────────────
    if data.startswith("as_cdnres_") and f"as_cdnres_{user_id}_" in data:
        session = _sessions.get(user_id)
        if not session or "pending_ep" not in session:
            await editMessage(msg, "\u26a0\ufe0f Session expired."); return
        res_code = data.split("_")[-1]   # 480 / 720 / 1080 / best
        session["pending_ep"]["cdn_res"] = res_code
        await _show_cdn_lang_picker(msg, user_id)
        return

    # ── CDN language selected → resolve m3u8 → download ──────────────────────
    if data.startswith("as_cdnlang_") and f"as_cdnlang_{user_id}_" in data:
        session = _sessions.get(user_id)
        if not session or "pending_ep" not in session:
            await editMessage(msg, "\u26a0\ufe0f Session expired."); return
        ep        = session.pop("pending_ep")
        cdn_hash  = ep.get("cdn_url")
        res_code  = ep.get("cdn_res", "best")
        label     = _ep_label(ep["title"], ep["s"], ep["e"])
        lang_code = data.split("_")[-1]   # hin / tam / eng / auto
        _sessions.pop(user_id, None)
        await _trigger_cdn_download(client, msg, user_id, label, cdn_hash,
                                    lang_code=lang_code, res_code=res_code)
        return

    # ── Custom episode range button clicked ───────────────────────────────────
    if data.startswith("as_cr_") and f"as_cr_{user_id}_" in data:
        parts      = data.split("_")
        season_num = int(parts[-1])
        session    = _sessions.get(user_id)
        if not session:
            await editMessage(msg, "⚠️ Session expired."); return
        session["waiting_custom_range"] = True
        session["waiting_season_num"]   = season_num
        title = session["series_data"]["title"]
        episodes = session["series_data"]["seasons"][season_num]["episodes"]
        ep_nums  = [e["num"] for e in episodes]
        ep_min, ep_max = min(ep_nums), max(ep_nums)
        await editMessage(msg,
            f"🎌 **{title}** — Season {season_num}\n\n"
            f"🔢 **Send episode range** as: `start,end`\n"
            f"Example: `1,13` or `18,22`\n\n"
            f"_Available episodes: {ep_min}–{ep_max}_"
        )
        return



# ══════════════════════════════════════════════════════════════════════════════
# CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _cb_pick_anime(client, msg, user_id: int, idx: int):
    session = _sessions.get(user_id)
    if not session:
        await editMessage(msg, "⚠️ Session expired."); return

    anime = session["results"][idx]
    session["anime"] = anime
    await editMessage(msg, f"⏳ Loading episodes for **{anime['title']}**…")

    series_data = await asyncio.get_event_loop().run_in_executor(
        None, _parse_series_page, anime["slug"]
    )
    if not series_data or not series_data.get("seasons"):
        # No SxE episodes found — try treating it as a movie
        await editMessage(msg, f"⏳ Checking if **{anime['title']}** is a movie…")
        movie_data = await asyncio.get_event_loop().run_in_executor(
            None, _parse_movie_page, anime["slug"]
        )
        if movie_data and movie_data.get("raw_links"):
            resolved = await _resolve_all(movie_data["raw_links"])
            resolved = _assign_quality_by_position(resolved)
            if not resolved:
                await editMessage(msg,
                    f"❌ Found the movie page but couldn't resolve any download links.\n"
                    f"Check: `{movie_data['page_url']}`")
                return
            session["movie_links"]  = resolved
            session["movie_title"]  = movie_data["title"]
            available = sorted({l.get("quality", "?") for l in resolved if l.get("quality")})
            buttons   = _quality_buttons(user_id, "as_mq_", available or None)
            await editMessage(msg,
                f"🎬 **{movie_data['title']}** *(Movie)*\n"
                f"🔗 {len(resolved)} link(s) found\n\n"
                "Select quality to download:",
                buttons.build_menu(2)
            )
            return
        await editMessage(msg,
            f"❌ Could not load episode list for **{anime['title']}**.\n"
            f"Check: `{BASE_URL}/series/{anime['slug']}/`")
        return

    session["series_data"] = series_data
    season_nums = sorted(series_data["seasons"].keys())
    total_eps   = sum(len(s["episodes"]) for s in series_data["seasons"].values())

    buttons = ButtonMaker()
    buttons.ibutton("📺 Single Episode", f"as_mode_ep_{user_id}")
    buttons.ibutton("📦 Full Season",    f"as_mode_season_{user_id}")
    buttons.ibutton("❌ Cancel",         f"as_cancel_{user_id}")
    await editMessage(msg,
        f"🎌 **{series_data['title']}**\n"
        f"📅 Seasons: {len(season_nums)}  |  🎞 Episodes: {total_eps}\n\n"
        "What do you want?",
        buttons.build_menu(2)
    )


async def _cb_episode_selected(client, msg, user_id: int, data: str):
    """User picked a specific episode → fetch links → show quality picker."""
    parts      = data.split("_")
    ep_num     = int(parts[-1])
    season_num = int(parts[-2])
    session    = _sessions.get(user_id)
    if not session:
        await editMessage(msg, "⚠️ Session expired."); return

    season_data = session["series_data"]["seasons"].get(season_num)
    ep_info     = next((e for e in season_data["episodes"] if e["num"] == ep_num), None)
    if not ep_info:
        await editMessage(msg, "❌ Episode not found."); return

    title = session["series_data"]["title"]
    label = _ep_label(title, season_num, ep_num)
    await editMessage(msg, f"⏳ Fetching links for **{label}**…")

    links, ml_links, cdn_url = await _get_download_links(ep_info["ep_slug"])

    if not links and not ml_links and not cdn_url:
        _sessions.pop(user_id, None)
        await editMessage(msg,
            f"\u274c No download links found for **{label}**.\n\n"
            f"Debug: `/animesaltdbg {ep_info['ep_slug']}`"
        )
        return

    # Store all link types in session — keep ALL primary links including Mega
    # (Mega is tried first; CDN/multilang shown as fallback buttons if blocked)
    session["pending_ep"] = {
        "title":    title,
        "s":        season_num,
        "e":        ep_num,
        "ep_slug":  ep_info["ep_slug"],
        "links":    links,
        "ml_links": ml_links,
        "cdn_url":  cdn_url,
    }

    if links:
        # Primary links exist (may include Mega) → show quality picker
        available    = _available_quality_keys(links)
        groups       = group_links_by_quality(links)
        quality_info = "\n".join(
            f"  \u2022 {QUALITY_LABELS.get(k, k+'p')}: {len(v)} link(s)"
            for k, v in sorted(groups.items()) if k
        ) or "  \u2022 Unknown quality"
        buttons = _quality_buttons(user_id, "as_q_", available or None)
        # Show fallback options alongside — user can pick if Mega is blocked
        if ml_links:
            buttons.ibutton(
                f"\U0001f310 Language ({len(ml_links)})",
                f"as_lang_{user_id}"
            )
        if cdn_url:
            buttons.ibutton("\U0001f4fa Stream Fallback", f"as_cdn_{user_id}")
        status = f"\u2705 Found **{len(links)}** link(s):\n{quality_info}"
        if ml_links or cdn_url:
            status += "\n\n_If Mega is blocked use fallback buttons below_"
        await editMessage(msg,
            f"\U0001f3cc **{label}**\n\n{status}\n\n"
            "\U0001f3ac Select quality to download:",
            buttons.build_menu(2)
        )
    elif ml_links:
        # No primary links — show language picker
        LOGGER.info(f"[AnimeSalt] No primary links, using multilang for {ep_info['ep_slug']}")
        await _show_lang_picker(msg, user_id, label, ml_links)
    else:
        # Only CDN stream available
        LOGGER.info(f"[AnimeSalt] Only CDN stream available for {ep_info['ep_slug']}")
        await _show_cdn_picker(msg, user_id, label, cdn_url)


async def _cb_episode_quality(client, msg, user_id: int, data: str):
    """
    Quality chosen for a single episode.
    data format: as_q_{uid}_{quality_key}
    """
    quality_key = data.split("_")[-1]          # '480', '720', '1080', '2160', 'all'
    session     = _sessions.get(user_id)
    if not session or "pending_ep" not in session:
        await editMessage(msg, "⚠️ Session expired."); return

    ep    = session.pop("pending_ep")
    links = ep["links"]
    title = ep["title"]
    label = _ep_label(title, ep["s"], ep["e"])

    # Filter by quality
    filtered = filter_links_by_quality(links, quality_key)
    if not filtered and quality_key != "all":
        q_label = QUALITY_LABELS.get(quality_key, quality_key + "p")
        await editMessage(msg,
            f"⚠️ No **{q_label}** links found for **{label}**.\n"
            f"Available: {', '.join(_available_quality_keys(links)) or 'unknown'}\n\n"
            "Try `/animesalt` again and pick a different quality."
        )
        return

    _sessions.pop(user_id, None)

    if len(filtered) == 1:
        lnk     = filtered[0]
        url     = lnk.get("url", "")
        q_label = QUALITY_LABELS.get(quality_key, quality_key + "p")

        # ── Mega pre-check: verify accessible before triggering download ──────
        if _is_mega(lnk):
            await editMessage(msg,
                f"\U0001f3cc **{label}**\n"
                f"\U0001f3ac Quality: **{q_label}**\n"
                f"\U0001f4be Server: **Mega**\n\n"
                "\u23f3 Checking Mega link\u2026"
            )
            blocked = await _async_mega_is_blocked(url)
            if blocked:
                LOGGER.info(
                    f"[AnimeSalt] Mega blocked for {label} — "
                    f"auto-redirecting to fallback"
                )
                # Retrieve fallback options from session if still alive
                fallback_session = _sessions.get(user_id, {})
                cdn_hash  = ep.get("cdn_url")
                ml_links  = ep.get("ml_links", [])

                if ml_links:
                    await editMessage(msg,
                        f"\U0001f3cc **{label}**\n\n"
                        "\u274c Mega is blocked from this server\n"
                        "\U0001f504 Auto-switching to language downloads\u2026"
                    )
                    # Re-store in session for lang picker
                    _sessions[user_id] = {"pending_ep": ep}
                    await _show_lang_picker(msg, user_id, label, ml_links)
                elif cdn_hash:
                    await editMessage(msg,
                        f"\U0001f3cc **{label}**\n\n"
                        "\u274c Mega is blocked from this server\n"
                        "\U0001f504 Auto-switching to stream download\u2026"
                    )
                    _sessions[user_id] = {"pending_ep": ep}
                    await _show_cdn_picker(msg, user_id, label, cdn_hash)
                else:
                    await editMessage(msg,
                        f"\u274c **{label}**\n\n"
                        "Mega is blocked and no fallback is available."
                    )
                return

        # Mega OK or non-Mega link — proceed normally
        await editMessage(msg,
            f"\U0001f3cc **{label}**\n"
            f"\U0001f3ac Quality: **{q_label}**\n"
            f"\U0001f4be Server: **{lnk.get('server', '?')}**\n\n"
            f"\U0001f4e5 Starting download\u2026"
        )
        await _trigger_leech(client, msg.chat.id, url, label, message=msg)
        return

    # Multiple options for the same quality → let user pick exact one
    buttons = ButtonMaker()
    for i, lnk in enumerate(filtered):
        buttons.ibutton(f"💾 {_link_label(lnk)}", f"as_dl_{user_id}_{i}")
    buttons.ibutton("❌ Cancel", f"as_cancel_{user_id}")
    session2 = {"pending_dl": {"label": label, "links": filtered}}
    _sessions[user_id] = session2
    await editMessage(msg,
        f"🎌 **{label}**\n"
        f"✅ **{len(filtered)}** option(s) — choose one:",
        buttons.build_menu(2)
    )


async def _cb_season_quality(client, msg, user_id: int, data: str):
    """
    Quality chosen for a full season batch download.
    data format: as_sq_{uid}_{quality_key}
    """
    quality_key = data.split("_")[-1]
    session     = _sessions.get(user_id)
    if not session or "pending_ssn" not in session:
        await editMessage(msg, "⚠️ Session expired."); return

    season_num  = session["pending_ssn"]
    series_data = session["series_data"]
    title       = series_data["title"]
    episodes    = series_data["seasons"][season_num]["episodes"]
    # Apply custom range filter if user picked a range instead of full season
    if "custom_ep_range" in session:
        ep_nums  = set(session["custom_ep_range"])
        episodes = [e for e in episodes if e["num"] in ep_nums]
    q_label     = QUALITY_LABELS.get(quality_key, quality_key + "p")

    await editMessage(msg,
        f"⏳ Fetching **{q_label}** links for **{title}** S{season_num} "
        f"({len(episodes)} eps)…\n_This may take a moment._"
    )

    all_links: list[tuple[int, list[dict]]] = []
    failed_eps: list[int] = []

    for ep in episodes:
        primary, ml, ep_cdn = await _get_download_links(ep["ep_slug"])

        # Pre-check Mega — if blocked, auto-fall to CDN/multilang
        mega_links    = [l for l in primary if _is_mega(l)]
        non_mega      = _filter_mega(primary)
        mega_blocked  = False

        if mega_links and not non_mega:
            # All primary links are Mega — check if accessible
            mega_blocked = await _async_mega_is_blocked(
                mega_links[0].get("url", mega_links[0].get("tracker_url", ""))
            )

        if primary and not mega_blocked:
            filtered = filter_links_by_quality(primary, quality_key)
            if not filtered:
                filtered = primary
            all_links.append((ep["num"], filtered, "primary", None))
        elif ml:
            LOGGER.info(f"[AnimeSalt] Ep {ep['num']}: multilang fallback")
            all_links.append((ep["num"], ml, "multilang", None))
        elif ep_cdn or (mega_blocked and ep_cdn):
            LOGGER.info(f"[AnimeSalt] Ep {ep['num']}: CDN stream fallback")
            all_links.append((ep["num"], [], "cdn", ep_cdn))
        elif non_mega:
            # Has non-Mega primary links — use them
            filtered = filter_links_by_quality(non_mega, quality_key) or non_mega
            all_links.append((ep["num"], filtered, "primary", None))
        else:
            failed_eps.append(ep["num"])
        await sleep(1.2)

    _sessions.pop(user_id, None)

    if not all_links:
        await editMessage(msg,
            f"❌ No **{q_label}** links found for **{title}** Season {season_num}."
        )
        return

    # Send summary and trigger batch leech
    summary = (
        f"🎌 **{title}** — Season {season_num}\n"
        f"🎬 Quality: **{q_label}**\n"
        f"✅ {len(all_links)} episode(s) found"
        + (f" | ❌ {len(failed_eps)} failed: {failed_eps}" if failed_eps else "")
        + "\n\n📥 Starting batch download…"
    )
    await editMessage(msg, summary)

    for ep_num, links, source, ep_cdn_url in all_links:
        label = _ep_label(title, season_num, ep_num)

        if source == "cdn":
            if not ep_cdn_url:
                LOGGER.warning(f"[AnimeSalt] Batch CDN: no hash for ep {ep_num}")
                continue

            # Check Tamil availability first
            lang_to_use = await _check_stream_lang(ep_cdn_url, "tam")
            if lang_to_use != "tam":
                # Tamil not found — inform admin
                await client.send_message(
                    msg.chat.id,
                    f"\u26a0\ufe0f **{label}**: Tamil audio not found in stream\n"
                    f"\U0001f504 Using available: **{lang_to_use.upper()}**"
                )

            if quality_key == "all":
                for res in ("480", "720", "1080"):
                    status_msg = await client.send_message(
                        msg.chat.id,
                        f"\U0001f4fa Queuing: **{label}** [{res}p | {lang_to_use.upper()}]"
                    )
                    await _trigger_cdn_download(
                        client, status_msg, user_id,
                        f"{label} [{res}p]", ep_cdn_url,
                        lang_code=lang_to_use, res_code=res
                    )
                    await sleep(2.0)
            else:
                res = quality_key if quality_key in ("480","720","1080") else "best"
                status_msg = await client.send_message(
                    msg.chat.id,
                    f"\U0001f4fa Queuing: **{label}** [{res}p | {lang_to_use.upper()}]"
                )
                await _trigger_cdn_download(
                    client, status_msg, user_id, label, ep_cdn_url,
                    lang_code=lang_to_use, res_code=res
                )
        else:
            lnk = links[0]
            url = lnk.get("url", lnk.get("tracker_url", ""))
            if not url:
                LOGGER.warning(f"[AnimeSalt] Batch: no URL for ep {ep_num}, skipping")
                continue
            suffix = (f" \U0001f310 [{lnk.get('audio','?')}]"
                      if source == "multilang" else "")
            trigger_msg = await client.send_message(
                msg.chat.id,
                f"\U0001f4e5 Queuing: **{label}**{suffix}"
            )
            await _trigger_leech(client, msg.chat.id, url, label,
                                  message=trigger_msg)
        await sleep(2.0)


# ── Movie quality callback ─────────────────────────────────────────────────────

async def _cb_movie_quality(client, msg, user_id: int, data: str):
    """as_mq_{uid}_{quality_key} — user picked a quality for a movie."""
    # data format: as_mq_{uid}_{quality_key}
    parts       = data.split("_")
    quality_key = parts[-1]          # e.g. "1080", "720", "all"

    session = _sessions.get(user_id)
    if not session or "movie_links" not in session:
        await editMessage(msg, "⚠️ Session expired."); return

    links = session["movie_links"]
    title = session.get("movie_title", "Movie")

    if quality_key == "all":
        filtered = links
    else:
        filtered = [l for l in links if l.get("quality") == quality_key]
        if not filtered:
            filtered = links   # fallback: send everything

    _sessions.pop(user_id, None)

    q_label = QUALITY_LABELS.get(quality_key, quality_key + "p") if quality_key != "all" else "All Qualities"
    await editMessage(msg,
        f"🎬 **{title}** *(Movie)*\n"
        f"🎬 Quality: **{q_label}**\n"
        f"🔗 {len(filtered)} link(s) queued\n\n"
        "📥 Starting download(s)…"
    )

    for i, lnk in enumerate(filtered):
        # Send a UNIQUE message per download — MirrorLeechListener uses
        # message.id as its UID and download path key. Reusing the same
        # msg for multiple downloads causes later ones to silently overwrite
        # the first entry in download_dict.
        label = f"🎬 {title}" + (f" [{i+1}/{len(filtered)}]" if len(filtered) > 1 else "")
        trigger_msg = await client.send_message(
            msg.chat.id,
            f"📥 Queuing: **{label}**"
        )
        await _trigger_leech(client, msg.chat.id, lnk["url"], label, message=trigger_msg)
        if i < len(filtered) - 1:
            await sleep(3.0)   # stagger so task_manager doesn't queue-collide


# ── Legacy direct-download callback (kept for backwards compat) ───────────────

async def _cb_direct_download(client, msg, user_id: int, data: str):
    """as_dl_{uid}_{idx} — picks one link from pending_dl session."""
    idx     = int(data.split("_")[-1])
    session = _sessions.get(user_id)
    if not session or "pending_dl" not in session:
        await editMessage(msg, "⚠️ Session expired."); return

    pending = session.pop("pending_dl")
    links   = pending["links"]
    label   = pending.get("label", "Episode")
    if idx >= len(links):
        await editMessage(msg, "❌ Invalid selection."); return

    lnk = links[idx]
    _sessions.pop(user_id, None)
    await editMessage(msg,
        f"🎌 **{label}**\n"
        f"💾 **{_link_label(lnk)}**\n\n"
        f"📥 Starting download…"
    )
    await _trigger_leech(client, msg.chat.id, lnk["url"], label, message=msg)


# ══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _check_stream_lang(cdn_hash: str, preferred: str = "tam") -> str:
    """
    Check available audio languages in the CDN m3u8 stream.
    Returns preferred lang if available, otherwise first available lang,
    otherwise 'auto'.

    Fetches master.m3u8 and parses EXT-X-MEDIA:TYPE=AUDIO entries.
    """
    def _do() -> str:
        try:
            m3u8_url = None
            # Quick API call to get m3u8 URL
            CDN_BASE  = "https://as-cdn21.top"
            video_url = f"{CDN_BASE}/video/{cdn_hash}"
            api_url   = f"{CDN_BASE}/player/index.php?data={cdn_hash}&do=getVideo"
            session   = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            session.get(video_url, timeout=10, headers={"Referer": BASE_URL})
            r = session.post(
                api_url,
                data={"hash": cdn_hash, "r": ""},
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Referer": video_url},
                timeout=10,
            )
            data     = r.json()
            m3u8_url = data.get("videoSource") or data.get("securedLink")
            if not m3u8_url:
                return "auto"

            # Fetch master.m3u8 and parse audio track languages
            m = session.get(m3u8_url, timeout=10,
                            headers={"Referer": video_url})
            langs = re.findall(
                r'EXT-X-MEDIA:TYPE=AUDIO[^\n]*LANGUAGE="([^"]+)"',
                m.text, re.IGNORECASE
            )
            langs = [l.lower()[:3] for l in langs]
            LOGGER.info(f"[AnimeSalt] Stream langs for {cdn_hash}: {langs}")

            if not langs:
                return preferred  # can't detect, assume preferred exists

            if preferred in langs:
                return preferred

            # Preferred not found — return first available
            return langs[0] if langs else "auto"

        except Exception as e:
            LOGGER.warning(f"[AnimeSalt] _check_stream_lang error: {e}")
            return preferred  # assume available on error

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _do),
            timeout=25,
        )
    except asyncio.TimeoutError:
        return preferred


async def _trigger_cdn_download(client, msg, user_id: int,
                                label: str, cdn_hash: str,
                                lang_code: str = "auto",
                                res_code: str = "best") -> None:
    """
    Resolve as-cdn21.top video hash → signed master.m3u8 → yt-dlp with Referer.

    Flow:
      1. GET  as-cdn21.top/video/{hash}  (get session cookie)
      2. POST as-cdn21.top/player/index.php?data={hash}&do=getVideo
      3. JSON → videoSource = signed master.m3u8 URL
      4. YoutubeDLHelper with Referer header + audio language selection
         (the CDN returns 403 if Referer is missing)

    lang_code: ISO 639-2 code e.g. "hin","tam","eng" or "auto" for best
    """
    if not cdn_hash:
        await editMessage(msg, "\u274c No stream available for this episode.")
        return

    _sessions.pop(user_id, None)
    await editMessage(msg,
        f"\U0001f3cc **{label}**\n"
        "\U0001f4fa Resolving stream URL\u2026"
    )

    m3u8_url = await _resolve_cdn_m3u8(cdn_hash)
    if not m3u8_url:
        await editMessage(msg,
            f"\u274c **{label}**\n\n"
            "Could not resolve CDN stream URL.\n"
            "The CDN may require a browser session or the link has expired."
        )
        return

    LOGGER.info(f"[AnimeSalt] HLS m3u8 resolved: {m3u8_url[:100]}")
    await editMessage(msg,
        f"\U0001f3cc **{label}**\n"
        "\U0001f4fa HLS stream resolved\n\n"
        "\U0001f4e5 Starting download\u2026"
    )

    # ── Use YoutubeDLHelper (yt-dlp) with Referer header ────────────────────
    # aria2c returns "Not added" for m3u8 URLs — yt-dlp handles HLS natively.
    # We inject http_headers directly into opts before calling add_download.
    try:
        from bot import DOWNLOAD_DIR
        from bot.helper.listeners.tasks_listener import MirrorLeechListener
        from bot.helper.mirror_utils.download_utils.yt_dlp_download import YoutubeDLHelper

        if msg.from_user and msg.from_user.username:
            tag = f"@{msg.from_user.username}"
        elif msg.from_user:
            tag = msg.from_user.mention
        else:
            tag = label or "AnimeSalt"

        listener = MirrorLeechListener(
            msg,
            isLeech=True,
            tag=tag,
            source_url=m3u8_url,
            leech_utils={"screenshots": 0, "thumb": ""},
        )
        path = f"{DOWNLOAD_DIR}{msg.id}/"

        helper = YoutubeDLHelper(listener)

        # Inject Referer — CDN returns 403 without it
        helper.opts["http_headers"] = {
            "Referer": f"https://as-cdn21.top/video/{cdn_hash}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        # Build yt-dlp format string from resolution + language choices
        res_map = {"480": "480", "720": "720", "1080": "1080", "best": None}
        height  = res_map.get(res_code)

        if lang_code == "all":
            # ── Multi-audio MKV: download best video + ALL audio tracks ──────
            # yt-dlp merges everything into a single MKV with multiple
            # selectable audio streams (works in VLC, MPV, MX Player etc.)
            if height:
                fmt = (
                    f"bestvideo[height<={height}]"
                    f"/best[height<={height}]/best"
                )
            else:
                fmt = "bestvideo/best"

            # These opts tell yt-dlp to keep all audio tracks
            # Full stream copy — no re-encoding, fastest postprocessing
            helper.opts["postprocessor_args"] = {
                "ffmpeg": [
                    "-map", "0:v:0?", "-map", "0:a?",
                    "-c", "copy",
                ]
            }
            helper.opts["merge_output_format"] = "mkv"
            helper.opts["allow_multiple_audio_streams"] = True
            helper.opts["allow_multiple_video_streams"] = False
            lang_label = "All Languages (MKV)"

        elif height and lang_code and lang_code != "auto":
            fmt = (
                f"bestvideo[height<={height}]+bestaudio[language={lang_code}]"
                f"/bestvideo[height<={height}]+bestaudio"
                f"/best[height<={height}]/best"
            )
            lang_label = lang_code.upper()
        elif height:
            fmt = (
                f"bestvideo[height<={height}]+bestaudio"
                f"/best[height<={height}]/best"
            )
            lang_label = "Auto"
        elif lang_code and lang_code != "auto":
            fmt = (
                f"bestvideo+bestaudio[language={lang_code}]"
                f"/bestvideo+bestaudio/best"
            )
            lang_label = lang_code.upper()
        else:
            fmt = "bestvideo+bestaudio/best"
            lang_label = "Auto"

        res_label = {"480": "480p", "720": "720p", "1080": "1080p",
                     "best": "Best"}.get(res_code, res_code)
        LOGGER.info(
            f"[AnimeSalt] CDN format: {fmt} "
            f"(res={res_label} lang={lang_label})"
        )

        await helper.add_download(
            m3u8_url,
            path,
            label.replace(" ", "."),  # filename hint
            fmt,
            False,                    # not a playlist
            "",                       # no extra options string
        )

    except Exception as e:
        LOGGER.error(f"[AnimeSalt] yt-dlp CDN download failed: {e}")
        await client.send_message(
            msg.chat.id,
            f"\u26a0\ufe0f Stream download failed: `{e}`\n\n"
            f"m3u8 URL (valid for ~24h):\n`{m3u8_url}`"
        )


async def _show_cdn_picker(msg, user_id: int, label: str, cdn_hash: str):
    """Step 1 — choose resolution for CDN stream download."""
    if not cdn_hash:
        await editMessage(msg, "\u274c No stream available for this episode.")
        return

    RES_OPTIONS = [
        ("\U0001f4f1 480p  (SD)",   "480"),
        ("\U0001f4bb 720p  (HD)",   "720"),
        ("\U0001f5a5  1080p (FHD)", "1080"),
        ("\U0001f4e6 Best Quality", "best"),
    ]
    buttons = ButtonMaker()
    for res_label, res_code in RES_OPTIONS:
        buttons.ibutton(res_label, f"as_cdnres_{user_id}_{res_code}")
    buttons.ibutton("\u274c Cancel", f"as_cancel_{user_id}")

    await editMessage(msg,
        f"\U0001f3cc **{label}**\n\n"
        "\U0001f4fa Stream download (HLS via yt-dlp)\n\n"
        "\U0001f3ac Select resolution:",
        buttons.build_menu(2)
    )


async def _show_cdn_lang_picker(msg, user_id: int):
    """Step 2 — choose audio language after resolution is picked."""
    session = _sessions.get(user_id)
    if not session or "pending_ep" not in session:
        await editMessage(msg, "\u26a0\ufe0f Session expired."); return

    ep        = session["pending_ep"]
    label     = _ep_label(ep["title"], ep["s"], ep["e"])
    res_code  = ep.get("cdn_res", "best")
    res_label = {"480": "480p SD", "720": "720p HD",
                 "1080": "1080p FHD", "best": "Best"}.get(res_code, res_code)

    LANG_OPTIONS = [
        ("\U0001f1ee\U0001f1f3 Hindi",     "hin"),
        ("\U0001f1ee\U0001f1f3 Tamil",     "tam"),
        ("\U0001f1ee\U0001f1f3 Telugu",    "tel"),
        ("\U0001f1ee\U0001f1f3 Bengali",   "ben"),
        ("\U0001f1ee\U0001f1f3 Malayalam", "mal"),
        ("\U0001f1ee\U0001f1f3 Kannada",   "kan"),
        ("\U0001f1ee\U0001f1f3 Marathi",   "mar"),
        ("\U0001f1ec\U0001f1e7 English",   "eng"),
        ("\U0001f1ef\U0001f1f5 Japanese",  "jpn"),
        ("\U0001f4e6 Auto",                 "auto"),
        ("\U0001f3b6 All Languages (MKV)",  "all"),
    ]
    buttons = ButtonMaker()
    for lang_label, lang_code in LANG_OPTIONS:
        buttons.ibutton(lang_label, f"as_cdnlang_{user_id}_{lang_code}")
    buttons.ibutton("\u274c Cancel", f"as_cancel_{user_id}")

    await editMessage(msg,
        f"\U0001f3cc **{label}**\n"
        f"\U0001f3ac Resolution: **{res_label}**\n\n"
        "\U0001f3b5 Select audio language:",
        buttons.build_menu(2)
    )



async def _show_lang_picker(msg, user_id: int, label: str, ml_links: list[dict]):
    """Show language selection buttons for multilang fallback downloads."""
    if not ml_links:
        await editMessage(msg, "❌ No language links available.")
        return
    buttons = ButtonMaker()
    for i, lnk in enumerate(ml_links):
        lang   = lnk.get("audio", f"Link {i+1}")
        server = lnk.get("server", "?")
        server_tag = f" [{server}]" if server and server != "?" else ""
        buttons.ibutton(f"🌐 {lang}{server_tag}", f"as_lang_{user_id}_{i}")
    buttons.ibutton("❌ Cancel", f"as_cancel_{user_id}")
    await editMessage(msg,
        f"\U0001f3cc **{label}**\n\n"
        "\u26a0\ufe0f Primary links unavailable — select language to download:\n"
        "_(These are language-specific download links)_",
        buttons.build_menu(2)
    )


async def _cb_lang_selected(client, msg, user_id: int, data: str):
    """
    User picked a language from the multilang fallback picker.
    data format: as_lang_{uid}_{idx}
    """
    idx     = int(data.split("_")[-1])
    session = _sessions.get(user_id)
    if not session or "pending_ep" not in session:
        await editMessage(msg, "⚠️ Session expired."); return

    ep       = session.pop("pending_ep")
    ml_links = ep.get("ml_links", [])
    if idx >= len(ml_links):
        await editMessage(msg, "❌ Invalid selection."); return

    lnk   = ml_links[idx]
    label = _ep_label(ep["title"], ep["s"], ep["e"])
    lang  = lnk.get("audio", "Unknown")
    url   = lnk.get("url", lnk.get("tracker_url", ""))

    _sessions.pop(user_id, None)
    if not url:
        await editMessage(msg, f"❌ Could not resolve link for {lang}."); return

    await editMessage(msg,
        f"\U0001f3cc **{label}**\n"
        f"\U0001f310 Language: **{lang}**\n"
        f"\U0001f4be Server: **{lnk.get('server', '?')}**\n\n"
        "\U0001f4e5 Starting download\u2026"
    )
    trigger_msg = await client.send_message(
        msg.chat.id,
        f"📥 Queuing: **{label} [{lang}]**"
    )
    await _trigger_leech(client, msg.chat.id, url,
                         f"{label} [{lang}]", message=trigger_msg)


async def _show_season_picker(msg, session: dict, user_id: int, mode: str):
    series_data = session["series_data"]
    season_nums = sorted(series_data["seasons"].keys())
    buttons     = ButtonMaker()
    cb_prefix   = "as_sep_s" if mode == "ep" else "as_ssn"
    for s in season_nums:
        n = len(series_data["seasons"][s]["episodes"])
        buttons.ibutton(f"Season {s} ({n} eps)", f"{cb_prefix}_{user_id}_{s}")
    buttons.ibutton("❌ Cancel", f"as_cancel_{user_id}")
    action = "pick episode from" if mode == "ep" else "batch download"
    await editMessage(msg,
        f"🎌 **{series_data['title']}**\nSelect a season to {action}:",
        buttons.build_menu(2)
    )


async def _show_episode_picker(msg, session: dict, user_id: int, season_num: int):
    season_data = session["series_data"]["seasons"][season_num]
    episodes    = season_data["episodes"]
    buttons     = ButtonMaker()
    for ep in episodes[:48]:
        buttons.ibutton(f"Ep {ep['num']}", f"as_ep_{user_id}_{season_num}_{ep['num']}")
    if len(episodes) > 48:
        buttons.ibutton("…more", f"as_cancel_{user_id}")
    buttons.ibutton("🔢 Custom Range", f"as_cr_{user_id}_{season_num}")
    buttons.ibutton("❌ Cancel",       f"as_cancel_{user_id}")
    title = session["series_data"]["title"]
    await editMessage(msg,
        f"🎌 **{title}** — Season {season_num}\nChoose an episode:",
        buttons.build_menu(6)
    )


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM RANGE INPUT HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def animesalt_range_input(client: Client, message: Message):
    """
    Receives the 'start,end' text from the user after they clicked Custom Range.
    Only acts when session has waiting_custom_range=True; ignores all other messages.
    """
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return

    session = _sessions.get(user_id)
    if not session or not session.get("waiting_custom_range"):
        return

    text = (message.text or "").strip()
    # Ignore command messages — let other handlers deal with them
    if not text or text.startswith("/"):
        return

    m = re.match(r"^(\d+)\s*,\s*(\d+)$", text)
    if not m:
        await sendMessage(message,
            "❌ Invalid format. Send like `1,13` or `22,25`\n"
            "_Start and end episode numbers separated by a comma._"
        )
        return

    start = int(m.group(1))
    end   = int(m.group(2))
    if start > end:
        await sendMessage(message, "❌ Start must be ≤ end. Example: `1,13`")
        return

    season_num = session["waiting_season_num"]
    episodes   = session["series_data"]["seasons"].get(season_num, {}).get("episodes", [])
    avail_nums = {e["num"] for e in episodes}
    valid_eps  = [n for n in range(start, end + 1) if n in avail_nums]

    if not valid_eps:
        ep_list = sorted(avail_nums)
        await sendMessage(message,
            f"❌ No episodes found in range **{start}–{end}** for Season {season_num}.\n"
            f"Available: {ep_list[0]}–{ep_list[-1]}"
        )
        return

    # Clear waiting state, set batch state (reuses full-season quality flow)
    del session["waiting_custom_range"]
    del session["waiting_season_num"]
    session["pending_ssn"]     = season_num
    session["custom_ep_range"] = valid_eps

    title   = session["series_data"]["title"]
    n_eps   = len(valid_eps)
    missing = [n for n in range(start, end + 1) if n not in avail_nums]
    note    = (f"\n⚠️ Not found (skipped): {missing}" if missing else "")
    buttons = _quality_buttons(user_id, "as_sq_")
    await sendMessage(message,
        f"🎌 **{title}** — Season {season_num}\n"
        f"📺 Range: **Ep {start}–{end}** → {n_eps} episode(s){note}\n\n"
        "🎬 Select quality to download:",
        buttons.build_menu(2)
    )


# ══════════════════════════════════════════════════════════════════════════════
# DEBUG COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def animesalt_test(client: Client, message: Message):
    """/animesalttest — connectivity check"""
    wait         = await sendMessage(message, "⏳ Testing animesalt.ac…")
    proxy_status = f"🔌 Proxy: `{_PROXY_URL[:40]}...`" if _PROXY_URL else "⚠️ No proxy"

    r = await _fetch(SEARCH_API, params={"search": "naruto", "type": "post", "per_page": 3})
    if not r:
        await editMessage(wait,
            f"❌ Blocked / failed.\n{proxy_status}\n\n"
            "Fix: add `ANIMESALR_PROXY=http://user:pass@host:port` to config.env")
        return
    try:
        data   = json.loads(r)
        titles = [x.get("title", "?") for x in data[:3]] if isinstance(data, list) else []
        await editMessage(wait,
            f"✅ **Connected!**\n{proxy_status}\n"
            f"🎬 Leech cmd: `/{LEECH_CMD}`\n\n" +
            ("\n".join(f"• {t}" for t in titles) if titles else f"`{r[:200]}`")
        )
    except Exception:
        await editMessage(wait, f"⚠️ Unexpected:\n`{r[:300]}`\n{proxy_status}")


async def animesalt_dbg(client: Client, message: Message):
    """/animesaltdbg <ep-slug>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await sendMessage(message, "Usage: `/animesaltdbg naruto-shippuden-1x1`")
        return

    ep_slug = args[1].strip().strip("/").split("/")[-1]
    ep_url  = f"{BASE_URL}/episode/{ep_slug}/"
    status  = await sendMessage(message, f"⏳ Fetching `{ep_slug}`…")

    html = await _fetch(ep_url, timeout=30)
    if not html:
        proxy_hint = (f"Proxy `{_PROXY_URL[:40]}...`" if _PROXY_URL
                      else "No proxy! Add `ANIMESALR_PROXY=...`")
        await editMessage(status, f"❌ **Blocked / timeout**\n{proxy_hint}")
        return

    soup = BeautifulSoup(html, "html.parser")
    out  = [f"✅ Page fetched ({len(html):,} chars)"]

    modal  = soup.find(id="mdl-download") or soup.find(class_="mdl-cn")
    out.append(f"{'✅' if modal else '❌'} #mdl-download modal in HTML")

    dl_div = soup.select_one(".download-links")
    out.append(f"{'✅' if dl_div else '❌'} .download-links div")
    if dl_div:
        rows = dl_div.find_all("tr")
        out.append(f"   └ {len(rows)} <tr> rows")
        for a in dl_div.find_all("a", href=True)[:5]:
            out.append(f"   └ <a> `{a['href'][:90]}`")

    raw = _parse_download_links_from_html(html)
    out.append(f"\n{'✅' if raw else '❌'} Tracker links found: {len(raw)}")
    for l in raw[:5]:
        q = l.get("quality", "?") or "?"
        out.append(f"  • [{q} {l.get('audio','?')}] `{l['tracker_url'][:80]}`")

    # Multilang-plyr check
    ml_raw = _extract_multilang_links(html)
    out.append(f"\n{'✅' if ml_raw else '❌'} Multilang-plyr links: {len(ml_raw)}")
    for l in ml_raw:
        out.append(f"  🌐 [{l.get('audio','?')}] `{l['tracker_url'][:80]}`")

    # CDN hash check
    cdn_hash = _extract_cdn_hash(html)
    out.append(f"\n{'✅' if cdn_hash else '❌'} CDN hash: `{cdn_hash or 'not found'}`")

    await editMessage(status,
        f"📋 **Debug: {ep_slug}**\n\n" + "\n".join(out) + "\n\n⏳ Resolving…"
    )

    if raw:
        resolved = await _resolve_all(raw)
        out.append(f"\n🎉 **{len(resolved)} resolved:**")
        for l in resolved:
            q = parse_quality(l.get("quality", "") + " " + l.get("audio", "")) or "?"
            out.append(f"  💾 [{q}] {l.get('server','?')} `{l.get('url','')[:70]}`")
        groups = group_links_by_quality(resolved)
        out.append("\n📊 Quality breakdown:")
        for k, v in sorted(groups.items()):
            lbl = QUALITY_LABELS.get(k, k + "p") if k else "Unknown"
            out.append(f"  {lbl}: {len(v)} link(s)")
    else:
        out.append("\n❌ **No tracker links in page HTML**")
        if modal:
            for a in modal.find_all("a", href=True)[:10]:
                out.append(f"  modal <a>: `{a['href'][:80]}`")
        out.append("⚠️ Page may require JS or login for download links")

    if ml_raw:
        ml_resolved = await _resolve_all(ml_raw)
        out.append(f"\n🌐 **Multilang resolved ({len(ml_resolved)}):**")
        for l in ml_resolved:
            out.append(f"  [{l.get('audio','?')}] {l.get('server','?')} `{l.get('url','')[:70]}`")

    if cdn_hash:
        out.append(f"\n⏳ Testing CDN API for hash `{cdn_hash}`…")
        await editMessage(status, f"📋 **Debug: {ep_slug}**\n\n" + "\n".join(out[:60]))
        m3u8 = await _resolve_cdn_m3u8(cdn_hash)
        if m3u8:
            out.append(f"✅ **CDN m3u8 resolved:**")
            out.append(f"  `{m3u8[:120]}`")
        else:
            out.append("❌ **CDN API failed** — cookie/session issue or IP blocked")

    await editMessage(status, f"📋 **Debug: {ep_slug}**\n\n" + "\n".join(out[:65]))


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER REGISTRATION
# ══════════════════════════════════════════════════════════════════════════════

# Full callback data routing — must also handle as_dl_ (legacy)
_original_callback = animesalt_callback

async def _full_callback(client: Client, query):
    data    = query.data
    user_id = query.from_user.id
    msg     = query.message

    if data.startswith("as_dl_"):
        await query.answer()
        await _cb_direct_download(client, msg, user_id, data)
        return

    await _original_callback(client, query)


bot.add_handler(MessageHandler(
    animesalt_cmd,
    filters=command(f"animesalt{CMD_SUFFIX}") & CustomFilters.authorized & ~CustomFilters.blacklisted
))
bot.add_handler(MessageHandler(
    animesalt_cmd,
    filters=command(f"am{CMD_SUFFIX}") & CustomFilters.authorized & ~CustomFilters.blacklisted
))
bot.add_handler(MessageHandler(
    animesalt_cmd,
    filters=command(f"anime{CMD_SUFFIX}") & CustomFilters.authorized & ~CustomFilters.blacklisted
))
bot.add_handler(MessageHandler(
    animesalt_test,
    filters=command(f"animesalttest{CMD_SUFFIX}") & CustomFilters.authorized & ~CustomFilters.blacklisted
))
bot.add_handler(MessageHandler(
    animesalt_dbg,
    filters=command(f"animesaltdbg{CMD_SUFFIX}") & CustomFilters.authorized & ~CustomFilters.blacklisted
))
bot.add_handler(MessageHandler(
    animesalt_range_input,
    filters=CustomFilters.authorized & ~CustomFilters.blacklisted
), group=1)
bot.add_handler(CallbackQueryHandler(
    _full_callback,
    filters=regex(r"^as_")
))
