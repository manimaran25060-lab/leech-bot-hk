
from bot import LOGGER, config_dict, user_data
from bot.helper.ext_utils.bot_utils import sync_to_async

def _clean_name_sync(name: str) -> str:
    """Synchronous URL/username cleaner for filenames — strips spam before prefix is added."""
    import re as _re
    # Split off extension so we don't mangle it
    if '.' in name:
        base, ext = name.rsplit('.', 1)
    else:
        base, ext = name, ''

    # Replace underscores with spaces for easier pattern matching
    base = base.replace('_', ' ')

    spam_patterns = [
        r'^\[[\w\s\-\.]+\]',              # [GroupTag] at start e.g. [AWI], [SubsPlease]
        r'\[[0-9A-Fa-f]{6,8}\]',          # hex hash in brackets e.g. [FD3D82E3]
        r'@[\w]+',                        # @username
        r'https?://\S+',                  # https://...
        r'www\.\S+',                      # www....
        r't\.me/\S+',                     # t.me/...
        r'\b[\w-]+\.(com|net|org|in|art|cymru|eu|me|io|co|tv|app|site|online|store|shop|club|info|biz|live|fun|xyz|icu|buzz|pw|cc|se|pro|top|vip|wiki|news|blog|tech|dev|ai)\b\S*',
        r'1tamilmv\s*\.\s*\w+',
        r'tamiltwist\s*\.\s*\w+',
        r'1tamilblasters\s*\.\s*\w+',
        r'moviesda\s*\.\s*\w+',
        r'cooltamil\s*\.\s*\w+',
        r'isaimini\s*\.\s*\w+',
        r'tamilyogi\s*\.\s*\w+',
        r'movierulz\s*\.\s*\w+',
    ]
    for pat in spam_patterns:
        base = _re.sub(pat, '', base, flags=_re.IGNORECASE)

    # Collapse multiple dashes/separators left behind
    base = _re.sub(r'(\s*[-]\s*){2,}', ' - ', base)
    base = _re.sub(r'^\s*[-]\s*', '', base)
    base = _re.sub(r'\s*[-]\s*$', '', base)
    base = _re.sub(r'\s{2,}', ' ', base).strip()

    if not base:
        base = name.rsplit('.', 1)[0] if '.' in name else name

    return f"{base}.{ext}" if ext else base


async def format_filename(name: str, user_id: int, isMirror: bool = False):
    """Apply user-configured prefix/suffix to filename and build caption."""
    user_dict = user_data.get(user_id, {})
    if isMirror:
        prefix  = user_dict.get('mprefix',  '') or config_dict.get('MIRROR_FILENAME_PREFIX', '')
        suffix  = user_dict.get('msuffix',  '') or config_dict.get('MIRROR_FILENAME_SUFFIX', '')
        caption = ''
    else:
        prefix  = user_dict.get('lprefix',  '') or config_dict.get('LEECH_FILENAME_PREFIX', '')
        suffix  = user_dict.get('lsuffix',  '') or config_dict.get('LEECH_FILENAME_SUFFIX', '')
        caption = user_dict.get('lcaption', '') or config_dict.get('LEECH_FILENAME_CAPTION', '')

    # Clean URLs/@usernames from the raw filename BEFORE adding prefix/suffix
    name = _clean_name_sync(name)

    if prefix:
        name = f"{prefix} {name}"
    if suffix:
        # insert suffix before extension
        if '.' in name:
            base, ext = name.rsplit('.', 1)
            name = f"{base} {suffix}.{ext}"
        else:
            name = f"{name} {suffix}"
    # Build caption — replace {filename} placeholder if present, else use name as caption
    if caption:
        base_name = name.rsplit('.', 1)[0] if '.' in name else name
        cap_mono = caption.replace('{filename}', base_name).replace('{name}', base_name)
    else:
        # Default: use filename (without extension) as caption
        cap_mono = name.rsplit('.', 1)[0] if '.' in name else name
    # Append invisible zero-width space marker — bot detects this to skip re-leeching its own files
    cap_mono = cap_mono + '\u200b'
    return name, cap_mono


async def get_document_type(path: str):
    """Return (is_video, is_audio, is_image) for the given file path."""
    from bot.helper.ext_utils.fs_utils import get_mime_type
    try:
        mime = await sync_to_async(get_mime_type, path)
        is_video = mime.startswith('video/')
        is_audio = mime.startswith('audio/')
        is_image = mime.startswith('image/')
        return is_video, is_audio, is_image
    except Exception:
        return False, False, False


async def split_file(path: str, size: int, file_: str, dirpath: str,
                     split_size: int, listener):
    """Split a file into chunks using ffmpeg (video) or plain binary split."""
    from asyncio import create_subprocess_exec
    from asyncio.subprocess import PIPE
    from bot.helper.ext_utils.fs_utils import get_mime_type
    try:
        mime = await sync_to_async(get_mime_type, path)
    except Exception:
        mime = 'application/octet-stream'

    if mime.startswith('video/'):
        # Use ffmpeg to split cleanly on keyframes
        base = file_.rsplit('.', 1)[0] if '.' in file_ else file_
        ext  = file_.rsplit('.', 1)[1] if '.' in file_ else 'mkv'
        parts = -(-size // split_size)   # ceiling division
        duration_cmd = ['ffprobe', '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path]
        proc = await create_subprocess_exec(*duration_cmd, stdout=PIPE, stderr=PIPE)
        out, _ = await proc.communicate()
        try:
            total_dur = float(out.decode().strip())
        except Exception:
            total_dur = 0
        if total_dur and parts > 1:
            # Calculate segment duration based on split_size vs total file size
            # so each part stays within the size limit (not equal-time division)
            seg = (split_size / size) * total_dur
            for i in range(parts):
                out_path = f"{dirpath}/{base}.part{i+1:03d}.{ext}"
                start = i * seg
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                       '-ss', str(start), '-i', path,
                       '-t', str(seg), '-map', '0', '-c', 'copy',
                       '-avoid_negative_ts', 'make_zero',
                       '-fs', str(split_size),
                       out_path]
                sp = await create_subprocess_exec(*cmd, stderr=PIPE)
                _, err = await sp.communicate()
                if sp.returncode != 0:
                    LOGGER.error(f"ffmpeg split error: {err.decode()}")
                    return "errored"
                if listener.suproc == 'cancelled':
                    return False
            return True

    # Fallback: plain binary split
    base = file_.rsplit('.', 1)[0] if '.' in file_ else file_
    ext  = '.' + file_.rsplit('.', 1)[1] if '.' in file_ else ''
    chunk = 0
    async with aiopen(path, 'rb') as f:
        while True:
            data = await f.read(split_size)
            if not data:
                break
            chunk += 1
            out_path = f"{dirpath}/{base}.part{chunk:03d}{ext}"
            async with aiopen(out_path, 'wb') as out_f:
                await out_f.write(data)
            if listener.suproc == 'cancelled':
                return False
    return True if chunk > 1 else "errored"


async def get_media_info(path: str):
    """Return (duration_secs, artist, title) for a media file using ffprobe."""
    from bot.helper.ext_utils.bot_utils import cmd_exec
    try:
        result = await cmd_exec([
            'ffprobe', '-hide_banner', '-loglevel', 'error',
            '-show_entries', 'format_tags=artist,title:format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=0', path
        ])
        stdout = result[0]
        duration, artist, title = 0, '', ''
        for line in stdout.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                k = k.strip().lower()
                v = v.strip()
                if k == 'duration':
                    try:
                        duration = int(float(v))
                    except Exception:
                        duration = 0
                elif k == 'tag:artist':
                    artist = v
                elif k == 'tag:title':
                    title = v
        return duration, artist, title
    except Exception:
        return 0, '', ''


async def take_ss(video_path: str, duration):
    """Take a screenshot from a video at the middle (or at 0 if duration unknown)."""
    from aiofiles.os import path as aiopath
    from os import path as ospath
    from bot.helper.ext_utils.bot_utils import cmd_exec
    try:
        if not duration:
            result = await cmd_exec([
                'ffprobe', '-hide_banner', '-loglevel', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', video_path
            ])
            try:
                duration = int(float(result[0].strip()))
            except Exception:
                duration = 0
        ss_time = duration // 2
        thumb_path = f"{video_path}.jpg"
        await cmd_exec([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', str(ss_time), '-i', video_path,
            '-vframes', '1', '-q:v', '2', thumb_path
        ])
        if await aiopath.isfile(thumb_path):
            return thumb_path
    except Exception as e:
        LOGGER.error(f"take_ss error: {e}")
    return None


async def get_sample_video(video_path: str, duration, sample_duration: int):
    """Cut a short sample clip from the middle of a video via stream copy (no re-encode)."""
    from aiofiles.os import path as aiopath
    from bot.helper.ext_utils.bot_utils import cmd_exec
    try:
        if not duration:
            result = await cmd_exec([
                'ffprobe', '-hide_banner', '-loglevel', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', video_path
            ])
            try:
                duration = int(float(result[0].strip()))
            except Exception:
                duration = 0
        # Skip sampling if the source is too short to meaningfully sample
        if not duration or duration <= sample_duration * 2:
            return None
        start_time = (duration - sample_duration) // 2
        sample_path = f"{video_path}.sample.mp4"
        await cmd_exec([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', str(start_time), '-i', video_path,
            '-t', str(sample_duration), '-c', 'copy', sample_path
        ])
        if await aiopath.isfile(sample_path):
            return sample_path
    except Exception as e:
        LOGGER.error(f"get_sample_video error: {e}")
    return None


async def get_audio_thumb(audio_path: str):
    """Extract embedded thumbnail from an audio file."""
    from aiofiles.os import path as aiopath
    from bot.helper.ext_utils.bot_utils import cmd_exec
    try:
        thumb_path = f"{audio_path}.jpg"
        await cmd_exec([
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-i', audio_path, '-an', '-vcodec', 'copy', thumb_path
        ])
        if await aiopath.isfile(thumb_path):
            return thumb_path
    except Exception as e:
        LOGGER.error(f"get_audio_thumb error: {e}")
    return None


async def get_ss(video_path: str, num_ss: int):
    """Take multiple screenshots and upload to Telegraph, return URL."""
    from aiofiles.os import path as aiopath, makedirs, remove as aioremove
    from os import path as ospath
    from bot.helper.ext_utils.bot_utils import cmd_exec
    from bot.helper.ext_utils.telegraph_helper import telegraph
    try:
        ss_dir = f"{video_path}_ss"
        await makedirs(ss_dir, exist_ok=True)
        result = await cmd_exec([
            'ffprobe', '-hide_banner', '-loglevel', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', video_path
        ])
        try:
            duration = int(float(result[0].strip()))
        except Exception:
            duration = 60
        num_ss = max(1, min(int(num_ss), 10))
        interval = duration // (num_ss + 1)
        imgs = []
        for i in range(1, num_ss + 1):
            out = ospath.join(ss_dir, f"ss{i:02d}.jpg")
            await cmd_exec([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-ss', str(i * interval), '-i', video_path,
                '-vframes', '1', '-q:v', '2', out
            ])
            if await aiopath.isfile(out):
                imgs.append(out)
        if not imgs:
            return ''
        content = ''.join(f'<img src="{img}">' for img in imgs)
        page = await telegraph.create_page(title='Screenshots', content=content)
        for img in imgs:
            try:
                await aioremove(img)
            except Exception:
                pass
        return f"https://graph.org/{page['path']}"
    except Exception as e:
        LOGGER.error(f"get_ss error: {e}")
        return ''


async def get_mediainfo_link(file_path: str):
    """Generate a Telegraph mediainfo page for the file, return URL."""
    from bot.helper.ext_utils.bot_utils import cmd_exec
    from bot.helper.ext_utils.telegraph_helper import telegraph
    from os import path as ospath
    from shlex import split as ssplit
    try:
        stdout, _, _ = await cmd_exec(ssplit(f'mediainfo "{file_path}"'))
        if not stdout:
            return ''
        def parseinfo(out):
            tc = ''
            trigger = False
            section_dict = {'General': '🗒', 'Video': '🎞', 'Audio': '🔊', 'Text': '🔠', 'Menu': '🗃'}
            for line in out.split('\n'):
                for section, emoji in section_dict.items():
                    if line.startswith(section):
                        trigger = True
                        if not line.startswith('General'):
                            tc += '</pre><br>'
                        tc += f"<h4>{emoji} {line.replace('Text', 'Subtitle')}</h4>"
                        break
                if trigger:
                    tc += '<br><pre>'
                    trigger = False
                else:
                    tc += line + '\n'
            tc += '</pre><br>'
            return tc
        tc = f"<h4>📌 {ospath.basename(file_path)}</h4><br><br>"
        tc += parseinfo(stdout)
        page = await telegraph.create_page(title='MediaInfo', content=tc)
        return f"https://graph.org/{page['path']}"
    except Exception as e:
        LOGGER.error(f"get_mediainfo_link error: {e}")
        return ''
