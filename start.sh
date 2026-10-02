python3 -c "
f = '/usr/local/lib/python3.10/dist-packages/pyrogram/utils.py'
try:
    with open(f, 'r') as file:
        content = file.read()
    if 'MIN_CHANNEL_ID = -1002147483647' in content:
        content = content.replace('MIN_CHANNEL_ID = -1002147483647', 'MIN_CHANNEL_ID = -1009999999999999')
        with open(f, 'w') as file:
            file.write(content)
        print('Pyrogram MIN_CHANNEL_ID patched')
    else:
        print('Pyrogram MIN_CHANNEL_ID already patched or not found')
except FileNotFoundError:
    print('Pyrogram utils.py not found, skipping patch')
"

python3 update.py

# Ensure all Python requirements are installed (covers Oracle VM where Heroku
# auto-install doesn't happen â€” specifically needed for mega.py)
pip3 install -r requirements.txt --quiet --no-warn-script-location 2>&1 | tail -5

# Fix ffmpeg version parsing IndexError
python3 -c "
import os
f = 'bot/helper/ext_utils/bot_utils.py'
if os.path.exists(f):
    c = open(f).read()
    old = \"vf = result.stdout.split('\\\\n')[0].split(' ')[2].split('ubuntu')[0]\\n    except FileNotFoundError:\\n        vf = ''\"
    new = \"vf_raw = result.stdout.split('\\\\n')[0].split(' ')\\n        vf = vf_raw[2].split('ubuntu')[0].split('-')[0] if len(vf_raw) > 2 else 'N/A'\\n    except (FileNotFoundError, IndexError):\\n        vf = 'N/A'\"
    if old in c:
        open(f, 'w').write(c.replace(old, new))
        print('ffmpeg fix applied')
    else:
        print('ffmpeg already fixed or not found')
"

# â”€â”€ FIX 1: Premium session â€” move user client start after bot_loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 - << 'PATCH'
with open('bot/__init__.py', 'r') as f:
    c = f.read()

c = c.replace(
    "IS_PREMIUM_USER = False\nuser = ''\nUSER_SESSION_STRING = environ.get('USER_SESSION_STRING', '')\nif len(USER_SESSION_STRING) != 0:\n    log_info(\"Creating client from USER_SESSION_STRING\")\n    try:\n        user = tgClient(':memory:', TELEGRAM_API, TELEGRAM_HASH, session_string=USER_SESSION_STRING,\n                        parse_mode=enums.ParseMode.HTML, no_updates=True, sleep_threshold=60,\n                        workers=1000).start()\n        IS_PREMIUM_USER = True\n    except Exception as e:\n        log_error(f\"Failed making client from USER_SESSION_STRING : {e}\")\n        user = ''",
    "IS_PREMIUM_USER = False\nuser = ''\nUSER_SESSION_STRING = environ.get('USER_SESSION_STRING', '')"
)

c = c.replace(
    "MAX_SPLIT_SIZE = 4194304000 if IS_PREMIUM_USER else 2097152000\n\nLEECH_SPLIT_SIZE = environ.get('LEECH_SPLIT_SIZE', '')\nif str(LEECH_SPLIT_SIZE) in [\"4194304000\", \"2097152000\"] or len(LEECH_SPLIT_SIZE) == 0 or int(LEECH_SPLIT_SIZE) > MAX_SPLIT_SIZE:\n    LEECH_SPLIT_SIZE = MAX_SPLIT_SIZE\nelse:\n    LEECH_SPLIT_SIZE = int(LEECH_SPLIT_SIZE)",
    "MAX_SPLIT_SIZE = 2097152000  # updated after premium check\n\n_ENV_LEECH_SPLIT = environ.get('LEECH_SPLIT_SIZE', '')\nLEECH_SPLIT_SIZE = MAX_SPLIT_SIZE  # updated after premium check"
)

c = c.replace(
    "log_info(\"Creating client from BOT_TOKEN\")\nbot = tgClient('bot', TELEGRAM_API, TELEGRAM_HASH, bot_token=BOT_TOKEN, workers=1000,\n               parse_mode=enums.ParseMode.HTML, sleep_threshold=60).start()\nbot_loop = bot.loop\nbot_name = bot.me.username\nscheduler = AsyncIOScheduler(timezone=str(get_localzone()), event_loop=bot_loop)",
    """log_info("Creating client from BOT_TOKEN")
bot = tgClient('bot', TELEGRAM_API, TELEGRAM_HASH, bot_token=BOT_TOKEN, workers=1000,
               parse_mode=enums.ParseMode.HTML, sleep_threshold=60).start()
bot_loop = bot.loop
bot_name = bot.me.username
scheduler = AsyncIOScheduler(timezone=str(get_localzone()), event_loop=bot_loop)

if len(USER_SESSION_STRING) != 0:
    async def _start_user_client():
        global user, IS_PREMIUM_USER, MAX_SPLIT_SIZE, LEECH_SPLIT_SIZE
        log_info("Creating client from USER_SESSION_STRING")
        try:
            _client = tgClient(':memory:', TELEGRAM_API, TELEGRAM_HASH,
                               session_string=USER_SESSION_STRING,
                               parse_mode=enums.ParseMode.HTML,
                               no_updates=True, sleep_threshold=60,
                               workers=1000)
            await _client.start()
            user = _client
            IS_PREMIUM_USER = True
            MAX_SPLIT_SIZE = 4194304000
            _env = _ENV_LEECH_SPLIT
            if str(_env) in ["4194304000", "2097152000"] or len(_env) == 0 or int(_env) > MAX_SPLIT_SIZE:
                LEECH_SPLIT_SIZE = MAX_SPLIT_SIZE
            else:
                LEECH_SPLIT_SIZE = int(_env)
            if 'config_dict' in globals():
                config_dict['LEECH_SPLIT_SIZE'] = LEECH_SPLIT_SIZE
            log_info("Premium user client started. MAX_SPLIT_SIZE set to 4 GB.")
        except Exception as e:
            log_error(f"Failed making client from USER_SESSION_STRING : {e}")
            user = ''
    bot_loop.run_until_complete(_start_user_client())"""
)

with open('bot/__init__.py', 'w') as f:
    f.write(c)
print("__init__.py patched:", "Premium user client" in c)
PATCH

# â”€â”€ FIX 2: Show MAX_SPLIT_SIZE in user settings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 -c "
with open('bot/modules/users_settings.py', 'r') as f:
    c = f.read()
c = c.replace(
    \"get_readable_file_size(config_dict['LEECH_SPLIT_SIZE']) + ' (Default)'\",
    \"get_readable_file_size(MAX_SPLIT_SIZE) + ' (Default)'\"
)
with open('bot/modules/users_settings.py', 'w') as f:
    f.write(c)
print('users_settings.py patched')
"

# â”€â”€ FIX 3: tasks_listener â€” always use MAX_SPLIT_SIZE as ceiling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 -c "
with open('bot/helper/listeners/tasks_listener.py', 'r') as f:
    c = f.read()
c = c.replace(
    \"LEECH_SPLIT_SIZE = min(user_dict.get('split_size', False) or config_dict['LEECH_SPLIT_SIZE'], MAX_SPLIT_SIZE)\n            cmd = [\\\"7z\\\"\",
    \"_user_split = user_dict.get('split_size', False)\n            LEECH_SPLIT_SIZE = min(_user_split, MAX_SPLIT_SIZE) if _user_split else MAX_SPLIT_SIZE\n            cmd = [\\\"7z\\\"\"
)
c = c.replace(
    \"checked = False\n                LEECH_SPLIT_SIZE = min(\n                    user_dict.get('split_size', False) or config_dict['LEECH_SPLIT_SIZE'], MAX_SPLIT_SIZE)\",
    \"checked = False\n                _user_split = user_dict.get('split_size', False)\n                LEECH_SPLIT_SIZE = min(_user_split, MAX_SPLIT_SIZE) if _user_split else MAX_SPLIT_SIZE\"
)
with open('bot/helper/listeners/tasks_listener.py', 'w') as f:
    f.write(c)
print('tasks_listener.py patched')
"

# â”€â”€ FIX 4: leech_utils â€” ffmpeg size-based split â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 -c "
with open('bot/helper/ext_utils/leech_utils.py', 'r') as f:
    c = f.read()
c = c.replace(
    \"        if total_dur and parts > 1:\n            seg = total_dur / parts\n            for i in range(parts):\n                out_path = f\\\"{dirpath}/{base}.part{i+1:03d}.{ext}\\\"\n                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',\n                       '-ss', str(i * seg), '-i', path,\n                       '-t', str(seg), '-map', '0', '-c', 'copy',\n                       '-avoid_negative_ts', 'make_zero', out_path]\",
    \"        if total_dur and parts > 1:\n            seg = (split_size / size) * total_dur\n            for i in range(parts):\n                out_path = f\\\"{dirpath}/{base}.part{i+1:03d}.{ext}\\\"\n                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',\n                       '-ss', str(i * seg), '-i', path,\n                       '-t', str(seg), '-map', '0', '-c', 'copy',\n                       '-avoid_negative_ts', 'make_zero',\n                       '-fs', str(split_size), out_path]\"
)
with open('bot/helper/ext_utils/leech_utils.py', 'w') as f:
    f.write(c)
print('leech_utils.py patched')
"

# â”€â”€ FIX 5: lk21 bypasser IPv6 crash â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 -c "
import site, os
dirs = []
try:
    dirs += site.getsitepackages()
except:
    pass
try:
    dirs.append(site.getusersitepackages())
except:
    pass
for d in dirs:
    f = os.path.join(d, 'lk21/extractors/bypasser.py')
    if os.path.exists(f):
        c = open(f).read()
        old = '''                for url in exrex.generate(valid_regex):
                    Bypass.bypassPattern[key][\"support\"].add(
                        removeprefix(urlparse(url if url.startswith((\"http\",\"//\")) else \"http://\" + url).netloc, \"www.\"))'''
        new = '''                for url in exrex.generate(valid_regex):
                    try:
                        Bypass.bypassPattern[key][\"support\"].add(
                            removeprefix(urlparse(url if url.startswith((\"http\",\"//\")) else \"http://\" + url).netloc, \"www.\"))
                    except ValueError:
                        pass'''
        if old in c:
            open(f,'w').write(c.replace(old,new))
            print('lk21 bypasser fixed')
        else:
            print('lk21 already fixed')
        break
"

# â”€â”€ FIX 6: Mega download â€” real-time speed via psutil network tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€
python3 -c "
with open('bot/helper/mirror_utils/download_utils/mega_download.py', 'r') as f:
    c = f.read()
old = '''    async def _poll_loop(self) -> None:
        prev_bytes = 0
        while not self.is_cancelled:
            await asyncio.sleep(self._POLL_INTERVAL)
            if self._watch_path is None:
                continue
            current = self._dir_size(self._watch_path)
            self.speed = max(0, current - prev_bytes) / self._POLL_INTERVAL
            self.downloaded_bytes = current
            prev_bytes = current'''
new = '''    @staticmethod
    def _get_net_rx():
        try:
            import psutil
            c = psutil.net_io_counters()
            return c.bytes_recv if c else 0
        except Exception:
            total = 0
            try:
                with open(\"/proc/net/dev\") as f:
                    for line in f:
                        if \":\" not in line:
                            continue
                        iface, data = line.split(\":\", 1)
                        if iface.strip() == \"lo\":
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
            disk = self._dir_size(self._watch_path) if self._watch_path else 0
            net_now   = self._get_net_rx()
            net_delta = max(0, net_now - prev_net)
            prev_net  = net_now
            if disk > 0:
                delta = max(0, disk - prev_disk)
                self.speed = delta / self._POLL_INTERVAL
                self.downloaded_bytes = disk
                prev_disk = disk
            elif net_delta > 0:
                self.speed = net_delta / self._POLL_INTERVAL
                self.downloaded_bytes += net_delta'''
if old in c:
    open('bot/helper/mirror_utils/download_utils/mega_download.py','w').write(c.replace(old,new))
    print('mega_download.py patched')
else:
    print('mega_download.py already patched')
"

# Fix mega_download_status.py
python3 -c "
content = '''#!/usr/bin/env python3
from time import time
from bot.helper.ext_utils.bot_utils import EngineStatus, get_readable_file_size, MirrorStatus, get_readable_time


class MegaDownloadStatus:

    def __init__(self, name, size, gid, obj, message, upload_details):
        self.__obj = obj
        self.__name = name
        self.__size = size
        self.__gid = gid
        self.message = message
        self.upload_details = upload_details
        self.__start_time = time()

    def _effective_size(self):
        if self.__size and self.__size > 0:
            return self.__size
        return getattr(self.__obj, \"total_bytes\", 0)

    def _downloaded(self):
        try:
            return self.__obj.downloaded_bytes or 0
        except Exception:
            return 0

    def _speed(self):
        try:
            return self.__obj.speed or 0.0
        except Exception:
            return 0.0

    def _is_buffering(self):
        return self._downloaded() == 0

    def name(self):
        return self.__name

    def progress_raw(self):
        downloaded = self._downloaded()
        size = self._effective_size()
        if downloaded > 0 and size > 0:
            return round(downloaded / size * 100, 2)
        if downloaded > 0:
            return min(round((downloaded / (downloaded + 10 * 1024 * 1024)) * 99, 2), 99.0)
        elapsed = time() - self.__start_time
        return round((elapsed * (99 / 60)) % 99, 2)

    def progress(self):
        return f\"{self.progress_raw()}%\"

    def status(self):
        return MirrorStatus.STATUS_DOWNLOADING

    def processed_bytes(self):
        d = self._downloaded()
        return get_readable_file_size(d) if d > 0 else \"0B\"

    def eta(self):
        downloaded = self._downloaded()
        size = self._effective_size()
        spd = self._speed()
        if downloaded > 0 and size > 0 and spd > 0:
            return get_readable_time((size - downloaded) / spd)
        if self._is_buffering():
            return \"Connecting...\"
        if spd > 0 and size > 0:
            return get_readable_time(size / spd)
        return \"-\"

    def size(self):
        s = self._effective_size()
        return get_readable_file_size(s) if s > 0 else \"N/A\"

    def speed(self):
        spd = self._speed()
        if spd > 0:
            return f\"{get_readable_file_size(spd)}/s\"
        if self._is_buffering():
            return \"Connecting...\"
        return \"0.0B/s\"

    def gid(self):
        return self.__gid

    def download(self):
        return self.__obj

    def eng(self):
        return EngineStatus().STATUS_MEGA
'''
open('bot/helper/mirror_utils/status_utils/mega_download_status.py','w').write(content)
print('mega_download_status.py patched')
"

python3 -c "import asyncio; asyncio.set_event_loop(asyncio.new_event_loop()); import runpy; runpy.run_module('bot', run_name='__main__')"

