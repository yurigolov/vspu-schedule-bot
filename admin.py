"""Администрирование: /stats, /broadcast и учёт активности пользователей.

Админы задаются переменной ADMIN_IDS (Telegram ID через запятую). Для остальных
этих команд нет: бот на них молчит, а в лог пишет ID — так админ узнаёт свой.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    TelegramObject,
)

from storage import Storage

log = logging.getLogger(__name__)
router = Router()

SEND_INTERVAL = 0.05  # ~20 сообщений в секунду: лимит Telegram — около 30
PROGRESS_EVERY = 50  # как часто обновлять сообщение о ходе рассылки

BROADCAST_USAGE = (
    "Напиши текст рассылки после команды, можно в несколько строк:\n\n"
    "<code>/broadcast **Важно!** Завтра пары по расписанию субботы.\n"
    "Подробности — [на сайте](https://rsp-vspu.online/)</code>\n\n"
    "Поддерживается Markdown: <code>**жирный**</code>, <code>*курсив*</code>, "
    "<code>~~зачёркнутый~~</code>, <code>[ссылка](https://…)</code>, "
    "<code># заголовок</code>, списки <code>- пункт</code>, цитаты <code>&gt; текст</code>, таблицы. "
    "Форматирование, сделанное средствами Telegram, тоже сохранится.\n\n"
    "Сначала покажу, как будет выглядеть сообщение, и спрошу подтверждение."
)


def parse_admin_ids(raw: str | None) -> frozenset[int]:
    return frozenset(int(x) for x in re.findall(r"\d+", raw or ""))


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, admins: frozenset[int]) -> bool:
        return event.from_user is not None and event.from_user.id in admins


# ---------- учёт активности ----------

class ActivityMiddleware(BaseMiddleware):
    """Отмечает в базе каждого, кто пишет боту или жмёт кнопки, — для /stats и рассылки."""

    def __init__(self, storage: Storage):
        self.storage = storage

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is not None and not user.is_bot and (chat is None or chat.type == "private"):
            try:
                self.storage.touch(user.id, username=user.username,
                                   first_name=user.first_name, last_name=user.last_name)
            except sqlite3.Error:
                log.exception("Не удалось записать активность пользователя %s", user.id)
        return await handler(event, data)


@router.my_chat_member(F.chat.type == "private")
async def on_my_chat_member(event: ChatMemberUpdated, storage: Storage) -> None:
    """Telegram сообщает, когда пользователь блокирует бота или разблокирует его."""
    storage.set_blocked(event.from_user.id, event.new_chat_member.status == ChatMemberStatus.KICKED)


# ---------- /stats ----------

def format_stats(storage: Storage) -> str:
    s = storage.stats()
    periods = ((1, "сутки"), (7, "неделя"), (30, "месяц"))
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"👥 Пользователей: <b>{s.total}</b> (выбрали группу: {s.with_group})",
        f"🚫 Заблокировали бота: {s.blocked}",
        "🆕 Новые: " + " · ".join(f"{label} {s.new[d]}" for d, label in periods),
        "🔥 Активные: " + " · ".join(f"{label} {s.active[d]}" for d, label in periods),
        f"💾 Сохранено копий расписаний групп: {storage.saved_schedules_count()}",
    ]
    if s.langs:
        lines.append("🌐 Языки: " + " · ".join(f"{lang} {n}" for lang, n in s.langs.items()))
    if s.groups_by_faculty:
        lines += ["", "🏫 <b>По факультетам</b>",
                  ", ".join(f"{escape(f)} — {n}" for f, n in s.groups_by_faculty.items())]
    if s.top_groups:
        lines += ["", "🏆 <b>Топ групп</b>"]
        lines += [f"{i}. {escape(name)} — {n}" for i, (name, n) in enumerate(s.top_groups, 1)]
    return "\n".join(lines)


@router.message(Command("stats"), IsAdmin())
async def cmd_stats(message: Message, storage: Storage) -> None:
    await message.answer(format_stats(storage))


# ---------- /broadcast ----------

class Broadcast(StatesGroup):
    confirm = State()  # data["markdown"] — текст, ждём подтверждения


class BroadcastCb(CallbackData, prefix="bc"):
    action: str  # send | cancel


_broadcast_lock = asyncio.Lock()
_background: set[asyncio.Task] = set()  # держим ссылки, чтобы задачи не собрал сборщик мусора


def to_rich_markdown(html_text: str) -> str:
    """Текст команды /broadcast → Markdown для rich-сообщения.

    Берём message.html_text: если админ выделил текст средствами Telegram (или клиент
    сам превратил **жирный** в форматирование), оно приходит HTML-тегами, а rich
    Markdown понимает вложенный HTML. Набранная вручную разметка остаётся как есть.
    """
    text = re.sub(r"^/\S+[ \t]*", "", html_text, count=1).strip()
    # html_text экранирует «>», а без него не работают цитаты Markdown;
    # «<» оставляем экранированным, чтобы он не открыл HTML-тег
    text = text.replace("&gt;", ">")

    # В Markdown одиночный перенос строки склеивает строки в абзац, а в Telegram
    # люди пишут построчно. Делаем такие переносы жёсткими (два пробела в конце строки),
    # кроме блоков кода.
    lines = text.split("\n")
    out = []
    in_code = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):  # граница блока кода Markdown
            in_code = not in_code
            out.append(line)
            continue
        if "<pre" in line:  # блок кода, оформленный средствами Telegram
            in_code = True
        if in_code:
            out.append(line)
            if "</pre>" in line:
                in_code = False
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and nxt.strip() and not nxt.lstrip().startswith("```"):
            line = line.rstrip() + "  "
        out.append(line)
    return "\n".join(out)


def confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Отправить ({count})",
                             callback_data=BroadcastCb(action="send").pack()),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=BroadcastCb(action="cancel").pack()),
    ]])


@router.message(Command("broadcast"), IsAdmin())
async def cmd_broadcast(message: Message, state: FSMContext, storage: Storage) -> None:
    markdown = to_rich_markdown(message.html_text)
    if not markdown:
        await message.answer(BROADCAST_USAGE)
        return
    if _broadcast_lock.locked():
        await message.answer("⏳ Предыдущая рассылка ещё идёт — дождись её окончания.")
        return

    # Превью ровно в том виде, в каком сообщение получат пользователи
    try:
        await message.answer_rich(InputRichMessage(markdown=markdown))
    except TelegramBadRequest as e:
        await message.answer(f"❌ Telegram не принял разметку: <code>{escape(e.message)}</code>")
        return

    count = len(storage.audience())
    await state.set_state(Broadcast.confirm)
    await state.update_data(markdown=markdown)
    await message.answer(f"👆 Так сообщение увидят пользователи. Отправить его {count} чел.?",
                         reply_markup=confirm_keyboard(count))


@router.callback_query(BroadcastCb.filter(F.action == "cancel"), IsAdmin())
async def on_broadcast_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await query.message.edit_text("✖️ Рассылка отменена.")


@router.callback_query(BroadcastCb.filter(F.action == "send"), IsAdmin())
async def on_broadcast_send(query: CallbackQuery, state: FSMContext, storage: Storage) -> None:
    markdown = (await state.get_data()).get("markdown")
    if await state.get_state() != Broadcast.confirm.state or not markdown:
        # например, бот перезапускался и черновик пропал
        await query.answer("Черновик не найден — отправь /broadcast заново.", show_alert=True)
        return
    if _broadcast_lock.locked():
        await query.answer("Предыдущая рассылка ещё идёт.", show_alert=True)
        return
    await state.clear()
    await query.answer()
    await query.message.edit_text("⏳ Рассылка началась…")
    task = asyncio.create_task(run_broadcast(query.bot, storage, markdown, query.message))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _send_one(bot: Bot, storage: Storage, user_id: int, markdown: str) -> str:
    """Отправляет одно сообщение. Возвращает 'sent', 'blocked' или 'failed'."""
    for _ in range(3):
        try:
            await bot.send_rich_message(user_id, InputRichMessage(markdown=markdown))
            return "sent"
        except TelegramRetryAfter as e:  # превысили лимит — Telegram говорит, сколько ждать
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:  # заблокировал бота или удалил аккаунт
            storage.set_blocked(user_id, True)
            return "blocked"
        except TelegramAPIError as e:
            log.warning("Рассылка: не доставлено %s: %s", user_id, e)
            return "failed"
    return "failed"


async def run_broadcast(bot: Bot, storage: Storage, markdown: str, report: Message) -> None:
    async with _broadcast_lock:
        users = storage.audience()
        result = {"sent": 0, "blocked": 0, "failed": 0}
        started = time.monotonic()
        log.info("Рассылка: старт, получателей %d", len(users))

        for i, user_id in enumerate(users, 1):
            try:
                result[await _send_one(bot, storage, user_id, markdown)] += 1
            except Exception:  # сеть и прочее — не прерываем рассылку из-за одного получателя
                log.exception("Рассылка: ошибка при отправке %s", user_id)
                result["failed"] += 1
            if i % PROGRESS_EVERY == 0 and i < len(users):
                try:
                    await report.edit_text(f"⏳ Рассылка: {i} из {len(users)}…")
                except TelegramAPIError:
                    pass
            await asyncio.sleep(SEND_INTERVAL)

        elapsed = int(time.monotonic() - started)
        log.info("Рассылка: готово %s за %d с", result, elapsed)
        text = (
            "✅ <b>Рассылка завершена</b>\n\n"
            f"Доставлено: {result['sent']} из {len(users)}\n"
            f"Заблокировали бота: {result['blocked']}\n"
            f"Ошибок: {result['failed']}\n"
            f"Время: {elapsed // 60} мин {elapsed % 60} с"
        )
        try:
            await report.edit_text(text)
        except TelegramAPIError:
            await bot.send_message(report.chat.id, text)


# ---------- не админ ----------

@router.message(Command("stats", "broadcast"))
async def not_admin(message: Message) -> None:
    """Для обычных пользователей этих команд нет — молчим. ID пишем в лог,
    чтобы админ мог найти свой и добавить в ADMIN_IDS."""
    log.info("Админская команда от не-админа: user_id=%s (@%s). "
             "Чтобы сделать его админом, добавь ID в ADMIN_IDS.",
             message.from_user.id, message.from_user.username or "-")
