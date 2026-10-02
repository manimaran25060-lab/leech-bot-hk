#!/usr/bin/env python3
from os import environ
from pyrogram.handlers import MessageHandler
from pyrogram.filters import command

from bot import config_dict, DATABASE_URL, GLOBAL_CAPTION_BLACKLIST, bot, LOGGER
from bot.helper.telegram_helper.message_utils import sendMessage
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.ext_utils.db_handler import DbManger


async def _persist_caption_blacklist():
    value = '|'.join(GLOBAL_CAPTION_BLACKLIST)
    environ['CAPTION_BLACKLIST'] = value
    config_dict['CAPTION_BLACKLIST'] = value
    if DATABASE_URL:
        await DbManger().update_config({'CAPTION_BLACKLIST': value})


async def addCapWord(_, message):
    args = message.text.split(None, 1)
    if len(args) < 2:
        await sendMessage(message, f"Give word(s) to blacklist. Use | to add multiple at once.\nExample: <code>/{BotCommands.AddCapWordCommand[0]} join|download</code>")
        return
    words = [w.strip() for w in args[1].split('|') if w.strip()]
    if not words:
        await sendMessage(message, "No valid word given!")
        return
    added, existing = [], []
    for w in words:
        if w.lower() in [x.lower() for x in GLOBAL_CAPTION_BLACKLIST]:
            existing.append(w)
        else:
            GLOBAL_CAPTION_BLACKLIST.append(w)
            added.append(w)
    if added:
        await _persist_caption_blacklist()
    msg = ""
    if added:
        msg += f"Added to blacklist: {', '.join(added)}\n"
    if existing:
        msg += f"Already in blacklist: {', '.join(existing)}"
    await sendMessage(message, msg or "Nothing added.")


async def rmCapWord(_, message):
    args = message.text.split(None, 1)
    if len(args) < 2:
        await sendMessage(message, f"Give word(s) to remove. Use | to remove multiple at once.\nExample: <code>/{BotCommands.RmCapWordCommand[0]} join|download</code>")
        return
    words = [w.strip().lower() for w in args[1].split('|') if w.strip()]
    if not words:
        await sendMessage(message, "No valid word given!")
        return
    removed, not_found = [], []
    for w in words:
        match = next((x for x in GLOBAL_CAPTION_BLACKLIST if x.lower() == w), None)
        if match:
            GLOBAL_CAPTION_BLACKLIST.remove(match)
            removed.append(match)
        else:
            not_found.append(w)
    if removed:
        await _persist_caption_blacklist()
    msg = ""
    if removed:
        msg += f"Removed from blacklist: {', '.join(removed)}\n"
    if not_found:
        msg += f"Not found in blacklist: {', '.join(not_found)}"
    await sendMessage(message, msg or "Nothing removed.")


async def listCapWords(_, message):
    if not GLOBAL_CAPTION_BLACKLIST:
        await sendMessage(message, "Caption blacklist is empty.")
        return
    msg = "<b>Caption Blacklist Words:</b>\n\n"
    msg += '\n'.join(f"{i}. <code>{w}</code>" for i, w in enumerate(GLOBAL_CAPTION_BLACKLIST, 1))
    await sendMessage(message, msg)


bot.add_handler(MessageHandler(addCapWord, filters=command(
    BotCommands.AddCapWordCommand) & CustomFilters.sudo))
bot.add_handler(MessageHandler(rmCapWord, filters=command(
    BotCommands.RmCapWordCommand) & CustomFilters.sudo))
bot.add_handler(MessageHandler(listCapWords, filters=command(
    BotCommands.ListCapWordCommand) & CustomFilters.sudo))
