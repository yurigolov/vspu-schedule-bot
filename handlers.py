"""Команды и кнопки бота. Бот для студентов, интерфейс на русском, английском
и португальском (тексты — в locales/*.toml).

Расписание своей группы открывается кнопками под полем ввода. Расписание
преподавателя или другой группы можно посмотреть карточкой — через /teacher
или поиск текстом. Своя группа при этом не меняется.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import schedules
from formatting import (TZ, format_day, format_now, format_subject_search, rich_day, rich_now,
                        rich_subject_search, rich_week, saved_copy_note, subject_hits, week_dates)
from i18n import LANGS, guess_lang, t, variants
from search import find
from storage import Storage
from vspu import GROUP, TEACHER, Schedule, SiteError, VspuClient

log = logging.getLogger(__name__)
router = Router()

MAX_MATCHES = 40  # больше кнопок в ответ на поиск показывать бессмысленно
MAX_SUBJECTS_SHOWN = 10  # предметов в ответ на /subject; остальные — «и ещё N»
MAX_SUBJECT_MATCHES = 200  # если подходит больше — просим уточнить
MAX_TEACHER_BUTTONS = 20

# Периоды расписания и их кнопки. Короткие коды — из-за лимита в 64 байта
# на callback_data. «Через неделю» — потому что расписание чередуется.
PERIODS = {"now": "now", "d0": "today", "d1": "tomorrow", "w": "week", "w1": "next_week"}

# Нажатие кнопки под полем ввода на любом языке → период
BUTTON_PERIODS = {text: period for period, key in PERIODS.items() for text in variants(f"buttons.{key}")}
COMMAND_PERIODS = {"now": "now", "today": "d0", "tomorrow": "d1", "week": "w", "nextweek": "w1"}

# Текст, который ищем как группу или фамилию: не команда и не нажатие кнопки
# расписания — иначе посреди выбора группы кнопка ушла бы в поиск.
IS_SEARCH_TEXT = F.text & ~F.text.startswith("/") & ~F.text.in_(set(BUTTON_PERIODS))

# Админские команды — только по-русски: ими пользуется админ
ADMIN_COMMANDS = [
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="broadcast", description="Рассылка"),
]


def bot_commands(lang: str, admin: bool = False) -> list[BotCommand]:
    commands = [BotCommand(command=c, description=t(lang, f"commands.{c}"))
                for c in ("teacher", "subject", "group", "language")]
    return commands + ADMIN_COMMANDS if admin else commands


def main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    def button(key: str) -> KeyboardButton:
        return KeyboardButton(text=t(lang, f"buttons.{key}"))
    return ReplyKeyboardMarkup(
        keyboard=[[button("now"), button("today"), button("tomorrow")],
                  [button("week"), button("next_week")]],
        resize_keyboard=True,
    )


class LangMiddleware(BaseMiddleware):
    """Передаёт в обработчики аргумент lang — язык пользователя. Пока он не
    выбран, берём язык из настроек Telegram (если он у бота есть)."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        storage: Storage | None = data.get("storage")
        lang = None
        if user is not None:
            lang = (storage.get_lang(user.id) if storage else None) or guess_lang(user.language_code)
        data["lang"] = lang or guess_lang(None)
        return await handler(event, data)


# Подключён прямо к роутеру: всем обработчикам ниже нужен аргумент lang
router.message.outer_middleware(LangMiddleware())
router.callback_query.outer_middleware(LangMiddleware())


class Choose(StatesGroup):
    group = State()    # выбор своей группы
    teacher = State()  # поиск преподавателя для просмотра
    subject = State()  # поиск преподавателя по предмету


class LangCb(CallbackData, prefix="l"):
    """Выбран язык. then_group — дальше спросить группу (так при /start)."""
    code: str
    then_group: bool = False


class PickCb(CallbackData, prefix="p"):
    """Выбрана группа или преподаватель. save — сделать группу своей,
    иначе только показать расписание."""
    kind: str
    name: str
    save: bool = False


class NavCb(CallbackData, prefix="n"):
    """Навигация по спискам: факультет → направление → группа, буква → преподаватель."""
    kind: str
    level: int
    key: str = ""
    save: bool = False


class ViewCb(CallbackData, prefix="v"):
    """Переключить период в карточке чужого расписания."""
    kind: str
    name: str
    period: str


class PinGroupCb(CallbackData, prefix="g"):
    """Сделать просматриваемую группу своей."""
    name: str


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def my_group(storage: Storage, user_id: int) -> str | None:
    # Раньше бот позволял сохранить преподавателя — такой выбор теперь не считаем.
    choice = storage.get(user_id)
    return choice[1] if choice and choice[0] == GROUP else None


async def set_chat_commands(bot: Bot, chat_id: int, lang: str, admin: bool) -> None:
    """Меню команд на языке пользователя."""
    try:
        await bot.set_my_commands(bot_commands(lang, admin), scope=BotCommandScopeChat(chat_id=chat_id))
    except TelegramAPIError as e:
        log.warning("Не удалось задать меню для чата %s: %s", chat_id, e)


# ---------- клавиатуры ----------

def _grid(buttons: list[InlineKeyboardButton], width: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.add(*buttons)
    kb.adjust(width)
    return kb


def lang_keyboard(then_group: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t(code, "lang.name"),
                             callback_data=LangCb(code=code, then_group=then_group).pack())
        for code in LANGS
    ]])


def matches_keyboard(kind: str, names: list[str], save: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=n, callback_data=PickCb(kind=kind, name=n, save=save).pack())
        for n in names
    ]
    return _grid(buttons, 2).as_markup()


def group_prompt_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "group.from_list"),
                              callback_data=NavCb(kind=GROUP, level=0, save=True).pack())],
    ])


def teacher_prompt_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "teacher.by_letter"),
                              callback_data=NavCb(kind=TEACHER, level=0).pack())],
    ])


def card_keyboard(kind: str, name: str, period: str, pinned: bool, lang: str = "ru") -> InlineKeyboardMarkup:
    """Кнопки под карточкой чужого расписания: переключение периода,
    а для чужой группы — ещё «сделать моей группой»."""
    buttons = [
        InlineKeyboardButton(text=("• " if p == period else "") + t(lang, f"periods.{key}"),
                             callback_data=ViewCb(kind=kind, name=name, period=p).pack())
        for p, key in PERIODS.items()
    ]
    rows = [buttons[:3], buttons[3:]]  # пять в ряд не помещаются на телефоне
    if kind == GROUP and not pinned:
        rows.append([InlineKeyboardButton(text=t(lang, "group.pin"),
                                          callback_data=PinGroupCb(name=name).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _faculty(group: str) -> str:
    return group.split("-", 1)[0]  # «ИЯ-АН_Оb251» → «ИЯ»


def _direction(group: str) -> str:
    return group.split("_", 1)[0]  # «ИЯ-АН_Оb251» → «ИЯ-АН»


async def nav_view(client: VspuClient, cb: NavCb, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура для очередного шага выбора из списка."""
    save = cb.save
    back = t(lang, "common.back")

    def nav(kind: str, level: int, key: str = "") -> str:
        return NavCb(kind=kind, level=level, key=key, save=save).pack()

    def pick(kind: str, name: str) -> str:
        return PickCb(kind=kind, name=name, save=save).pack()

    if cb.kind == GROUP:
        groups = await client.groups()
        if cb.level == 0:
            keys = sorted({_faculty(g) for g in groups})
            buttons = [InlineKeyboardButton(text=k, callback_data=nav(GROUP, 1, k)) for k in keys]
            return t(lang, "group.nav_faculty"), _grid(buttons, 4).as_markup()

        if cb.level == 1:
            keys = sorted({_direction(g) for g in groups if _faculty(g) == cb.key})
            buttons = [InlineKeyboardButton(text=k, callback_data=nav(GROUP, 2, k)) for k in keys]
            kb = _grid(buttons, 3)
            kb.row(InlineKeyboardButton(text=back, callback_data=nav(GROUP, 0)))
            return t(lang, "group.nav_direction", faculty=escape(cb.key)), kb.as_markup()

        names = [g for g in groups if _direction(g) == cb.key]
        buttons = [InlineKeyboardButton(text=n, callback_data=pick(GROUP, n)) for n in names]
        kb = _grid(buttons, 2)
        kb.row(InlineKeyboardButton(text=back, callback_data=nav(GROUP, 1, _faculty(cb.key))))
        return t(lang, "group.nav_group", direction=escape(cb.key)), kb.as_markup()

    teachers = await client.teachers()
    if cb.level == 0:
        by_letter = defaultdict(int)
        for name in teachers:
            by_letter[name[0].upper()] += 1
        buttons = [InlineKeyboardButton(text=k, callback_data=nav(TEACHER, 1, k)) for k in sorted(by_letter)]
        return t(lang, "teacher.nav_letter"), _grid(buttons, 6).as_markup()

    names = [n for n in teachers if n[0].upper() == cb.key]
    buttons = [InlineKeyboardButton(text=n, callback_data=pick(TEACHER, n)) for n in names]
    kb = _grid(buttons, 2)
    kb.row(InlineKeyboardButton(text=back, callback_data=nav(TEACHER, 0)))
    return t(lang, "teacher.nav_list", letter=escape(cb.key)), kb.as_markup()


# ---------- язык, выбор своей группы и поиск ----------

async def ask_language(message: Message, then_group: bool) -> None:
    # Пользователь, возможно, ещё не понимает ни один язык бота — спрашиваем на всех сразу
    await message.answer("\n".join(t(code, "lang.prompt") for code in LANGS),
                         reply_markup=lang_keyboard(then_group))


async def ask_group(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(Choose.group)
    await message.answer(t(lang, "group.prompt"), reply_markup=group_prompt_keyboard(lang))


async def ask_teacher(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(Choose.teacher)
    await message.answer(t(lang, "teacher.prompt"), reply_markup=teacher_prompt_keyboard(lang))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ask_language(message, then_group=True)


@router.message(Command("language", "lang"))
async def cmd_language(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ask_language(message, then_group=False)


@router.callback_query(LangCb.filter())
async def on_language(query: CallbackQuery, callback_data: LangCb, state: FSMContext,
                      storage: Storage, admins: frozenset[int] = frozenset()) -> None:
    lang = callback_data.code if callback_data.code in LANGS else "ru"
    storage.set_lang(query.from_user.id, lang)
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await set_chat_commands(query.bot, query.message.chat.id, lang, query.from_user.id in admins)

    if callback_data.then_group or my_group(storage, query.from_user.id) is None:
        await query.message.answer(t(lang, "start.hello"))
        await ask_group(query.message, state, lang)
    else:
        # клавиатура под полем ввода — уже на новом языке
        await query.message.answer(t(lang, "lang.chosen"), reply_markup=main_keyboard(lang))


@router.message(Command("group"))
# Кнопки «Сменить группу» больше нет, но у старых пользователей она остаётся
# на клавиатуре до следующего ответа бота — не отвечаем им «не понял».
@router.message(F.text == "⚙️ Сменить группу")
async def cmd_group(message: Message, state: FSMContext, lang: str) -> None:
    await ask_group(message, state, lang)


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext, storage: Storage, lang: str) -> None:
    """Скрытая команда: стирает всё, что бот знает о пользователе, и начинает
    регистрацию заново — с выбора языка. Нигде не упоминается — ни в меню, ни в /help."""
    storage.delete(message.from_user.id)
    await state.clear()
    try:
        await message.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=message.chat.id))
    except TelegramAPIError:
        pass
    await message.answer(t(lang, "common.reset_done"), reply_markup=ReplyKeyboardRemove())
    await ask_language(message, then_group=True)


@router.message(Command("teacher"))
async def cmd_teacher(message: Message, state: FSMContext, lang: str) -> None:
    await ask_teacher(message, state, lang)


@router.callback_query(NavCb.filter())
async def on_nav(query: CallbackQuery, callback_data: NavCb, client: VspuClient, lang: str) -> None:
    try:
        text, kb = await nav_view(client, callback_data, lang)
    except SiteError:
        await query.answer(t(lang, "common.site_down"), show_alert=True)
        return
    await query.answer()
    await query.message.edit_text(text, reply_markup=kb)


@router.callback_query(PickCb.filter())
async def on_pick(query: CallbackQuery, callback_data: PickCb, state: FSMContext,
                  client: VspuClient, storage: Storage, lang: str) -> None:
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    # save учитываем только для групп: старые кнопки могли предлагать сохранить преподавателя
    if callback_data.save and callback_data.kind == GROUP:
        await save_group(query.message, query.from_user.id, callback_data.name, client, storage, lang)
    else:
        await send_card(query.message, query.from_user.id, callback_data.kind, callback_data.name,
                        "d0", client, storage, lang, edit=False)


async def save_group(message: Message, user_id: int, name: str,
                     client: VspuClient, storage: Storage, lang: str) -> None:
    storage.set(user_id, GROUP, name)
    await message.answer(t(lang, "group.saved", group=escape(name)), reply_markup=main_keyboard(lang))
    await send_own(message, name, "d0", client, storage, lang)


async def handle_search(message: Message, kind: str, save: bool, state: FSMContext,
                        client: VspuClient, storage: Storage, lang: str) -> bool:
    """Ищет введённый текст среди групп или преподавателей. Найденную группу
    сохраняет как свою (save), остальное показывает карточкой.
    Возвращает False, если ничего не нашлось."""
    try:
        names = await (client.groups() if kind == GROUP else client.teachers())
    except SiteError:
        await message.answer(t(lang, "common.site_down"))
        return True

    save = save and kind == GROUP
    matches = find(message.text, names)
    if not matches:
        return False
    if len(matches) == 1:
        await state.clear()
        if save:
            await save_group(message, message.from_user.id, matches[0], client, storage, lang)
        else:
            await send_card(message, message.from_user.id, kind, matches[0], "d0",
                            client, storage, lang, edit=False)
    elif len(matches) > MAX_MATCHES:
        await message.answer(t(lang, "search.too_many", count=len(matches)))
    else:
        await message.answer(t(lang, "search.pick"), reply_markup=matches_keyboard(kind, matches, save))
    return True


@router.message(Choose.group, IS_SEARCH_TEXT)
async def on_group_text(message: Message, state: FSMContext,
                        client: VspuClient, storage: Storage, lang: str) -> None:
    if not await handle_search(message, GROUP, True, state, client, storage, lang):
        await message.answer(t(lang, "group.not_found"), reply_markup=group_prompt_keyboard(lang))


@router.message(Choose.teacher, IS_SEARCH_TEXT)
async def on_teacher_text(message: Message, state: FSMContext,
                          client: VspuClient, storage: Storage, lang: str) -> None:
    if not await handle_search(message, TEACHER, False, state, client, storage, lang):
        await message.answer(t(lang, "teacher.not_found"), reply_markup=teacher_prompt_keyboard(lang))


# ---------- поиск преподавателя по предмету ----------

@router.message(Command("subject"))
async def cmd_subject(message: Message, command: CommandObject, state: FSMContext,
                      storage: Storage, lang: str) -> None:
    """/subject философия — сразу ищет; /subject без слов — спрашивает предмет."""
    if command.args and command.args.strip():
        await state.clear()
        await send_subject_search(message, command.args.strip(), storage, lang)
    else:
        await state.set_state(Choose.subject)
        await message.answer(t(lang, "subject.prompt"))


@router.message(Choose.subject, IS_SEARCH_TEXT)
async def on_subject_text(message: Message, state: FSMContext, storage: Storage, lang: str) -> None:
    await state.clear()
    await send_subject_search(message, message.text.strip(), storage, lang)


async def send_subject_search(message: Message, query: str, storage: Storage, lang: str) -> None:
    """Кто ведёт предмет — по индексу, собранному из расписаний всех групп.
    Работает и когда сайт недоступен: индекс лежит в базе."""
    names = storage.subject_names()
    if not names:
        await message.answer(t(lang, "subject.building"))
        return
    matches = find(query, names)
    if not matches:
        await message.answer(t(lang, "subject.not_found"))
        return
    if len(matches) > MAX_SUBJECT_MATCHES:
        await message.answer(t(lang, "subject.too_many", count=len(matches)))
        return

    hits = subject_hits(storage.subject_rows(matches), my_group(storage, message.from_user.id))
    shown = hits[:MAX_SUBJECTS_SHOWN]
    updated_at = storage.subject_index_updated_at()

    # кнопки преподавателей — в том же порядке, что в таблице, без повторов
    teachers = list(dict.fromkeys(name for hit in shown for name, _, _ in hit.teachers))
    in_my_group = {name for hit in shown for name, _, mine in hit.teachers if mine}
    buttons = [InlineKeyboardButton(text=("⭐ " if name in in_my_group else "👤 ") + name,
                                    callback_data=PickCb(kind=TEACHER, name=name).pack())
               for name in teachers[:MAX_TEACHER_BUTTONS]]
    markup = _grid(buttons, 2).as_markup() if buttons else None

    await send_rich(message,
                    rich_subject_search(query, shown, len(hits), updated_at, lang),
                    [format_subject_search(query, shown, len(hits), updated_at, lang)],
                    markup)


# ---------- расписание ----------

async def load_schedule(message: Message, kind: str, name: str, client: VspuClient,
                        storage: Storage, lang: str) -> tuple[Schedule, int | None] | None:
    """Расписание с сайта или, если он недоступен, сохранённая копия
    (тогда второе значение — время копии)."""
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        return await schedules.get_schedule(client, storage, kind, name)
    except SiteError as e:
        log.warning("Не удалось получить расписание %s %s: %s", kind, name, e)
        await message.answer(t(lang, "common.site_down"))
        return None


def render(schedule: Schedule, period: str, saved_at: int | None = None,
           lang: str = "ru", at: datetime | None = None) -> tuple[str, list[str]]:
    """Rich-HTML с таблицами и запасной вариант обычным текстом.
    saved_at — время сохранённой копии, если сайт недоступен; at — «сейчас» (для тестов)."""
    kind, name = schedule.kind, schedule.name
    note = saved_copy_note(saved_at, lang) if saved_at else None
    at = at or now()
    if period == "now":
        return rich_now(schedule, at, note, lang), [format_now(schedule, at, note, lang)]
    if period in ("w", "w1"):
        dates = week_dates(at.date(), weeks_ahead=1 if period == "w1" else 0)
        # В запасном варианте — по сообщению на день: неделя целиком не влезет в 4096 символов.
        fallback = [format_day(schedule.day(d), d, kind, name, note, lang) for d in dates
                    if d.weekday() != 6 or (schedule.day(d) is not None and schedule.day(d).lessons)]
        title = t(lang, "schedule.next_week_title" if period == "w1" else "schedule.week_title")
        return rich_week(schedule, dates, title, note, lang), fallback
    d = at.date() + timedelta(days=int(period[1]))
    day = schedule.day(d)
    return rich_day(day, d, kind, name, note, lang), [format_day(day, d, kind, name, note, lang)]


async def send_rich(message: Message, rich_html: str, fallback: list[str], reply_markup) -> None:
    """Отправляет расписание таблицей (rich-сообщение). Если Telegram его не принял,
    отправляет обычным текстом, чтобы пользователь не остался без ответа."""
    try:
        await message.answer_rich(InputRichMessage(html=rich_html), reply_markup=reply_markup)
    except TelegramBadRequest as e:
        log.warning("Rich-сообщение не отправилось (%s), шлю обычный текст", e)
        for i, text in enumerate(fallback):
            await message.answer(text, reply_markup=reply_markup if i == len(fallback) - 1 else None)


async def send_own(message: Message, group: str, period: str,
                   client: VspuClient, storage: Storage, lang: str) -> None:
    """Расписание своей группы."""
    loaded = await load_schedule(message, GROUP, group, client, storage, lang)
    if loaded is None:
        return
    schedule, saved_at = loaded
    rich_html, fallback = render(schedule, period, saved_at, lang)
    await send_rich(message, rich_html, fallback, main_keyboard(lang))


async def send_card(message: Message, user_id: int, kind: str, name: str, period: str,
                    client: VspuClient, storage: Storage, lang: str, edit: bool) -> None:
    """Расписание преподавателя или чужой группы карточкой с переключением периода.
    Своя группа не меняется."""
    loaded = await load_schedule(message, kind, name, client, storage, lang)
    if loaded is None:
        return
    schedule, saved_at = loaded
    rich_html, fallback = render(schedule, period, saved_at, lang)
    markup = card_keyboard(kind, name, period, my_group(storage, user_id) == name, lang)
    if edit:
        try:
            await message.edit_text(rich_message=InputRichMessage(html=rich_html),
                                    reply_markup=markup, parse_mode=None)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):  # нажали уже выбранный период
                return
            log.warning("Не удалось обновить карточку (%s), шлю новую", e)
    await send_rich(message, rich_html, fallback, markup)


async def require_group(message: Message, state: FSMContext, storage: Storage, lang: str) -> str | None:
    group = my_group(storage, message.from_user.id)
    if group is None:
        await message.answer(t(lang, "group.choose_first"))
        await ask_group(message, state, lang)
        return None
    await state.clear()
    return group


@router.message(F.text.in_(set(BUTTON_PERIODS)))
async def on_period_button(message: Message, state: FSMContext, client: VspuClient,
                           storage: Storage, lang: str) -> None:
    if group := await require_group(message, state, storage, lang):
        await send_own(message, group, BUTTON_PERIODS[message.text], client, storage, lang)


@router.message(Command(*COMMAND_PERIODS))
async def on_period_command(message: Message, command: CommandObject, state: FSMContext,
                            client: VspuClient, storage: Storage, lang: str) -> None:
    if group := await require_group(message, state, storage, lang):
        await send_own(message, group, COMMAND_PERIODS[command.command.lower()], client, storage, lang)


@router.callback_query(ViewCb.filter())
async def on_view(query: CallbackQuery, callback_data: ViewCb,
                  client: VspuClient, storage: Storage, lang: str) -> None:
    await query.answer()
    period = callback_data.period if callback_data.period in PERIODS else "d0"
    await send_card(query.message, query.from_user.id, callback_data.kind, callback_data.name,
                    period, client, storage, lang, edit=True)


@router.callback_query(PinGroupCb.filter())
async def on_pin_group(query: CallbackQuery, callback_data: PinGroupCb, storage: Storage, lang: str) -> None:
    storage.set(query.from_user.id, GROUP, callback_data.name)
    await query.answer(t(lang, "group.pinned_toast"))
    # убираем кнопку «сделать моей группой», переключатель периода оставляем
    markup = query.message.reply_markup
    if markup:
        pin_prefix = PinGroupCb.__prefix__ + PinGroupCb.__separator__
        rows = [row for row in markup.inline_keyboard
                if not any((b.callback_data or "").startswith(pin_prefix) for b in row)]
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await query.message.answer(t(lang, "group.pinned", group=escape(callback_data.name)),
                               reply_markup=main_keyboard(lang))


@router.callback_query()
async def on_stale_button(query: CallbackQuery, lang: str) -> None:
    """Кнопки из старых версий бота (например, «Это я — сделать основным»)."""
    await query.answer(t(lang, "common.stale_button"), show_alert=True)


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str) -> None:
    await message.answer(t(lang, "common.help"))


@router.message(F.text, ~F.text.startswith("/"))
async def on_any_text(message: Message, state: FSMContext,
                      client: VspuClient, storage: Storage, lang: str) -> None:
    """Текст вне сценария выбора: ищем группу, затем преподавателя.
    Группу сохраняем как свою, только если своей ещё нет; остальное — карточкой."""
    has_group = my_group(storage, message.from_user.id) is not None
    if await handle_search(message, GROUP, not has_group, state, client, storage, lang):
        return
    if await handle_search(message, TEACHER, False, state, client, storage, lang):
        return
    await message.answer(t(lang, "search.not_understood"))
