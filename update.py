from logging import FileHandler, StreamHandler, INFO, basicConfig, error as log_error, info as log_info
from os import path as ospath, environ, remove
from subprocess import run as srun, call as scall
from pkg_resources import working_set
from requests import get as rget
from dotenv import load_dotenv, dotenv_values
from pymongo import MongoClient

if ospath.exists('log.txt'):
    with open('log.txt', 'r+') as f:
        f.truncate(0)

if ospath.exists('rlog.txt'):
    remove('rlog.txt')

basicConfig(format="[%(asctime)s] [%(levelname)s] - %(message)s",
            datefmt="%d-%b-%y %I:%M:%S %p",
            handlers=[FileHandler('log.txt'), StreamHandler()],
            level=INFO)

load_dotenv('config.env', override=True)

try:
    if bool(environ.get('_____REMOVE_THIS_LINE_____')):
        log_error('The README.md file there to be read! Exiting now!')
        exit()
except:
    pass

BOT_TOKEN = environ.get('BOT_TOKEN', '')
if len(BOT_TOKEN) == 0:
    log_error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

bot_id = BOT_TOKEN.split(':', 1)[0]

DATABASE_URL = environ.get('DATABASE_URL', '')
if len(DATABASE_URL) == 0:
    DATABASE_URL = None

if DATABASE_URL is not None:
    conn = MongoClient(DATABASE_URL)
    db = conn.thiruml
    old_config = db.settings.deployConfig.find_one({'_id': bot_id})
    config_dict = db.settings.config.find_one({'_id': bot_id})
    if old_config is not None:
        del old_config['_id']
    if (old_config is not None and old_config == dict(dotenv_values('config.env')) or old_config is None) \
            and config_dict is not None:
        environ['UPSTREAM_REPO'] = config_dict.get('UPSTREAM_REPO', '')
        environ['UPSTREAM_BRANCH'] = config_dict.get('UPSTREAM_BRANCH', '')
        environ['UPGRADE_PACKAGES'] = config_dict.get('UPDATE_PACKAGES', 'False')
    conn.close()

UPGRADE_PACKAGES = environ.get('UPGRADE_PACKAGES', 'False')
if UPGRADE_PACKAGES.lower() == 'true':
    packages = [dist.project_name for dist in working_set]
    scall("pip install " + ' '.join(packages), shell=True)

UPSTREAM_REPO = environ.get('UPSTREAM_REPO', '')
UPSTREAM_BRANCH = environ.get('UPSTREAM_BRANCH', '')

if len(UPSTREAM_REPO) == 0:
    UPSTREAM_REPO = "https://github.com/ThiruXD/Thiru-ML"

if len(UPSTREAM_BRANCH) == 0:
    UPSTREAM_BRANCH = 'hk_thiruml'

if UPSTREAM_REPO is not None:

    if ospath.exists('.git'):
        srun(["rm", "-rf", ".git"])

    update = srun([f"git init -q \
                     && git config --global user.email doc.adhikari@gmail.com \
                     && git config --global user.name weebzone \
                     && git add . \
                     && git commit -sm update -q \
                     && git remote add origin {UPSTREAM_REPO} \
                     && git fetch origin -q \
                     && git reset --hard origin/{UPSTREAM_BRANCH} -q"], shell=True)

    repo = UPSTREAM_REPO.split('/')
    UPSTREAM_REPO = f"https://github.com/{repo[-2]}/{repo[-1]}"
    if update.returncode == 0:
        log_info('Successfully updated with latest commits !!')
    else:
        log_error('Something went Wrong ! Retry or Ask Support !')
    log_info(f'UPSTREAM_REPO: {UPSTREAM_REPO} | UPSTREAM_BRANCH: {UPSTREAM_BRANCH}')

# Patch pyrofork MIN_CHANNEL_ID to support large channel IDs (must run after git pull)
try:
    pyro_utils = '/usr/local/lib/python3.10/dist-packages/pyrogram/utils.py'
    with open(pyro_utils, 'r') as pf:
        pcontent = pf.read()
    if 'MIN_CHANNEL_ID = -1002147483647' in pcontent:
        pcontent = pcontent.replace('MIN_CHANNEL_ID = -1002147483647', 'MIN_CHANNEL_ID = -1009999999999999')
        with open(pyro_utils, 'w') as pf:
            pf.write(pcontent)
        log_info('Pyrofork MIN_CHANNEL_ID patched successfully')
except Exception as e:
    log_error(f'Failed to patch pyrofork: {e}')

# Merge setting.env into config.env if it exists (for storing secrets in private repo)
if ospath.exists('setting.env'):
    with open('setting.env', 'r') as sf:
        secret_lines = sf.readlines()
    existing = {}
    if ospath.exists('config.env'):
        with open('config.env', 'r') as cf:
            for line in cf:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    existing[k.strip()] = v.strip()
    for line in secret_lines:
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            existing[k.strip()] = v.strip()
    with open('config.env', 'w') as cf:
        for k, v in existing.items():
            cf.write(f'{k} = {v}\n')
    log_info('setting.env merged into config.env')
    load_dotenv('config.env', override=True)
