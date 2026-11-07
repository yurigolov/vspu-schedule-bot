"""Клиент и парсер сайта расписания ВГПУ (https://rsp-vspu.online/).

Сайт на Django: на главной странице два <select> (группы и преподаватели),
а расписание отдаётся в ответ на POST /group или POST /teacher с CSRF-токеном.
Ответ — HTML-таблица примерно на месяц вперёд, начиная с понедельника текущей недели.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import aiohttp
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://rsp-vspu.online"
USER_AGENT = "Mozilla/5.0 (compatible; VSPU-schedule-bot/1.0)"

GROUP = "group"
TEACHER = "teacher"


@dataclass
class Lesson:
    time: str  # "09:00-10:35"
    number: int  # номер пары в дне, с 1
    text: str  # текст ячейки как на сайте, с нормализованными пробелами
    subgroup: str | None = None  # "1 п/г", если пара не у всей группы


@dataclass
class Day:
    date: date
    weekday: str  # "ПН", "ВТ", ...
    lessons: list[Lesson] = field(default_factory=list)
    week: str = ""  # «числитель» / «знаменатель» — с октября 2026 сайт пишет его под датой


@dataclass
class Schedule:
    kind: str  # GROUP или TEACHER
    name: str
    days: dict[date, Day]
    fetched_at: int = 0  # unix-время загрузки с сайта (0 — неизвестно)

    def day(self, d: date) -> Day | None:
        return self.days.get(d)

    def to_json(self) -> str:
        """Для хранения копии в базе."""
        return json.dumps({
            "kind": self.kind,
            "name": self.name,
            "days": [{"date": d.date.isoformat(), "weekday": d.weekday, "week": d.week,
                      "lessons": [asdict(lesson) for lesson in d.lessons]}
                     for d in self.days.values()],
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Schedule:
        data = json.loads(raw)
        days = {}
        for item in data["days"]:
            d = date.fromisoformat(item["date"])
            days[d] = Day(date=d, weekday=item["weekday"], week=item.get("week", ""),  # в старых копиях нет
                          lessons=[Lesson(**lesson) for lesson in item["lessons"]])
        return cls(kind=data["kind"], name=data["name"], days=days)


class SiteError(Exception):
    """Сайт недоступен или вернул что-то неожиданное."""


DATE_CELL = re.compile(r"\d{2}-\d{2}-\d{4}")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_options(html: str) -> tuple[list[str], list[str]]:
    """Достаёт списки групп и преподавателей с главной страницы."""
    soup = BeautifulSoup(html, "lxml")
    lists = []
    for select_id in ("group-select", "teacher-select"):
        select = soup.find("select", id=select_id)
        if select is None:
            raise SiteError(f"на странице нет списка {select_id}")
        values = [_clean(o.get("value", "")) for o in select.find_all("option")]
        lists.append([v for v in values if v])
    return lists[0], lists[1]


def parse_csrf(html: str) -> str:
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not m:
        raise SiteError("не найден CSRF-токен")
    return m.group(1)


def _is_hidden(tag: Tag) -> bool:
    return tag.has_attr("hidden")


def parse_schedule(html: str, kind: str, name: str) -> Schedule:
    """Разбирает таблицу расписания из ответа на POST /group или /teacher.

    Строение таблицы:
      - строка заголовка: Дата | День | Время | <по столбцу на подгруппу>;
      - строка с rowspan открывает новый день (дата, день недели);
      - строки class="gran" — пары: первая ячейка время, дальше ячейки подгрупп.
        Пара у всей группы — одна ячейка с colspan на все подгруппы.
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.find("div", class_="tb")
    table = container.find("table") if container else None
    if table is None:
        raise SiteError("в ответе нет таблицы расписания")

    subgroups = [_clean(td.get_text()) for td in table.find_all("td", class_="para")]
    multi = len(subgroups) > 1

    days: dict[date, Day] = {}
    current: Day | None = None
    number = 0

    for tr in table.find_all("tr"):
        cells = [td for td in tr.find_all("td", recursive=False) if not _is_hidden(td)]
        if not cells:
            continue

        if cells[0].has_attr("rowspan"):
            # Раньше в ячейке была только дата «05-10-2026», с октября 2026 —
            # «12-10-2026<br><br/>числитель». Берём дату регуляркой, остальное — тип недели.
            raw_date = _clean(cells[0].get_text(" "))
            m = DATE_CELL.search(raw_date)
            try:
                d = datetime.strptime(m.group(0), "%d-%m-%Y").date() if m else None
            except ValueError:
                d = None
            if d is None:
                current = None
                continue
            week = _clean(raw_date[m.end():]).lower()
            weekday = _clean(cells[1].get_text()) if len(cells) > 1 else ""
            current = days.setdefault(d, Day(date=d, weekday=weekday, week=week))
            number = 0
            continue

        if current is None or "gran" not in (tr.get("class") or []):
            continue

        number += 1
        slot_time = _clean(cells[0].get_text())
        column = 0
        for td in cells[1:]:
            span = int(td.get("colspan", 1) or 1)
            text = _clean(td.get_text())
            if text:
                subgroup = None
                if multi and span < len(subgroups):
                    subgroup = subgroups[column] if column < len(subgroups) else None
                current.lessons.append(
                    Lesson(time=slot_time, number=number, text=text, subgroup=subgroup)
                )
            column += span

    if not days:
        # Таблица есть, а дней не нашлось — значит, сайт поменял вёрстку. Считаем это
        # ошибкой сайта: иначе бот молча показал бы «пар нет» и затёр копии пустыми.
        raise SiteError("в таблице расписания не нашлось ни одного дня — изменилась вёрстка сайта?")
    return Schedule(kind=kind, name=name, days=days)


class VspuClient:
    """Асинхронный клиент сайта с простым кэшем в памяти."""

    def __init__(self, schedule_ttl: int = 15 * 60, lists_ttl: int = 6 * 60 * 60):
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._schedule_ttl = schedule_ttl
        self._lists_ttl = lists_ttl
        self._lists: tuple[float, list[str], list[str]] | None = None
        self._cache: dict[tuple[str, str], tuple[float, Schedule]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                # недолго: если сайт лежит, лучше быстрее показать сохранённую копию
                timeout=aiohttp.ClientTimeout(total=15),
                cookie_jar=aiohttp.CookieJar(),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _load_index(self) -> None:
        session = await self._get_session()
        try:
            async with session.get(f"{BASE_URL}/") as resp:
                if resp.status != 200:
                    raise SiteError(f"главная страница ответила {resp.status}")
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SiteError(f"сайт недоступен: {e}") from e
        self._token = parse_csrf(html)
        groups, teachers = parse_options(html)
        self._lists = (time.monotonic(), groups, teachers)

    async def _lists_fresh(self) -> tuple[list[str], list[str]]:
        async with self._lock:
            if self._lists is None or time.monotonic() - self._lists[0] > self._lists_ttl:
                await self._load_index()
            assert self._lists is not None
            return self._lists[1], self._lists[2]

    async def groups(self) -> list[str]:
        return (await self._lists_fresh())[0]

    async def teachers(self) -> list[str]:
        return (await self._lists_fresh())[1]

    def cached(self, kind: str, name: str) -> Schedule | None:
        """Свежее расписание из кэша в памяти — без обращения к сайту."""
        cached = self._cache.get((kind, name))
        if cached and time.monotonic() - cached[0] < self._schedule_ttl:
            return cached[1]
        return None

    async def schedule(self, kind: str, name: str, cache: bool = True) -> Schedule:
        """cache=False — не класть результат в кэш в памяти: так обходим все
        группы для индекса предметов, не раздувая память."""
        if kind not in (GROUP, TEACHER):
            raise ValueError(kind)
        key = (kind, name)
        if (cached := self.cached(kind, name)) is not None:
            return cached

        await self._lists_fresh()  # гарантирует, что есть CSRF-токен и cookie
        html = await self._post(kind, name)
        result = parse_schedule(html, kind, name)
        result.fetched_at = int(time.time())
        if cache:
            self._cache[key] = (time.monotonic(), result)
        return result

    async def _post(self, kind: str, name: str, retry: bool = True) -> str:
        session = await self._get_session()
        data = {"csrfmiddlewaretoken": self._token or "", kind: name}
        try:
            async with session.post(
                f"{BASE_URL}/{kind}", data=data, headers={"Referer": f"{BASE_URL}/"}
            ) as resp:
                if resp.status == 403 and retry:
                    # протух CSRF-токен или cookie — берём новые и пробуем ещё раз
                    async with self._lock:
                        await self._load_index()
                    return await self._post(kind, name, retry=False)
                if resp.status != 200:
                    raise SiteError(f"/{kind} ответил {resp.status}")
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SiteError(f"сайт недоступен: {e}") from e
