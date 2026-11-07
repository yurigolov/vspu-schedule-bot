"""Точка входа: python main.py"""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeChat
from dotenv import load_dotenv

import admin
import handlers
import schedules
from i18n import DEFAULT_LANG, LANGS
from storage import Storage
from vspu import VspuClient

async def set_commands(bot: Bot, storage: Storage, admins: frozenset[int]) -> None:
    """Меню команд. В меню только /teacher, /group и /language: расписание открывается
    кнопками под полем ввода (/today, /now и остальные при этом тоже работают).
    По умолчанию — по-русски, для Telegram на английском и португальском — на них.
    Кто выбрал язык в боте, получает меню на нём (это задаётся при выборе)."""
    await bot.set_my_commands(handlers.bot_commands(DEFAULT_LANG))
    for lang in LANGS:
        if lang != DEFAULT_LANG:
            await bot.set_my_commands(handlers.bot_commands(lang), language_code=lang)
    # Админам — те же команды плюс свои. Остальные админских команд не видят.
    for admin_id in admins:
        try:
            await bot.set_my_commands(
                handlers.bot_commands(storage.get_lang(admin_id) or DEFAULT_LANG, admin=True),
                scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramAPIError as e:
            # обычно значит, что админ ещё ни разу не писал боту
            logging.warning("Не удалось задать меню админа %s: %s", admin_id, e)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("Не задан BOT_TOKEN: создай файл .env по образцу .env.example")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    admins = admin.parse_admin_ids(os.getenv("ADMIN_IDS"))
    if admins:
        logging.info("Админы: %s", ", ".join(map(str, sorted(admins))))
    else:
        logging.warning("ADMIN_IDS не задан — /stats и /broadcast никому не доступны")

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    client = VspuClient()
    storage = Storage(os.getenv("DB_PATH", "data/bot.db"))

    dp = Dispatcher()
    # client, storage и admins попадают в обработчики как одноимённые аргументы
    dp["client"] = client
    dp["storage"] = storage
    dp["admins"] = admins
    dp.message.outer_middleware(admin.ActivityMiddleware(storage))
    dp.callback_query.outer_middleware(admin.ActivityMiddleware(storage))
    # админский роутер первым: в основном в конце стоит обработчик устаревших кнопок
    dp.include_router(admin.router)
    dp.include_router(handlers.router)

    await set_commands(bot, storage, admins)
    # копии расписаний групп пользователей — на случай, если сайт ляжет
    refresher = asyncio.create_task(schedules.refresh_loop(client, storage))
    # индекс «предмет → преподаватель» для /subject — раз в сутки по всем группам
    indexer = asyncio.create_task(schedules.subject_index_loop(client, storage))
    try:
        await dp.start_polling(bot)
    finally:
        refresher.cancel()
        indexer.cancel()
        await client.close()
        storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
