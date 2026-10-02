#!/usr/bin/env python3
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
        return getattr(self.__obj, 'total_bytes', 0)

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
        """True only when nothing has been downloaded yet (no disk, no net bytes)."""
        return self._downloaded() == 0

    def name(self):
        return self.__name

    def progress_raw(self):
        downloaded = self._downloaded()
        size = self._effective_size()

        if downloaded > 0 and size > 0:
            return round(downloaded / size * 100, 2)

        if downloaded > 0:
            # size unknown — cap at 99
            return min(round((downloaded / (downloaded + 10 * 1024 * 1024)) * 99, 2), 99.0)

        # Nothing yet — animate on 60s cycle
        elapsed = time() - self.__start_time
        return round((elapsed * (99 / 60)) % 99, 2)

    def progress(self):
        return f"{self.progress_raw()}%"

    def status(self):
        return MirrorStatus.STATUS_DOWNLOADING

    def processed_bytes(self):
        d = self._downloaded()
        return get_readable_file_size(d) if d > 0 else '0B'

    def eta(self):
        downloaded = self._downloaded()
        size = self._effective_size()
        spd = self._speed()

        if downloaded > 0 and size > 0 and spd > 0:
            remaining = size - downloaded
            return get_readable_time(remaining / spd)

        if self._is_buffering():
            return 'Connecting…'

        if spd > 0 and size > 0:
            return get_readable_time(size / spd)

        return '-'

    def size(self):
        s = self._effective_size()
        return get_readable_file_size(s) if s > 0 else 'N/A'

    def speed(self):
        spd = self._speed()
        if spd > 0:
            return f'{get_readable_file_size(spd)}/s'
        if self._is_buffering():
            return 'Connecting…'
        return '0.0B/s'

    def gid(self):
        return self.__gid

    def download(self):
        return self.__obj

    def eng(self):
        return EngineStatus().STATUS_MEGA
