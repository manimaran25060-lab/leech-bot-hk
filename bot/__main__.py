from time import time, monotonic
from datetime import datetime
from sys import executable
from os import execl as osexecl
import asyncio
from asyncio import create_subprocess_exec, gather, run as asyrun, sleep
from uuid import uuid4
from base64 import b64decode
from importlib import import_module, reload

from requests import get as rget
from pytz import timezone
from bs4 import BeautifulSoup
from signal import signal, SIGINT
from aiofiles.os import path as aiopath, remove as aioremove
from aiofiles import open as aiopen
from pyrogram import idle
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.filters import command, private, regex
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot import bot, user, bot_name, config_dict, user_data, botStartTime, LOGGER, Interval, DATABASE_URL, QbInterval, INCOMPLETE_TASK_NOTIFIER, scheduler, bot_cache
from bot.version import get_version
from .helper.ext_utils.fs_utils import start_cleanup, clean_all, exit_clean_up
from .helper.ext_utils.bot_utils import get_readable_time, cmd_exec, sync_to_async, new_task, set_commands, update_user_ldata, get_stats
from .helper.ext_utils.db_handler import DbManger
from .helper.telegram_helper.bot_commands import BotCommands
from .helper.telegram_helper.message_utils import sendMessage, editMessage, editReplyMarkup, sendFile, deleteMessage, delete_all_messages
from .helper.telegram_helper.filters import CustomFilters
from .helper.telegram_helper.button_build import ButtonMaker
from .helper.listeners.aria2_listener import start_aria2_listener
from .helper.themes import BotTheme
from .modules import authorize, cancel_mirror, mirror_leech, status, torrent_select, ytdlp, \
                     shell, eval, users_settings, bot_settings, save_msg, images, mediainfo, \
                     broadcast, category_select, autoleecher, animesalt, speedtest, caption_blacklist

async def stats(client, message):
    msg, btns = await get_stats(message)
    await sendMessage(message, msg, btns, photo='IMAGES')

@new_task
async def start(client, message):
    buttons = ButtonMaker()
    buttons.ubutton(BotTheme('ST_BN1_NAME'), BotTheme('ST_BN1_URL'))
    buttons.ubutton(BotTheme('ST_BN2_NAME'), BotTheme('ST_BN2_URL'))
    reply_markup = buttons.build_menu(2)
    if len(message.command) > 1 and message.command[1] == "thiruml":
        await deleteMessage(message)
    elif len(message.command) > 1 and config_dict['TOKEN_TIMEOUT']:
        userid = message.from_user.id
        encrypted_url = message.command[1]
        input_token, pre_uid = (b64decode(encrypted_url.encode()).decode()).split('&&')
        if int(pre_uid) != userid:
            return await sendMessage(message, BotTheme('OWN_TOKEN_GENERATE'))
        data = user_data.get(userid, {})
        if 'token' not in data or data['token'] != input_token:
            return await sendMessage(message, BotTheme('USED_TOKEN'))
        elif config_dict['LOGIN_PASS'] is not None and data['token'] == config_dict['LOGIN_PASS']:
            return await sendMessage(message, BotTheme('LOGGED_PASSWORD'))
        buttons.ibutton(BotTheme('ACTIVATE_BUTTON'), f'pass {input_token}', 'header')
        reply_markup = buttons.build_menu(2)
        msg = BotTheme('TOKEN_MSG', token=input_token, validity=get_readable_time(int(config_dict["TOKEN_TIMEOUT"])))
        return await sendMessage(message, msg, reply_markup)
    elif await CustomFilters.authorized(client, message):
        start_string = BotTheme('ST_MSG', help_command=f"/{BotCommands.HelpCommand}")
        await sendMessage(message, start_string, reply_markup, photo='IMAGES')
    elif config_dict['BOT_PM']:
        await sendMessage(message, BotTheme('ST_BOTPM'), reply_markup, photo='IMAGES')
    else:
        await sendMessage(message, BotTheme('ST_UNAUTH'), reply_markup, photo='IMAGES')
    await DbManger().update_pm_users(message.from_user.id)


async def token_callback(_, query):
    user_id = query.from_user.id
    input_token = query.data.split()[1]
    data = user_data.get(user_id, {})
    if 'token' not in data or data['token'] != input_token:
        return await query.answer('Already Used, Generate New One', show_alert=True)
    update_user_ldata(user_id, 'token', str(uuid4()))
    update_user_ldata(user_id, 'time', time())
    await query.answer('Activated Temporary Token!', show_alert=True)
    kb = query.message.reply_markup.inline_keyboard[1:]
    kb.insert(0, [InlineKeyboardButton(BotTheme('ACTIVATED'), callback_data='pass activated')])
    await editReplyMarkup(query.message, InlineKeyboardMarkup(kb))


async def login(_, message):
    if config_dict['LOGIN_PASS'] is None:
        return
    elif len(message.command) > 1:
        user_id = message.from_user.id
        input_pass = message.command[1]
        if user_data.get(user_id, {}).get('token', '') == config_dict['LOGIN_PASS']:
            return await sendMessage(message, BotTheme('LOGGED_IN'))
        if input_pass != config_dict['LOGIN_PASS']:
            return await sendMessage(message, BotTheme('INVALID_PASS'))
        update_user_ldata(user_id, 'token', config_dict['LOGIN_PASS'])
        return await sendMessage(message, BotTheme('PASS_LOGGED'))
    else:
        await sendMessage(message, BotTheme('LOGIN_USED'))



async def restart(client, message):
    restart_message = await sendMessage(message, BotTheme('RESTARTING'))
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await delete_all_messages()
    for interval in [QbInterval, Interval]:
        if interval:
            interval[0].cancel()
    await sync_to_async(clean_all)
    proc1 = await create_subprocess_exec('pkill', '-9', '-f', f'gunicorn|{bot_cache["pkgs"][-1]}')
    proc2 = await create_subprocess_exec('python3', 'update.py')
    await gather(proc1.wait(), proc2.wait())
    async with aiopen(".restartmsg", "w") as f:
        await f.write(f"{restart_message.chat.id}\n{restart_message.id}\n")
    osexecl(executable, executable, "-m", "bot")


async def ping(_, message):
    start_time = monotonic()
    reply = await sendMessage(message, BotTheme('PING'))
    end_time = monotonic()
    await editMessage(reply, BotTheme('PING_VALUE', value=int((end_time - start_time) * 1000)))


async def log(_, message):
    buttons = ButtonMaker()
    buttons.ibutton(BotTheme('LOG_DISPLAY_BT'), f'thiruml {message.from_user.id} logdisplay')
    buttons.ibutton(BotTheme('WEB_PASTE_BT'), f'thiruml {message.from_user.id} webpaste')
    await sendFile(message, 'log.txt', buttons=buttons.build_menu(1))


async def search_images():
    if not (query_list := config_dict['IMG_SEARCH']):
        return
    try:
        total_pages = config_dict['IMG_PAGE']
        base_url = "https://www.wallpaperflare.com/search"
        for query in query_list:
            query = query.strip().replace(" ", "+")
            for page in range(1, total_pages + 1):
                url = f"{base_url}?wallpaper={query}&width=1280&height=720&page={page}"
                r = rget(url)
                soup = BeautifulSoup(r.text, "html.parser")
                images = soup.select('img[data-src^="https://c4.wallpaperflare.com/wallpaper"]')
                if len(images) == 0:
                    LOGGER.info("Maybe Site is Blocked on your Server, Add Images Manually !!")
                for img in images:
                    img_url = img['data-src']
                    if img_url not in config_dict['IMAGES']:
                        config_dict['IMAGES'].append(img_url)
        if len(config_dict['IMAGES']) != 0:
            config_dict['STATUS_LIMIT'] = 2
        if DATABASE_URL:
            await DbManger().update_config({'IMAGES': config_dict['IMAGES'], 'STATUS_LIMIT': config_dict['STATUS_LIMIT']})
    except Exception as e:
        LOGGER.error(f"An error occurred: {e}")


async def bot_help(client, message):
    buttons = ButtonMaker()
    user_id = message.from_user.id
    buttons.ibutton(BotTheme('BASIC_BT'), f'thiruml {user_id} guide basic')
    buttons.ibutton(BotTheme('USER_BT'), f'thiruml {user_id} guide users')
    buttons.ibutton(BotTheme('MICS_BT'), f'thiruml {user_id} guide miscs')
    buttons.ibutton(BotTheme('O_S_BT'), f'thiruml {user_id} guide admin')
    buttons.ibutton(BotTheme('CLOSE_BT'), f'thiruml {user_id} close')
    await sendMessage(message, BotTheme('HELP_HEADER'), buttons.build_menu(2))


async def restart_notification():
    now=datetime.now(timezone(config_dict['TIMEZONE']))
    if await aiopath.isfile(".restartmsg"):
        with open(".restartmsg") as f:
            chat_id, msg_id = map(int, f)
    else:
        chat_id, msg_id = 0, 0

    async def send_incompelete_task_message(cid, msg):
        try:
            if msg.startswith("⌬ <b><i>Restarted Successfully!</i></b>"):
                await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg, disable_web_page_preview=True)
                await aioremove(".restartmsg")
            else:
                await bot.send_message(chat_id=cid, text=msg, disable_web_page_preview=True, disable_notification=True)
        except Exception as e:
            LOGGER.error(e)

    if INCOMPLETE_TASK_NOTIFIER and DATABASE_URL:
        if notifier_dict := await DbManger().get_incomplete_tasks():
            for cid, data in notifier_dict.items():
                msg = BotTheme('RESTART_SUCCESS', time=now.strftime('%I:%M:%S %p'), date=now.strftime('%d/%m/%y'), timz=config_dict['TIMEZONE'], version=get_version()) if cid == chat_id else BotTheme('RESTARTED')
                msg += "\n\n⌬ <b><i>Incomplete Tasks!</i></b>"
                for tag, links in data.items():
                    msg += f"\n➲ <b>User:</b> {tag}\n┖ <b>Tasks:</b>"
                    for index, link in enumerate(links, start=1):
                        msg_link, source = next(iter(link.items()))
                        msg += f" {index}. <a href='{source}'>S</a> ->  <a href='{msg_link}'>L</a> |"
                        if len(msg.encode()) > 4000:
                            await send_incompelete_task_message(cid, msg)
                            msg = ''
                if msg:
                    await send_incompelete_task_message(cid, msg)

    if await aiopath.isfile(".restartmsg"):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=BotTheme('RESTART_SUCCESS', time=now.strftime('%I:%M:%S %p'), date=now.strftime('%d/%m/%y'), timz=config_dict['TIMEZONE'], version=get_version()))
        except Exception as e:
            LOGGER.error(e)
        await aioremove(".restartmsg")


async def log_check():
    if config_dict['LEECH_LOG_ID']:
        for chat_id in config_dict['LEECH_LOG_ID'].split():
            chat_id, *topic_id = chat_id.split(":")
            try:
                # Force cache with bot client
                try:
                    chat = await bot.get_chat(int(chat_id))
                except Exception:
                    try:
                        msg = await bot.send_message(int(chat_id), ".")
                        await msg.delete()
                        chat = await bot.get_chat(int(chat_id))
                    except Exception:
                        LOGGER.error(f"Not Connected Chat ID : {chat_id}, Make sure the Bot is Added!")
                        continue

                # Force cache with user client too (needed for >2GB uploads)
                if user:
                    try:
                        await user.get_chat(int(chat_id))
                    except Exception:
                        try:
                            msg = await user.send_message(int(chat_id), ".")
                            await msg.delete()
                        except Exception as e:
                            LOGGER.error(f"User client failed to cache Chat ID : {chat_id} - {e}")

                if chat.type == ChatType.CHANNEL:
                    if not (await chat.get_member(bot.me.id)).privileges.can_post_messages:
                        LOGGER.error(f"Not Connected Chat ID : {chat_id}, Make the Bot is Admin in Channel to Connect!")
                        continue
                    if user and not (await chat.get_member(user.me.id)).privileges.can_post_messages:
                        LOGGER.error(f"Not Connected Chat ID : {chat_id}, Make the User is Admin in Channel to Connect!")
                        continue
                elif chat.type == ChatType.SUPERGROUP:
                    if not (await chat.get_member(bot.me.id)).status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                        LOGGER.error(f"Not Connected Chat ID : {chat_id}, Make the Bot is Admin in Group to Connect!")
                        continue
                    if user and not (await chat.get_member(user.me.id)).status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR]:
                        LOGGER.error(f"Not Connected Chat ID : {chat_id}, Make the User is Admin in Group to Connect!")
                        continue
                LOGGER.info(f"Connected Chat ID : {chat_id}")
            except Exception as e:
                LOGGER.error(f"Not Connected Chat ID : {chat_id}, ERROR: {e}")
    

async def dump_check():
    for uid, udata in user_data.items():
        if dumps := udata.get('ldump', {}):
            for name, chat_id in dumps.items():
                # Cache with bot client
                try:
                    await bot.get_chat(int(chat_id))
                    LOGGER.info(f"Dump Channel Cached (bot): {name} ({chat_id})")
                except Exception:
                    try:
                        msg = await bot.send_message(int(chat_id), ".")
                        await msg.delete()
                        LOGGER.info(f"Dump Channel Force Cached (bot): {name} ({chat_id})")
                    except Exception as e:
                        LOGGER.error(f"Dump Channel Failed (bot): {name} ({chat_id}) - {e}")
                # Cache with user client too (needed for files > 2GB)
                if user:
                    try:
                        await user.get_chat(int(chat_id))
                        LOGGER.info(f"Dump Channel Cached (user): {name} ({chat_id})")
                    except Exception:
                        try:
                            msg = await user.send_message(int(chat_id), ".")
                            await msg.delete()
                            LOGGER.info(f"Dump Channel Force Cached (user): {name} ({chat_id})")
                        except Exception as e:
                            LOGGER.error(f"Dump Channel Failed (user): {name} ({chat_id}) - {e}")

async def cache_all_channels():
    """
    Force-cache ALL channels the bot uses — for both bot and user clients.
    Prevents 'Peer id invalid' errors after restarts.
    Covers: LEECH_LOG_ID, MIRROR_LOG_ID, LINKS_LOG_ID, AUTO_LEECH_GRP_ID,
            AUTO_LEECH_DUMP_ID, and all user dump channels.
    """
    clients = [('bot', bot)]
    if user:
        clients.append(('user', user))

    async def force_cache(client_name, client, chat_id, label):
        try:
            await client.get_chat(int(chat_id))
            LOGGER.info(f"✅ Cached [{client_name}] {label} ({chat_id})")
        except Exception:
            try:
                msg = await client.send_message(int(chat_id), ".")
                await msg.delete()
                LOGGER.info(f"✅ Force cached [{client_name}] {label} ({chat_id})")
            except Exception as e:
                LOGGER.error(f"❌ Failed to cache [{client_name}] {label} ({chat_id}): {e}")

    # Collect all channel IDs from config
    channel_ids = []

    if config_dict.get('LEECH_LOG_ID'):
        for cid in str(config_dict['LEECH_LOG_ID']).split():
            cid = cid.split(':')[0]
            if cid:
                channel_ids.append((cid, 'LEECH_LOG_ID'))

    if config_dict.get('MIRROR_LOG_ID'):
        for cid in str(config_dict['MIRROR_LOG_ID']).split():
            cid = cid.split(':')[0]
            if cid:
                channel_ids.append((cid, 'MIRROR_LOG_ID'))

    if config_dict.get('LINKS_LOG_ID'):
        channel_ids.append((str(config_dict['LINKS_LOG_ID']), 'LINKS_LOG_ID'))

    if config_dict.get('AUTO_LEECH_GRP_ID'):
        channel_ids.append((str(config_dict['AUTO_LEECH_GRP_ID']), 'AUTO_LEECH_GRP_ID'))

    if config_dict.get('AUTO_LEECH_DUMP_ID'):
        channel_ids.append((str(config_dict['AUTO_LEECH_DUMP_ID']), 'AUTO_LEECH_DUMP_ID'))

    # User dump channels
    for uid, udata in user_data.items():
        if dumps := udata.get('ldump', {}):
            for name, cid in dumps.items():
                channel_ids.append((str(cid), f'dump:{name}'))

    # Cache all channels with all clients
    for cid, label in channel_ids:
        for client_name, client in clients:
            await force_cache(client_name, client, cid, label)

    LOGGER.info("✅ Channel caching complete")


async def keep_alive_channels():
    """
    Ping all dump/log channels every 30 minutes to keep peers fresh
    in Pyrogram's session cache. Prevents the 'PeerIdInvalid' error
    that causes dump channel forwarding to silently stop after inactivity.
    This is the root cause of the 'works in morning, stops by afternoon' issue.
    """
    LOGGER.info("✅ Channel keep-alive task started (pings every 30 min)")
    while True:
        await sleep(1800)  # 30 minutes
        channel_ids = []

        # Collect all channels that need to stay alive
        if config_dict.get('LEECH_LOG_ID'):
            for cid in str(config_dict['LEECH_LOG_ID']).split():
                channel_ids.append(cid.split(':')[0])

        if config_dict.get('AUTO_LEECH_DUMP_ID'):
            channel_ids.append(str(config_dict['AUTO_LEECH_DUMP_ID']))

        if config_dict.get('AUTO_LEECH_GRP_ID'):
            channel_ids.append(str(config_dict['AUTO_LEECH_GRP_ID']))

        if config_dict.get('MIRROR_LOG_ID'):
            channel_ids.append(str(config_dict['MIRROR_LOG_ID']))

        if config_dict.get('LINKS_LOG_ID'):
            channel_ids.append(str(config_dict['LINKS_LOG_ID']))

        # User dump channels
        for uid, udata in user_data.items():
            if dumps := udata.get('ldump', {}):
                for name, cid in dumps.items():
                    channel_ids.append(str(cid))

        clients = [('bot', bot)]
        if user:
            clients.append(('user', user))

        for cid in set(channel_ids):  # deduplicate
            if not cid or cid == 'None':
                continue
            for client_name, client in clients:
                try:
                    await client.get_chat(int(cid))
                except Exception:
                    # Peer went stale — force refresh via send_message
                    try:
                        msg = await client.send_message(int(cid), "\u200b")
                        await msg.delete()
                        LOGGER.info(f"🔄 Keep-alive re-cached [{client_name}] {cid}")
                    except Exception as e:
                        LOGGER.warning(f"⚠️ Keep-alive failed [{client_name}] {cid}: {e}")


async def main():
    await gather(start_cleanup(), restart_notification(), search_images(), set_commands(bot), log_check(), dump_check())
    # Keep-alive pings all channels every 30 min to prevent PeerIdInvalid after inactivity
    asyncio.ensure_future(keep_alive_channels())
    await sync_to_async(start_aria2_listener, wait=False)
    
    bot.add_handler(MessageHandler(
        start, filters=command(BotCommands.StartCommand) & private))
    bot.add_handler(CallbackQueryHandler(
        token_callback, filters=regex(r'^pass')))
    bot.add_handler(MessageHandler(
        login, filters=command(BotCommands.LoginCommand) & private))
    bot.add_handler(MessageHandler(log, filters=command(
        BotCommands.LogCommand) & CustomFilters.sudo))
    bot.add_handler(MessageHandler(restart, filters=command(
        BotCommands.RestartCommand) & CustomFilters.sudo))
    bot.add_handler(MessageHandler(ping, filters=command(
        BotCommands.PingCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
    bot.add_handler(MessageHandler(bot_help, filters=command(
        BotCommands.HelpCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
    bot.add_handler(MessageHandler(stats, filters=command(
        BotCommands.StatsCommand) & CustomFilters.authorized & ~CustomFilters.blacklisted))
    LOGGER.info(f"Thiru-ML Bot [@{bot_name}] Started!")
    if user:
        LOGGER.info(f"Thiru-ML's User [@{user.me.username}] Ready!")
    signal(SIGINT, exit_clean_up)

async def stop_signals():
    if user:
        await gather(bot.stop(), user.stop())
    else:
        await bot.stop()


bot_run = bot.loop.run_until_complete
bot_run(main())
bot_run(idle())
bot_run(stop_signals())
