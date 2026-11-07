"""Превращает расписание в сообщения Telegram на языке пользователя.

Основной вид — rich-сообщение (Bot API 10.2+, sendRichMessage) с таблицей.
Обычный HTML-текст остаётся запасным вариантом на случай, если Telegram
отклонит rich-сообщение. Тексты интерфейса — в locales/*.toml; названия
предметов, фамилии и аудитории остаются как на сайте.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from itertools import groupby
from zoneinfo import ZoneInfo

from i18n import DEFAULT_LANG, has, t, tl
from vspu import GROUP, Day, Lesson, Schedule

TZ = ZoneInfo("Europe/Moscow")  # Воронеж

# В расписании группы: «Предмет(тип)-Преподаватель - аудитория».
# Название предмета само может содержать скобки, поэтому берём последние перед «)-».
GROUP_CELL = re.compile(r"^(?P<subject>.+)\((?P<type>[^()]*)\)-(?P<who>.*?)\s+-\s*(?P<room>.*)$")
# В расписании преподавателя: «Предмет(тип)-аудитория[группа],[группа]».
TEACHER_CELL = re.compile(r"^(?P<subject>.+)\((?P<type>[^()]*)\)-(?P<room>.*?)(?P<groups>(?:\[[^\]]*\],?)+)$")
SUBGROUP = re.compile(r"^(\d+)\s*п/г$")


@dataclass
class LessonView:
    """Ячейка расписания, разобранная на части. Если формат незнакомый,
    заполнено только raw — тогда ячейку показываем как есть."""
    raw: str
    subject: str = ""
    type: str = ""  # сокращение с сайта: «л.», «пр.» …
    who: str = ""  # преподаватель (для группы) или группы через запятую (для преподавателя)
    room: str = ""

    @property
    def parsed(self) -> bool:
        return bool(self.subject)


def parse_lesson(lesson: Lesson, kind: str) -> LessonView:
    m = (GROUP_CELL if kind == GROUP else TEACHER_CELL).match(lesson.text)
    if not m:
        return LessonView(raw=lesson.text)
    if kind == GROUP:
        who = m["who"].strip()
    else:
        who = ", ".join(re.findall(r"\[([^\]]*)\]", m["groups"]))
    return LessonView(raw=lesson.text, subject=m["subject"].strip(), type=m["type"].strip(),
                      who=who, room=m["room"].strip())


def lesson_type_name(code: str, lang: str) -> str:
    key = f"lesson_types.{code}"
    return t(lang, key) if code and has(lang, key) else code


def subgroup_name(raw: str, lang: str) -> str:
    """«1 п/г» с сайта → «Subgroup 1» и т. п."""
    m = SUBGROUP.match(raw.strip())
    return t(lang, "schedule.subgroup", n=m.group(1)) if m else raw


def week_type_name(week: str, lang: str) -> str:
    """«числитель» с сайта → «numerator week» и т. п. Пусто — сайт тип не указал."""
    key = f"week_types.{week}"
    return t(lang, key) if week and has(lang, key) else week


def _with_week(text: str, days: list[Day | None], lang: str) -> str:
    """Дописывает тип недели к заголовку: «Понедельник, 12 октября · числитель»."""
    week = next((d.week for d in days if d is not None and d.week), "")
    return f"{text} · {week_type_name(week, lang)}" if week else text


def human_date(d: date, lang: str = DEFAULT_LANG) -> str:
    return t(lang, "schedule.date", weekday=tl(lang, "schedule.weekdays")[d.weekday()],
             day=d.day, month=tl(lang, "schedule.months")[d.month - 1])


def date_range(first: date, last: date, lang: str = DEFAULT_LANG) -> str:
    months = tl(lang, "schedule.months")
    if first.month == last.month:
        return t(lang, "schedule.range_same_month", d1=first.day, d2=last.day, month=months[last.month - 1])
    return t(lang, "schedule.range", d1=first.day, month1=months[first.month - 1],
             d2=last.day, month2=months[last.month - 1])


def week_dates(today: date, weeks_ahead: int = 0) -> list[date]:
    """Даты недели для кнопки «Неделя»: текущая неделя с понедельника,
    а в воскресенье — уже следующая. weeks_ahead=1 — неделя после неё
    («Через неделю»: расписание чередуется по неделям)."""
    monday = today - timedelta(days=today.weekday())
    if today.weekday() == 6:
        monday += timedelta(days=7)
    monday += timedelta(days=7 * weeks_ahead)
    return [monday + timedelta(days=i) for i in range(7)]


def saved_copy_note(fetched_at: int, lang: str = DEFAULT_LANG) -> str:
    """Пометка, что сайт недоступен и показана сохранённая копия."""
    when = datetime.fromtimestamp(fetched_at, TZ)
    day_month = t(lang, "schedule.day_month", day=when.day,
                  month=tl(lang, "schedule.months")[when.month - 1])
    return t(lang, "schedule.saved_copy", date=day_month, time=f"{when:%H:%M}")


def _who_icon(kind: str) -> str:
    return "👥" if kind == GROUP else "👤"


def _slot_time(slot: str) -> str:
    start, _, end = slot.partition("-")
    return f"{start}–{end}" if end else slot


def _subject_html(view: LessonView, subgroup: str | None, lang: str) -> str:
    text = escape(view.subject) if view.parsed else escape(view.raw)
    if view.parsed and view.type:
        text += f" <i>({escape(lesson_type_name(view.type, lang))})</i>"
    if subgroup:
        text = f"<u>{escape(subgroup_name(subgroup, lang))}</u> {text}"
    return text


def _header(kind: str, name: str, note: str | None) -> list[str]:
    parts = [f"<p>{_who_icon(kind)} {escape(name)}</p>"]
    if note:
        parts.append(f"<p><i>{escape(note)}</i></p>")
    return parts


# ---------- rich-сообщения с таблицами ----------

def _td(content: str, **attrs: object) -> str:
    attr_html = "".join(f' {k}="{v}"' for k, v in attrs.items() if v not in (None, 1))
    return f"<td{attr_html}>{content}</td>"


def rich_table(day: Day, kind: str, lang: str = DEFAULT_LANG) -> str:
    who_header = t(lang, "schedule.col_teacher" if kind == GROUP else "schedule.col_groups")
    headers = [t(lang, "schedule.col_number"), t(lang, "schedule.col_time"),
               t(lang, "schedule.col_subject"), who_header, t(lang, "schedule.col_room")]
    rows = ["<tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers) + "</tr>"]

    for (number, slot), group in groupby(day.lessons, key=lambda l: (l.number, l.time)):
        lessons = list(group)
        span = len(lessons)  # пары подгрупп в одно время — отдельные строки с общими № и временем
        for i, lesson in enumerate(lessons):
            cells = []
            if i == 0:
                cells.append(_td(str(number), rowspan=span, align="center", valign="middle"))
                cells.append(_td(_slot_time(slot), rowspan=span, valign="middle"))
            view = parse_lesson(lesson, kind)
            if view.parsed:
                cells.append(_td(_subject_html(view, lesson.subgroup, lang)))
                cells.append(_td(escape(view.who)))
                cells.append(_td(escape(view.room)))
            else:
                cells.append(_td(_subject_html(view, lesson.subgroup, lang), colspan=3))
            rows.append("<tr>" + "".join(cells) + "</tr>")

    return "<table bordered striped compact>" + "".join(rows) + "</table>"


def _rich_day_body(day: Day | None, lang: str) -> str | None:
    if day is None:
        return f"<p>{escape(t(lang, 'schedule.no_data'))}</p>"
    if not day.lessons:
        return f"<p>{escape(t(lang, 'schedule.no_lessons'))}</p>"
    return None


def rich_day(day: Day | None, d: date, kind: str, name: str, note: str | None = None,
             lang: str = DEFAULT_LANG) -> str:
    parts = [f"<h3>📅 {escape(_with_week(human_date(d, lang), [day], lang))}</h3>", *_header(kind, name, note)]
    parts.append(_rich_day_body(day, lang) or rich_table(day, kind, lang))
    return "".join(parts)


def rich_week(schedule: Schedule, dates: list[date], title: str | None = None,
              note: str | None = None, lang: str = DEFAULT_LANG) -> str:
    title = title or t(lang, "schedule.week_title")
    heading = _with_week(f"{title}: {date_range(dates[0], dates[-1], lang)}",
                         [schedule.day(d) for d in dates], lang)
    parts = [f"<h2>🗓 {escape(heading)}</h2>", *_header(schedule.kind, schedule.name, note)]
    for d in dates:
        day = schedule.day(d)
        if d.weekday() == 6 and day is not None and not day.lessons:
            continue  # пустое воскресенье не показываем
        parts.append(f"<h4>{escape(human_date(d, lang))}</h4>")
        parts.append(_rich_day_body(day, lang) or rich_table(day, schedule.kind, lang))
    return "".join(parts)


# ---------- «Сейчас» ----------

@dataclass
class Slot:
    number: int
    start: datetime
    end: datetime
    lessons: list[Lesson]


def _slots(day: Day | None) -> list[Slot]:
    """Пары дня с началом и концом во времени Воронежа."""
    if day is None:
        return []
    slots = []
    for (number, slot), group in groupby(day.lessons, key=lambda l: (l.number, l.time)):
        m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", slot)
        if not m:
            continue
        h1, m1, h2, m2 = map(int, m.groups())
        slots.append(Slot(number,
                          datetime.combine(day.date, time(h1, m1), TZ),
                          datetime.combine(day.date, time(h2, m2), TZ),
                          list(group)))
    return slots


def duration(delta: timedelta, lang: str = DEFAULT_LANG) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / 60))
    h, m = divmod(minutes, 60)
    if not h:
        return t(lang, "now.dur_min", m=m)
    return t(lang, "now.dur_h_min", h=h, m=m) if m else t(lang, "now.dur_h", h=h)


def now_status(schedule: Schedule, now: datetime, lang: str = DEFAULT_LANG) -> tuple[str, Day | None]:
    """Что сейчас: текст статуса и пары, которые к нему относятся (идущая,
    следующая сегодня или ближайшая в другой день) — для таблицы под статусом."""
    def pair(slot: Slot) -> str:
        return t(lang, "schedule.pair", n=slot.number)

    def hm(dt: datetime) -> str:
        return f"{dt:%H:%M}"

    slots = _slots(schedule.day(now.date()))
    current = next((s for s in slots if s.start <= now < s.end), None)
    if current:
        status = t(lang, "now.in_progress", pair=pair(current), start=hm(current.start),
                   end=hm(current.end), left=duration(current.end - now, lang))
        return status, Day(now.date(), "", current.lessons)

    upcoming = next((s for s in slots if s.start > now), None)
    if upcoming:
        started = any(s.end <= now for s in slots)
        key = "now.break" if started else "now.not_started"
        status = t(lang, key, pair=pair(upcoming), start=hm(upcoming.start),
                   left=duration(upcoming.start - now, lang))
        return status, Day(now.date(), "", upcoming.lessons)

    status = t(lang, "now.done_today" if slots else "now.no_today")
    for offset in range(1, 15):
        d = now.date() + timedelta(days=offset)
        future = _slots(schedule.day(d))
        if future:
            first = future[0]
            status += " " + t(lang, "now.next_day", date=human_date(d, lang), start=hm(first.start))
            return status, Day(d, "", first.lessons)
    return status + " " + t(lang, "now.none_soon"), None


def rich_now(schedule: Schedule, now: datetime, note: str | None = None,
             lang: str = DEFAULT_LANG) -> str:
    status, day = now_status(schedule, now, lang)
    parts = [f"<h3>{escape(t(lang, 'now.title', time=f'{now:%H:%M}'))}</h3>",
             *_header(schedule.kind, schedule.name, note), f"<p>{escape(status)}</p>"]
    if day is not None and day.lessons:
        parts.append(rich_table(day, schedule.kind, lang))
    return "".join(parts)


def format_now(schedule: Schedule, now: datetime, note: str | None = None,
               lang: str = DEFAULT_LANG) -> str:
    status, day = now_status(schedule, now, lang)
    lines = [f"<b>{escape(t(lang, 'now.title', time=f'{now:%H:%M}'))}</b>",
             f"{_who_icon(schedule.kind)} {escape(schedule.name)}"]
    if note:
        lines.append(f"<i>{escape(note)}</i>")
    lines += ["", escape(status)]
    if day is not None:
        for lesson in day.lessons:
            body = _plain_lesson_lines(lesson, schedule.kind, lang)
            if lesson.subgroup:
                body[0] = f"<u>{escape(subgroup_name(lesson.subgroup, lang))}</u>: {body[0]}"
            lines += [""] + body
    return "\n".join(lines)


# ---------- поиск преподавателя по предмету ----------

MAX_GROUPS_SHOWN = 3  # дальше — «+N», иначе у лектора потока строка на полэкрана


@dataclass
class SubjectHit:
    subject: str
    teachers: list[tuple[str, list[str], bool]]  # (преподаватель, его группы, ведёт ли в моей)

    @property
    def mine(self) -> bool:
        return any(m for _, _, m in self.teachers)

    @property
    def size(self) -> int:
        return sum(len(groups) for _, groups, _ in self.teachers)


def subject_hits(rows: list[tuple[str, str, str]], my_group: str | None) -> list[SubjectHit]:
    """Строки индекса (предмет, преподаватель, группа) → по предметам. Сначала то,
    что ведут в моей группе, потом — что шире преподаётся."""
    by_subject: dict[str, dict[str, list[str]]] = {}
    for subject, teacher, grp in rows:
        by_subject.setdefault(subject, {}).setdefault(teacher, []).append(grp)

    hits = []
    for subject, teachers in by_subject.items():
        items = []
        for teacher, groups in teachers.items():
            mine = my_group in groups
            groups = sorted(groups, key=lambda g: (g != my_group, g))  # моя группа — первой
            items.append((teacher, groups, mine))
        items.sort(key=lambda it: (not it[2], -len(it[1]), it[0]))
        hits.append(SubjectHit(subject, items))
    hits.sort(key=lambda h: (not h.mine, -h.size, h.subject))
    return hits


def _groups_text(groups: list[str]) -> str:
    shown = ", ".join(groups[:MAX_GROUPS_SHOWN])
    rest = len(groups) - MAX_GROUPS_SHOWN
    return f"{shown} +{rest}" if rest > 0 else shown


def _subject_footer(hits: list[SubjectHit], total: int, updated_at: int | None, lang: str) -> list[str]:
    lines = []
    if any(h.mine for h in hits):
        lines.append(t(lang, "subject.legend"))
    if total > len(hits):
        lines.append(t(lang, "subject.more", count=total - len(hits)))
    if updated_at:
        when = datetime.fromtimestamp(updated_at, TZ)
        day_month = t(lang, "schedule.day_month", day=when.day,
                      month=tl(lang, "schedule.months")[when.month - 1])
        lines.append(t(lang, "subject.updated", date=f"{day_month}, {when:%H:%M}"))
    lines.append(t(lang, "subject.open_hint"))
    return lines


def rich_subject_search(query: str, hits: list[SubjectHit], total: int, updated_at: int | None,
                        lang: str = DEFAULT_LANG) -> str:
    headers = [t(lang, "schedule.col_subject"), t(lang, "schedule.col_teacher"), t(lang, "schedule.col_groups")]
    rows = ["<tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers) + "</tr>"]
    for hit in hits:
        for i, (teacher, groups, mine) in enumerate(hit.teachers):
            cells = []
            if i == 0:
                cells.append(_td(escape(hit.subject), rowspan=len(hit.teachers), valign="middle"))
            cells.append(_td(("⭐ " if mine else "") + escape(teacher)))
            cells.append(_td(escape(_groups_text(groups))))
            rows.append("<tr>" + "".join(cells) + "</tr>")
    parts = [f"<h3>{escape(t(lang, 'subject.title', query=query))}</h3>",
             "<table bordered striped compact>" + "".join(rows) + "</table>"]
    parts += [f"<p>{escape(line)}</p>" for line in _subject_footer(hits, total, updated_at, lang)]
    return "".join(parts)


def format_subject_search(query: str, hits: list[SubjectHit], total: int, updated_at: int | None,
                          lang: str = DEFAULT_LANG) -> str:
    blocks = [f"<b>{escape(t(lang, 'subject.title', query=query))}</b>"]
    for hit in hits:
        lines = [f"<b>{escape(hit.subject)}</b>"]
        lines += [f"{'⭐' if mine else '👤'} {escape(teacher)} — {escape(_groups_text(groups))}"
                  for teacher, groups, mine in hit.teachers]
        blocks.append("\n".join(lines))
    blocks.append("\n".join(escape(line) for line in _subject_footer(hits, total, updated_at, lang)))
    return "\n\n".join(blocks)


# ---------- запасной вариант: обычный HTML-текст ----------

def _plain_lesson_lines(lesson: Lesson, kind: str, lang: str) -> list[str]:
    view = parse_lesson(lesson, kind)
    title = _subject_html(view, None, lang)
    if not view.parsed:
        return [title]
    details = []
    if view.who:
        details.append(f"{'👤' if kind == GROUP else '👥'} {escape(view.who)}")
    if view.room:
        details.append(f"🚪 {escape(view.room)}")
    return [title, "   ".join(details)] if details else [title]


def format_day(day: Day | None, d: date, kind: str, name: str, note: str | None = None,
               lang: str = DEFAULT_LANG) -> str:
    header = f"📅 <b>{escape(_with_week(human_date(d, lang), [day], lang))}</b>\n{_who_icon(kind)} {escape(name)}"
    if note:
        header += f"\n<i>{escape(note)}</i>"

    if day is None:
        return f"{header}\n\n{escape(t(lang, 'schedule.no_data'))}"
    if not day.lessons:
        return f"{header}\n\n{escape(t(lang, 'schedule.no_lessons'))}"

    blocks = []
    for (number, slot), lessons in groupby(day.lessons, key=lambda l: (l.number, l.time)):
        lines = [f"<b>{escape(t(lang, 'schedule.pair', n=number))}</b> · {_slot_time(slot)}"]
        for lesson in lessons:
            body = _plain_lesson_lines(lesson, kind, lang)
            if lesson.subgroup:
                body[0] = f"<u>{escape(subgroup_name(lesson.subgroup, lang))}</u>: {body[0]}"
            lines.extend(body)
        blocks.append("\n".join(lines))

    return header + "\n\n" + "\n\n".join(blocks)
