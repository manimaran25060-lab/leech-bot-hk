#!/usr/bin/env python3
"""
mega_download.py â€” multi-backend Mega downloader
Priority: Mega C++ SDK  â†’  mega.py (pure Python)  â†’  megatools CLI  â†’  direct API

mega.py is a lightweight pure-Python Mega client that handles all public
mega.nz links with zero native dependencies:
    pip install mega.py>=1.0.8   â† IMPORTANT: 1.0.8+ fixes /file/ URL key bug

megatools is a CLI fallback (apt install megatools).

Backend D (direct) is a pure-Python fallback that implements the Mega API
protocol directly, bypassing all the above.  Requires: requests, pycryptodome.

NOTE: rclone copyurl is NOT used â€” it cannot handle mega.nz URLs.
Rclone needs a fully configured mega: remote, which is rarely set up.
"""

import re
import base64
import struct
import json as _json
import shutil
import asyncio
import os
from secrets import token_hex
from asyncio import Event
from aiofiles.os import makedirs

# â”€â”€ Backend A: Mega C++ SDK (original) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from mega import MegaApi, MegaListener, MegaRequest, MegaTransfer, MegaError
    # Verify it's the real C++ SDK, not the mega.py pip package
    if not hasattr(MegaApi, 'startDownload'):
        raise ImportError("mega module found but is not the C++ SDK")
    MEGA_SDK_AVAILABLE = True
except (ImportError, AttributeError):
    MEGA_SDK_AVAILABLE = False
    MegaApi = MegaListener = MegaRequest = MegaTransfer = MegaError = None

# â”€â”€ Backend B: mega.py pure-Python client â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from mega import Mega as MegaPy          # pip install mega.py
    MEGAPY_AVAILABLE = True
except (ImportError, AttributeError):
    MEGAPY_AVAILABLE = False
    MegaPy = None

# â”€â”€ Backend C: megatools CLI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _find_megatools_bin():
    # Standard PATH search first
    for name in ("megadl", "megatools"):
        found = shutil.which(name)
        if found:
            return found
    # Hardcoded fallbacks â€” megatools may be installed but not in container PATH
    for candidate in (
        "/usr/bin/megadl",       "/usr/local/bin/megadl",
        "/usr/bin/megatools",    "/usr/local/bin/megatools",
        "/snap/bin/megatools",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

MEGATOOLS_BIN = _find_megatools_bin()
MEGATOOLS_AVAILABLE = MEGATOOLS_BIN is not None

MEGA_AVAILABLE = MEGA_SDK_AVAILABLE or MEGAPY_AVAILABLE or MEGATOOLS_AVAILABLE

# â”€â”€ Bot imports â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from bot import LOGGER, config_dict, download_dict_lock, download_dict, \
    non_queued_dl, queue_dict_lock
from bot.helper.telegram_helper.message_utils import sendMessage, sendStatusMessage
from bot.helper.ext_utils.bot_utils import (
    get_mega_link_type, async_to_sync, sync_to_async
)
from bot.helper.mirror_utils.status_utils.mega_download_status import MegaDownloadStatus
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.ext_utils.task_manager import is_queued, limit_checker, stop_duplicate_check


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BACKEND A â€” Mega C++ SDK  (original, unchanged)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

if MEGA_SDK_AVAILABLE:
    class MegaAppListener(MegaListener):
        _NO_EVENT_ON = (MegaRequest.TYPE_LOGIN, MegaRequest.TYPE_FETCH_NODES)
        NO_ERROR = "no error"

        def __init__(self, continue_event: Event, listener):
            self.continue_event = continue_event
            self.node = None
            self.public_node = None
            self.listener = listener
            self.is_cancelled = False
            self.error = None
            self.__bytes_transferred = 0
            self.__speed = 0
            self.__name = ''
            super().__init__()

        @property
        def speed(self):
            return self.__speed

        @property
        def downloaded_bytes(self):
            return self.__bytes_transferred

        def onRequestFinish(self, api, request, error):
            if str(error).lower() != "no error":
                self.error = error.copy()
                LOGGER.error(f'Mega onRequestFinishError: {self.error}')
                self.continue_event.set()
                return
            request_type = request.getType()
            if request_type == MegaRequest.TYPE_LOGIN:
                api.fetchNodes()
            elif request_type == MegaRequest.TYPE_GET_PUBLIC_NODE:
                self.public_node = request.getPublicMegaNode()
                self.__name = self.public_node.getName()
            elif request_type == MegaRequest.TYPE_FETCH_NODES:
                LOGGER.info("Fetching Root Node.")
                self.node = api.getRootNode()
                self.__name = self.node.getName()
                LOGGER.info(f"Node Name: {self.node.getName()}")
            if request_type not in self._NO_EVENT_ON or \
                    self.node and "cloud drive" not in self.__name.lower():
                self.continue_event.set()

        def onRequestTemporaryError(self, api, request, error: MegaError):
            LOGGER.error(f'Mega Request error in {error}')
            if not self.is_cancelled:
                self.is_cancelled = True
                async_to_sync(self.listener.onDownloadError,
                              f"RequestTempError: {error.toString()}")
            self.error = error.toString()
            self.continue_event.set()

        def onTransferUpdate(self, api: MegaApi, transfer: MegaTransfer):
            if self.is_cancelled:
                api.cancelTransfer(transfer, None)
                self.continue_event.set()
                return
            self.__speed = transfer.getSpeed()
            self.__bytes_transferred = transfer.getTransferredBytes()

        def onTransferFinish(self, api: MegaApi, transfer: MegaTransfer, error):
            try:
                if self.is_cancelled:
                    self.continue_event.set()
                elif transfer.isFinished() and (
                    transfer.isFolderTransfer()
                    or transfer.getFileName() == self.__name
                ):
                    async_to_sync(self.listener.onDownloadComplete)
                    self.continue_event.set()
            except Exception as e:
                LOGGER.error(e)

        def onTransferTemporaryError(self, api, transfer, error):
            filen  = transfer.getFileName()
            state  = transfer.getState()
            errStr = error.toString()
            LOGGER.error(f'Mega download error in file {transfer} {filen}: {error}')
            if state in [1, 4]:
                return
            self.error = errStr
            if not self.is_cancelled:
                self.is_cancelled = True
                async_to_sync(self.listener.onDownloadError,
                              f"TransferTempError: {errStr} ({filen})")
                self.continue_event.set()

        async def cancel_download(self):
            self.is_cancelled = True
            await self.listener.onDownloadError("Download Canceled by user")


class AsyncExecutor:
    def __init__(self):
        self.continue_event = Event()

    async def do(self, function, args):
        self.continue_event.clear()
        await sync_to_async(function, *args)
        await self.continue_event.wait()


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BACKEND B â€” mega.py pure Python  (no native libs needed)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _mega_download_megapy(mega_link: str, path: str,
                                  name: str | None,
                                  helper=None) -> tuple[bool, str]:
    """
    Download using the mega.py library (pip install mega.py).
    Works for all public mega.nz/file/ and mega.nz/folder/ links.
    Returns (success: bool, error_message: str).

    If *helper* (_MegaCliHelper) is provided, its total_bytes field is
    updated after the file size is resolved so the status card shows %/ETA.
    """
    if not MEGAPY_AVAILABLE:
        return False, "mega.py not installed"

    email    = config_dict.get("MEGA_EMAIL",    "")
    password = config_dict.get("MEGA_PASSWORD", "")

    def _do():
        try:
            m = MegaPy()
            if email and password:
                m = m.login(email, password)
                LOGGER.info("[MegaDownload] mega.py: logged in with credentials")
            else:
                m = m.login()          # anonymous login â€” works for public links
                LOGGER.info("[MegaDownload] mega.py: anonymous login")

            # â”€â”€ Probe file size so the status card can show % and ETA â”€â”€â”€â”€â”€â”€â”€â”€
            if helper is not None:
                try:
                    info = m.get_public_url_info(mega_link)
                    total = int(info.get("size", 0))
                    if total > 0:
                        helper.total_bytes = total
                        LOGGER.info(f"[MegaDownload] mega.py file size: {total} bytes")
                except Exception as probe_err:
                    LOGGER.warning(f"[MegaDownload] mega.py size probe failed: {probe_err}")

            os.makedirs(path, exist_ok=True)
            LOGGER.info(f"[MegaDownload] mega.py downloading: {mega_link[:70]}")
            m.download_url(mega_link, dest_path=path, dest_filename=name or None)
            return True, ""
        except IndexError as e:
            # â”€â”€ Root-cause: mega.py < 1.0.8 key-parsing bug â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # Old mega.py splits the base64 key string at character 22:
            #   k = [base64_to_a32(key[:22]), base64_to_a32(key[22:])]
            # For a 43-char key, key[22:] = 21 chars â†’ only 3 uint32s.
            # Then `iv = (k[1][4], k[1][5], 0, 0)` raises IndexError.
            # Fix: pip install --upgrade 'mega.py>=1.0.8'
            LOGGER.error(f"[MegaDownload] mega.py IndexError (key-split bug in old mega.py): {e}")
            return False, (
                f"tuple index out of range â€” mega.py key-parsing bug. "
                f"Fix: pip install --upgrade 'mega.py>=1.0.8'"
            )
        except UnboundLocalError as e:
            # Known mega.py bug: local variable 'i' referenced before assignment
            # Happens when file chunk list is empty (expired/invalid link).
            # Fix: pip install --upgrade mega.py
            err = f"mega.py internal error ({e}). Upgrade: pip install --upgrade mega.py"
            LOGGER.error(f"[MegaDownload] mega.py UnboundLocalError (known bug): {e}")
            return False, err
        except Exception as e:
            err = str(e)
            LOGGER.error(f"[MegaDownload] mega.py failed: {err}")
            return False, err

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _do),
            timeout=3600,   # 1-hour hard cap
        )
    except asyncio.TimeoutError:
        return False, "mega.py timed out after 1 hour"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BACKEND C â€” megatools CLI
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _mega_download_megatools(mega_link: str, path: str,
                                     name: str | None,
                                     helper=None) -> tuple[bool, str]:
    """
    Download using the megatools CLI (apt install megatools).
    Parses real-time progress from megatools stderr output line by line
    and updates helper.downloaded_bytes, helper.total_bytes, helper.speed
    so the status card shows live progress/speed/ETA.
    Returns (success, error_message).
    """
    import re

    if not MEGATOOLS_AVAILABLE:
        return False, "megatools not installed (apt install megatools)"

    await makedirs(path, exist_ok=True)

    email    = config_dict.get("MEGA_EMAIL",    "")
    password = config_dict.get("MEGA_PASSWORD", "")

    cmd = [MEGATOOLS_BIN]
    if MEGATOOLS_BIN.endswith("megatools"):
        cmd.append("dl")

    if email and password:
        cmd += ["--username", email, "--password", password]

    cmd += ["--path", path, mega_link]

    # megatools progress line format:
    # filename: 44.80% - 297.0 MiB (311459800 bytes) of 663.0 MiB (16.1 MiB/s)
    _PROGRESS_RE = re.compile(
        r"([\d.]+)%\s*-\s*[\d.]+ \w+\s*\((\d+)\s*bytes\)\s*of\s*[\d.]+ \w+\s*\(([\d.]+)\s*(\w+)/s\)"
    )
    _UNIT = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}

    LOGGER.info(f"[MegaDownload] megatools: {' '.join(cmd[:4])}â€¦ {mega_link[:60]}")
    err_lines = []
    try:
        # Force the newer /usr/local/lib libcurl (built with Hashcash/curl_easy_header
        # support) to take priority over the older system libcurl the dynamic
        # linker would otherwise pick up.
        _env = os.environ.copy()
        _env["LD_LIBRARY_PATH"] = "/usr/local/lib:" + _env.get("LD_LIBRARY_PATH", "")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            env=_env,
        )

        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            if "509" in line and ("over quota" in line.lower() or "quota" in line.lower()):
                # megatools retries this forever with exponential backoff (2s,
                # 4s, 8s, 16s, ...) which looks identical to a hang. Bail out
                # immediately instead of waiting through endless retries.
                LOGGER.warning(f"[MegaDownload] 509 over-quota detected, aborting retry loop: {line}")
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return False, "509 (bandwidth quota exceeded)"
            m = _PROGRESS_RE.search(line)
            if m and helper is not None:
                downloaded = int(m.group(2))
                speed_val  = float(m.group(3))
                speed_unit = m.group(4)
                speed_bps  = speed_val * _UNIT.get(speed_unit, 1)
                # Derive total from downloaded + percentage
                pct = float(m.group(1))
                if pct > 0:
                    helper.total_bytes     = int(downloaded * 100 / pct)
                helper.downloaded_bytes    = downloaded
                helper.speed               = speed_bps
            else:
                err_lines.append(line)

        await proc.wait()
        if proc.returncode == 0:
            return True, ""
        err = "\n".join(err_lines).strip()
        LOGGER.error(f"[MegaDownload] megatools rc={proc.returncode}: {err}")
        return False, err
    except Exception as e:
        LOGGER.error(f"[MegaDownload] megatools exception: {e}")
        return False, str(e)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# COMMON QUEUE / LIMIT HELPERS for CLI backends
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class _MegaCliHelper:
    """
    Real-time progress tracker for the mega.py / megatools CLI backends.

    mega.py runs blocking in a thread-executor and has no progress callback,
    but it writes the file incrementally to disk.  We poll the destination
    directory every second and derive downloaded_bytes + speed from the
    growing file size, so the status message shows live numbers instead of 0.
    """

    _POLL_INTERVAL = 1.0   # seconds between disk polls

    def __init__(self):
        self.downloaded_bytes: int = 0
        self.total_bytes: int = 0      # set by _mega_download_megapy after probing
        self.speed: float = 0.0
        self.is_cancelled: bool = False
        self._watch_path: str | None = None
        self._monitor_task: asyncio.Task | None = None

    # â”€â”€ public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start_monitoring(self, path: str) -> None:
        """Begin background polling of *path* for a growing file."""
        self._watch_path = path
        self._monitor_task = asyncio.get_event_loop().create_task(
            self._poll_loop(), name="mega_cli_monitor"
        )

    def stop_monitoring(self) -> None:
        """Cancel the polling task (called after download finishes/errors)."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()

    async def cancel_download(self) -> None:
        self.is_cancelled = True
        self.stop_monitoring()

    # â”€â”€ internals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _dir_size(path: str) -> int:
        """
        Sum of all file sizes recursively under *path*.

        Recursive walk catches mega.py temp subdirs and partial files
        (.part, .tmp) so the status card shows live progress where possible.
        Note: mega.py decrypts files in-memory before writing, so for
        smaller files progress may jump from 0% to 100% at the end.
        """
        total = 0
        try:
            for dirpath, _dirs, filenames in os.walk(path):
                for fname in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, fname))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    @staticmethod
    def _get_net_rx() -> int:
        """Get total system received bytes via psutil (most reliable)."""
        try:
            import psutil
            c = psutil.net_io_counters()
            return c.bytes_recv if c else 0
        except Exception:
            total = 0
            try:
                with open('/proc/net/dev') as f:
                    for line in f:
                        if ':' not in line:
                            continue
                        iface, data = line.split(':', 1)
                        if iface.strip() == 'lo':
                            continue
                        fields = data.split()
                        if fields:
                            total += int(fields[0])
            except Exception:
                pass
            return total

    async def _poll_loop(self) -> None:
        prev_disk = 0
        prev_net  = self._get_net_rx()
        while not self.is_cancelled:
            await asyncio.sleep(self._POLL_INTERVAL)

            # Check dest dir + /tmp for growing files (mega.py writes temp
            # encrypted chunks to /tmp before decrypting to dest)
            disk = self._dir_size(self._watch_path) if self._watch_path else 0

            net_now   = self._get_net_rx()
            net_delta = max(0, net_now - prev_net)
            prev_net  = net_now

            if disk > 0:
                # Decrypted file hit dest dir â€” track disk growth
                delta      = max(0, disk - prev_disk)
                self.speed = delta / self._POLL_INTERVAL
                self.downloaded_bytes = disk
                prev_disk  = disk
            elif net_delta > 0:
                # Still in memory â€” use network RX bytes for live speed
                self.speed             = net_delta / self._POLL_INTERVAL
                self.downloaded_bytes += net_delta


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BACKEND D â€” direct Mega API  (pure Python, no mega.py / megatools needed)
# Fixes the key-parsing IndexError in mega.py < 1.0.8 by implementing the
# Mega protocol from scratch using requests + pycryptodome.
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _parse_mega_url(url: str):
    """
    Parse a mega.nz /file/ or /#! URL.
    Returns (file_id, aes_key_bytes_16, iv_int) or raises ValueError.

    The 43-char base64url key in the URL encodes 32 bytes:
      bytes  0-15 = file_key_part1 (XOR'd with part2 to get AES key)
      bytes 16-31 = file_key_part2 (IV in first 8 bytes)

    ROOT CAUSE of 'tuple index out of range':
      Old mega.py splits at character 22 of the base64 string, which does NOT
      align to the 16-byte boundary.  key[22:] = 21 chars â†’ 3 uint32s only.
      Then `iv = (k[1][4], k[1][5], 0, 0)` crashes with IndexError.
      Fix: always decode full 32 bytes first, THEN split at uint32 boundary.
    """
    m = re.match(r'https?://mega\.nz/file/([^#]+)#(.+)', url)
    if m:
        file_id, key_b64 = m.group(1), m.group(2)
    else:
        m = re.match(r'https?://mega\.nz/#!([^!]+)!(.+)', url)
        if not m:
            raise ValueError(f"Unsupported Mega URL format: {url}")
        file_id, key_b64 = m.group(1), m.group(2)

    padded    = key_b64 + '=' * (-len(key_b64) % 4)
    key_bytes = base64.urlsafe_b64decode(padded)[:32]
    if len(key_bytes) < 32:
        raise ValueError(f"Mega key too short after decode: {len(key_bytes)} bytes")

    k       = struct.unpack('>8I', key_bytes)          # 8 Ã— uint32
    aes_key = struct.pack('>4I',
        k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
    iv_int  = (k[4] << 96) | (k[5] << 64)             # upper 64 bits of CTR

    return file_id, aes_key, iv_int


async def _mega_download_direct(mega_link: str, path: str,
                                 name: str | None,
                                 helper=None) -> tuple[bool, str]:
    """
    Backend D: direct Mega API download â€” no mega.py or megatools required.
    Requires: requests, pycryptodome.

    This is the definitive fix for:
      'Mega download failed: tuple index out of range'
    caused by mega.py < 1.0.8 incorrectly splitting the /file/ URL key.
    """
    try:
        import requests
        from Crypto.Cipher import AES
        from Crypto.Util import Counter as _Counter
    except ImportError as ie:
        return False, f"Backend D unavailable: {ie} â€” need requests + pycryptodome"

    def _do_direct():
        try:
            file_id, aes_key, iv_int = _parse_mega_url(mega_link)
        except ValueError as e:
            return False, str(e)

        # â”€â”€ 1. Ask Mega API for the download URL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        try:
            resp = requests.post(
                'https://g.api.mega.co.nz/cs',
                data=_json.dumps([{'a': 'g', 'g': 1, 'p': file_id}]),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            return False, f"Mega API request failed: {e}"

        if isinstance(data, list):
            data = data[0]
        if isinstance(data, int):
            _code_hints = {
                -2:  "file not found or link expired",
                -3:  "temporary error, try again",
                -6:  "too many requests",
                -9:  "IP/account blocked â€” Oracle Cloud IPs are often banned by Mega",
                -16: "user blocked",
            }
            hint = _code_hints.get(data, "")
            return False, f"Mega API error {data}" + (f": {hint}" if hint else "")

        download_url = data.get('g')
        file_size    = int(data.get('s', 0))
        attr_b64     = data.get('at', '')

        if not download_url:
            return False, f"Mega API gave no download URL â€” response: {data}"

        # â”€â”€ 2. Decrypt filename from encrypted attributes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        resolved_name = name
        if not resolved_name and attr_b64:
            try:
                attr_enc  = base64.urlsafe_b64decode(attr_b64 + '=' * (-len(attr_b64) % 4))
                cipher    = AES.new(aes_key, AES.MODE_CBC, iv=b'\x00' * 16)
                attr_dec  = cipher.decrypt(attr_enc).rstrip(b'\x00')
                attr_str  = attr_dec.decode('utf-8', errors='ignore')
                if attr_str.startswith('MEGA{'):
                    resolved_name = _json.loads(attr_str[4:]).get('n')
            except Exception as ae:
                LOGGER.warning(f"[MegaDownload-Direct] attr decrypt: {ae}")

        resolved_name = resolved_name or file_id
        dest_file     = os.path.join(path, resolved_name)
        os.makedirs(path, exist_ok=True)

        if helper and file_size:
            helper.total_bytes = file_size

        # â”€â”€ 3. Stream-download + AES-CTR decrypt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        LOGGER.info(f"[MegaDownload-Direct] {resolved_name} ({file_size} bytes)")
        try:
            ctr    = _Counter.new(128, initial_value=iv_int)
            cipher = AES.new(aes_key, AES.MODE_CTR, counter=ctr)
            with requests.get(download_url, stream=True, timeout=60) as r:
                if r.status_code == 509:
                    return False, "509 Bandwidth Limit Exceeded â€” this file's transfer quota is used up"
                r.raise_for_status()
                downloaded = 0
                with open(dest_file, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        if chunk:
                            f.write(cipher.decrypt(chunk))
                            downloaded += len(chunk)
                            if helper:
                                helper.downloaded_bytes = downloaded
        except Exception as e:
            try:
                os.remove(dest_file)
            except OSError:
                pass
            return False, f"Download/decrypt error: {e}"

        LOGGER.info(f"[MegaDownload-Direct] done â†’ {dest_file}")
        return True, ""

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _do_direct),
            timeout=3600,
        )
    except asyncio.TimeoutError:
        return False, "direct download timed out after 1 hour"


async def _run_cli_download(mega_link: str, path: str, listener, name: str):
    """Shared wrapper for mega.py + megatools: handles queue, limits, status."""
    display_name = name or "mega_file"

    msg, button = await stop_duplicate_check(display_name, listener)
    if msg:
        await sendMessage(listener.message, msg, button)
        return

    gid  = token_hex(5)
    size = 0    # CLI backends don't expose file size upfront

    if limit_exceeded := await limit_checker(size, listener, isMega=True):
        await sendMessage(listener.message, limit_exceeded)
        return

    added_to_queue, event = await is_queued(listener.uid)
    if added_to_queue:
        LOGGER.info(f"[MegaDownload] Queued: {display_name}")
        async with download_dict_lock:
            download_dict[listener.uid] = QueueStatus(
                display_name, size, gid, listener, 'Dl'
            )
        await listener.onDownloadStart()
        await sendStatusMessage(listener.message)
        await event.wait()
        async with download_dict_lock:
            if listener.uid not in download_dict:
                return

    async with queue_dict_lock:
        non_queued_dl.add(listener.uid)

    # â”€â”€ Register in download_dict BEFORE onDownloadStart so that
    #    tasks_listener.onDownloadComplete() can find download_dict[uid].
    #    The CLI backends block in a thread and can't stream byte counts,
    #    so _MegaCliHelper provides zero-valued stubs that are safe to call.
    cli_helper = _MegaCliHelper()
    async with download_dict_lock:
        download_dict[listener.uid] = MegaDownloadStatus(
            display_name, size, gid, cli_helper, listener.message, listener.upload_details
        )

    await listener.onDownloadStart()
    await sendStatusMessage(listener.message)

    success, err = False, ""
    errors = []

    # megatools first — native C, 16MB/s on ARM, real-time progress parsing
    if MEGATOOLS_AVAILABLE:
        LOGGER.info("[MegaDownload] Trying megatools backend")
        success, err = await _mega_download_megatools(mega_link, path, name, helper=cli_helper)
        if not success and err:
            errors.append(f"megatools: {err}")

    # Fall back to mega.py if megatools failed
    if not success and MEGAPY_AVAILABLE:
        LOGGER.info("[MegaDownload] Trying mega.py backend")
        cli_helper.start_monitoring(path)
        success, err = await _mega_download_megapy(mega_link, path, name, helper=cli_helper)
        if not success and err:
            errors.append(f"mega.py: {err}")

    # Backend D: direct Mega API — only supports /file/ or /#! links (see
    # _parse_mega_url), so it can never handle /folder/ links. Skip it for
    # those instead of producing a confusing "unsupported format" error.
    if not success and get_mega_link_type(mega_link) == "file":
        LOGGER.info("[MegaDownload] mega.py/megatools failed, trying direct API backend")
        cli_helper.stop_monitoring()
        success, err = await _mega_download_direct(mega_link, path, name, helper=cli_helper)
        if not success and err:
            errors.append(f"direct: {err}")

    cli_helper.stop_monitoring()

    async with queue_dict_lock:
        non_queued_dl.discard(listener.uid)

    if success:
        await listener.onDownloadComplete()
    else:
        hint = ""
        if not MEGAPY_AVAILABLE:
            hint = "\n\nðŸ’¡ Add `mega.py>=1.0.8` to requirements.txt and rebuild."
        elif "key-parsing bug" in err or "tuple index" in err:
            hint = "\n\nðŸ’¡ Fix: upgrade mega.py â€” `pip install --upgrade 'mega.py>=1.0.8'`"
        elif "error -9" in err or "banned" in err.lower() or "blocked" in err.lower():
            hint = (
                "\n\nðŸ’¡ Mega.nz may be blocking this server's IP (Oracle Cloud is often banned). "
                "Try routing Mega traffic through a proxy or use a residential IP."
            )
        elif "509" in err or "bandwidth" in err.lower():
            hint = (
                "\n\nðŸ’¡ This Mega link's bandwidth quota is exhausted. "
                "The file owner needs to wait for it to reset (usually 6 hours), "
                "or try a different Mega link / source."
            )
        full_err = "\n".join(errors) if errors else err
        await listener.onDownloadError(f"❌ Mega download failed:\n{full_err}{hint}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PUBLIC API
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def add_mega_download(mega_link: str, path: str, listener, name: str):
    """
    Entry point for all Mega downloads.

    Backend priority:
      1. Mega C++ SDK  â€” if Python bindings are compiled + installed
      2. mega.py       â€” pure Python, pip install mega.py  â† easiest fix
      3. megatools     â€” CLI, apt install megatools
    """
    if not MEGA_AVAILABLE:
        await sendMessage(
            listener.message,
            "âŒ No Mega backend available.\n\n"
            "**Quick fix:** add `mega.py` to requirements.txt and rebuild:\n"
            "`pip install mega.py`"
        )
        return

    LOGGER.info(
        f"[MegaDownload] backends available â€” "
        f"SDK={MEGA_SDK_AVAILABLE} mega.py={MEGAPY_AVAILABLE} "
        f"megatools={MEGATOOLS_AVAILABLE}"
    )

    if MEGA_SDK_AVAILABLE:
        try:
            await _add_mega_download_sdk(mega_link, path, listener, name)
        except Exception as sdk_err:
            LOGGER.error(
                f"[MegaDownload] C++ SDK failed ({sdk_err}), "
                "falling back to mega.py / megatools"
            )
            await _run_cli_download(mega_link, path, listener, name)
    else:
        await _run_cli_download(mega_link, path, listener, name)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ORIGINAL SDK IMPLEMENTATION (unchanged logic)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _add_mega_download_sdk(mega_link: str, path: str, listener, name: str):
    MEGA_EMAIL    = config_dict['MEGA_EMAIL']
    MEGA_PASSWORD = config_dict['MEGA_PASSWORD']

    executor   = AsyncExecutor()
    api        = MegaApi(None, None, None, 'Thiru-ML')
    folder_api = None

    mega_listener = MegaAppListener(executor.continue_event, listener)
    api.addListener(mega_listener)

    if MEGA_EMAIL and MEGA_PASSWORD:
        await executor.do(api.login, (MEGA_EMAIL, MEGA_PASSWORD))

    if get_mega_link_type(mega_link) == "file":
        await executor.do(api.getPublicNode, (mega_link,))
        node = mega_listener.public_node
    else:
        folder_api = MegaApi(None, None, None, 'Thiru-ML')
        folder_api.addListener(mega_listener)
        await executor.do(folder_api.loginToFolder, (mega_link,))
        node = await sync_to_async(folder_api.authorizeNode, mega_listener.node)

    if mega_listener.error is not None:
        await sendMessage(listener.message, str(mega_listener.error))
        await executor.do(api.logout, ())
        if folder_api is not None:
            await executor.do(folder_api.logout, ())
        return

    name = name or node.getName()
    msg, button = await stop_duplicate_check(name, listener)
    if msg:
        await sendMessage(listener.message, msg, button)
        await executor.do(api.logout, ())
        if folder_api is not None:
            await executor.do(folder_api.logout, ())
        return

    gid  = token_hex(5)
    size = api.getSize(node)
    if limit_exceeded := await limit_checker(size, listener, isMega=True):
        await sendMessage(listener.message, limit_exceeded)
        return

    added_to_queue, event = await is_queued(listener.uid)
    if added_to_queue:
        LOGGER.info(f"Added to Queue/Download: {name}")
        async with download_dict_lock:
            download_dict[listener.uid] = QueueStatus(name, size, gid, listener, 'Dl')
        await listener.onDownloadStart()
        await sendStatusMessage(listener.message)
        await event.wait()
        async with download_dict_lock:
            if listener.uid not in download_dict:
                await executor.do(api.logout, ())
                if folder_api is not None:
                    await executor.do(folder_api.logout, ())
                return
        LOGGER.info(f'Start Queued Download from Mega: {name}')

    async with download_dict_lock:
        download_dict[listener.uid] = MegaDownloadStatus(
            name, size, gid, mega_listener, listener.message, listener.upload_details
        )
    async with queue_dict_lock:
        non_queued_dl.add(listener.uid)

    await listener.onDownloadStart()
    await sendStatusMessage(listener.message)
    LOGGER.info(f"Download from Mega SDK: {name}")

    await makedirs(path, exist_ok=True)
    await executor.do(api.startDownload, (node, path, name, None, False, None))
    await executor.do(api.logout, ())
    if folder_api is not None:
        await executor.do(folder_api.logout, ())
