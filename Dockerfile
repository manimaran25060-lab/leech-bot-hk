FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

WORKDIR /usr/src/app
RUN chmod 777 /usr/src/app

RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-dev \
    git curl wget \
    aria2 ffmpeg qbittorrent-nox \
    libmagic1 libmagic-dev \
    p7zip-full p7zip-rar \
    unzip zip xz-utils pv jq mediainfo \
    gcc g++ \
    libssl3 libcrypto++8 libsodium23 \
    zlib1g libsqlite3-0 libc-ares2 \
    libcurl4 libreadline8 libuv1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN curl https://rclone.org/install.sh | bash

RUN ln -sf $(which aria2c) /usr/local/bin/zetra && \
    ln -sf $(which qbittorrent-nox) /usr/local/bin/xon-bit && \
    ln -sf $(which ffmpeg) /usr/local/bin/ggrof && \
    ln -sf $(which rclone) /usr/local/bin/cross-suck

RUN pip3 install --upgrade setuptools pip
RUN pip3 install lk21
COPY patch_lk21.py .
RUN python3 patch_lk21.py
RUN pip3 install --use-pep517 pymediainfo pyaes

COPY . .
RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["bash", "start.sh"]
