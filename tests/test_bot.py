"""Тесты парсера, форматирования и поиска на сохранённых страницах сайта (6 октября 2026)."""

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from formatting import format_day  # noqa: E402
from search import find, normalize  # noqa: E402
from vspu import GROUP, TEACHER, parse_csrf, parse_options, parse_schedule  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def group_schedule():
    return parse_schedule(load("group_IYa-AN_Ob251.html"), GROUP, "ИЯ-АН_Оb251")


@pytest.fixture(scope="module")
def teacher_schedule():
    return parse_schedule(load("teacher_Anisimova.html"), TEACHER, "Анисимова О.Н.")


def test_index_lists():
    groups, teachers = parse_options(load("index.html"))
    assert len(groups) == 306
    assert len(teachers) == 467
    assert "ИЯ-АН_Оb251" in groups
    assert "Анисимова О.Н." in teachers
    assert parse_csrf(load("index.html"))


def test_group_days_range(group_schedule):
    assert min(group_schedule.days) == date(2026, 10, 5)
    assert max(group_schedule.days) == date(2026, 11, 6)
    assert group_schedule.day(date(2026, 10, 5)).weekday == "ПН"


def test_group_common_and_subgroup_lessons(group_schedule):
    lessons = group_schedule.day(date(2026, 10, 5)).lessons
    assert [(l.number, l.subgroup) for l in lessons] == [(3, None), (5, "1 п/г"), (5, "2 п/г")]
    assert lessons[0].time == "12:40-14:15"
    assert lessons[0].text == "Общая педагогика(л.)-Востроилова Е.В. - 4-522"
    assert "Бугаков Н.И." in lessons[2].text


def test_teacher_lessons(teacher_schedule):
    assert teacher_schedule.day(date(2026, 10, 5)).lessons == []
    lessons = teacher_schedule.day(date(2026, 10, 6)).lessons
    assert [l.number for l in lessons] == [3, 4]
    assert lessons[0].text.endswith("[ИЯ-АН_Оb251]")


def test_format_group_day(group_schedule):
    d = date(2026, 10, 5)
    text = format_day(group_schedule.day(d), d, GROUP, group_schedule.name)
    assert "Понедельник, 5 октября" in text
    assert "<b>3 пара</b> · 12:40–14:15" in text
    assert "Общая педагогика <i>(лекция)</i>" in text
    assert "👤 Востроилова Е.В.   🚪 4-522" in text
    assert "<u>2 п/г</u>:" in text


def test_format_teacher_day(teacher_schedule):
    d = date(2026, 10, 6)
    text = format_day(teacher_schedule.day(d), d, TEACHER, teacher_schedule.name)
    assert "👥 ИЯ-АН_Оb251   🚪 4-509" in text


def test_format_empty_and_missing_day(group_schedule):
    sunday = date(2026, 10, 11)
    assert "Пар нет" in format_day(group_schedule.day(sunday), sunday, GROUP, "x")
    assert "пока нет" in format_day(None, date(2026, 12, 1), GROUP, "x")


def test_format_unusual_cells_kept_as_is():
    from vspu import Day, Lesson
    d = date(2026, 10, 6)
    day = Day(d, "ВТ", [
        Lesson("09:00-10:35", 1, "Иностранный язык()-Варенова О.С. - 4-430"),
        Lesson("10:45-12:20", 2, "Педпрактика (в ОО и ДО)(пр.)-Учитель 1 -"),
        Lesson("12:40-14:15", 3, "Что-то совсем <другое>"),
    ])
    text = format_day(day, d, GROUP, "x")
    assert "Иностранный язык\n👤 Варенова О.С.   🚪 4-430" in text
    assert "Педпрактика (в ОО и ДО) <i>(практика)</i>\n👤 Учитель 1" in text
    assert "Что-то совсем &lt;другое&gt;" in text


@pytest.mark.parametrize("query, expected", [
    ("ИЯ-АН_Оb251", ["ИЯ-АН_Оb251"]),
    ("ия-ан_ob251", ["ИЯ-АН_Оb251"]),  # латинская o вместо русской
    ("ан 251", ["ИЯ-АН_Оb251"]),
    ("АН об251", ["ИЯ-АН_Оb251"]),  # русская б вместо латинской b
])
def test_find_group(query, expected):
    groups, _ = parse_options(load("index.html"))
    assert find(query, groups) == expected


def test_find_teacher():
    _, teachers = parse_options(load("index.html"))
    assert find("анисимова", teachers) == ["Анисимова О.Н."]
    assert find("Анисимова О.Н.", teachers) == ["Анисимова О.Н."]
    assert find("абвгдеёжз", teachers) == []
    assert find("   ", teachers) == []


def test_normalize():
    assert normalize("ИЯ-АН_Оb251") == normalize("ия ан ob 251")


# ---------- rich-сообщения с таблицами ----------

from html.parser import HTMLParser  # noqa: E402

from formatting import rich_day, rich_week, week_dates  # noqa: E402

RICH_TAGS = {"h2", "h3", "h4", "p", "table", "tr", "th", "td", "b", "i", "u"}


class TagChecker(HTMLParser):
    """Проверяет, что теги сбалансированы и входят в список поддерживаемых Telegram."""

    def __init__(self):
        super().__init__()
        self.stack, self.unknown = [], set()

    def handle_starttag(self, tag, attrs):
        if tag not in RICH_TAGS:
            self.unknown.add(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"лишний </{tag}>"
        self.stack.pop()


def check_html(text: str) -> None:
    checker = TagChecker()
    checker.feed(text)
    assert not checker.stack, f"не закрыты: {checker.stack}"
    assert not checker.unknown, f"неподдерживаемые теги: {checker.unknown}"


def test_rich_group_day(group_schedule):
    d = date(2026, 10, 5)
    html = rich_day(group_schedule.day(d), d, GROUP, group_schedule.name)
    check_html(html)
    assert html.startswith("<h3>📅 Понедельник, 5 октября</h3><p>👥 ИЯ-АН_Оb251</p>")
    assert "<th>Преподаватель</th>" in html
    assert ("<tr><td align=\"center\" valign=\"middle\">3</td><td valign=\"middle\">12:40–14:15</td>"
            "<td>Общая педагогика <i>(лекция)</i></td><td>Востроилова Е.В.</td><td>4-522</td></tr>") in html
    # две подгруппы в одно время: № и время объединены на две строки
    assert '<td rowspan="2" align="center" valign="middle">5</td>' in html
    assert "<td><u>2 п/г</u> Элективные дисциплины" in html
    assert html.count("<tr>") == 1 + 3  # заголовок + 3 строки пар


def test_rich_teacher_day(teacher_schedule):
    d = date(2026, 10, 6)
    html = rich_day(teacher_schedule.day(d), d, TEACHER, teacher_schedule.name)
    check_html(html)
    assert "<th>Группы</th>" in html
    assert "<td>ИЯ-АН_Оb251</td><td>4-509</td>" in html


def test_rich_empty_and_unparsed():
    from vspu import Day, Lesson
    d = date(2026, 10, 6)
    assert "Пар нет" in rich_day(Day(d, "ВТ"), d, GROUP, "x")
    assert "пока нет" in rich_day(None, d, GROUP, "x")
    html = rich_day(Day(d, "ВТ", [Lesson("09:00-10:35", 1, "Что-то <другое>")]), d, GROUP, "x")
    check_html(html)
    assert '<td colspan="3">Что-то &lt;другое&gt;</td>' in html


def test_week_dates():
    assert week_dates(date(2026, 10, 6))[0] == date(2026, 10, 5)  # вторник → эта неделя
    assert week_dates(date(2026, 10, 5))[-1] == date(2026, 10, 11)
    assert week_dates(date(2026, 10, 11))[0] == date(2026, 10, 12)  # воскресенье → следующая


def test_rich_week(group_schedule):
    html = rich_week(group_schedule, week_dates(date(2026, 10, 6)))
    check_html(html)
    assert html.startswith("<h2>🗓 Неделя: 5–11 октября</h2>")
    assert html.count("<h4>") == 6  # пустое воскресенье пропущено
    assert "<h4>Суббота, 10 октября</h4>" in html
    assert "Воскресенье" not in html
    assert len(html) < 32768


def test_rich_week_month_boundary(group_schedule):
    html = rich_week(group_schedule, week_dates(date(2026, 11, 2)))
    assert html.startswith("<h2>🗓 Неделя: 2–8 ноября</h2>")
    assert "пока нет" in html  # 7–8 ноября на сайте ещё нет
    html = rich_week(group_schedule, week_dates(date(2026, 10, 28)))
    assert html.startswith("<h2>🗓 Неделя: 26 октября – 1 ноября</h2>")


# ---------- карточки чужого расписания ----------


def test_callback_data_fits_for_all_names():
    """Кнопки с самыми длинными реальными именами укладываются в 64 байта callback_data."""
    from handlers import PickCb, PinGroupCb, card_keyboard
    groups, teachers = parse_options(load("index.html"))
    for kind, names in ((GROUP, groups), (TEACHER, teachers)):
        longest = max(names, key=lambda n: len(n.encode()))
        card_keyboard(kind, longest, "d1", pinned=False)
        PickCb(kind=kind, name=longest, save=True).pack()
    PinGroupCb(name=max(groups, key=lambda n: len(n.encode()))).pack()


def test_card_keyboard_student_only():
    """В карточке преподавателя только переключатель периода, без «сделать основным»;
    в карточке чужой группы — ещё «сделать моей группой», если она не своя."""
    from handlers import card_keyboard
    teacher = card_keyboard(TEACHER, "Анисимова О.Н.", "d0", pinned=False)
    assert [[b.text for b in row] for row in teacher.inline_keyboard] == [
        ["Сейчас", "• Сегодня", "Завтра"], ["Неделя", "Через неделю"]]
    group = card_keyboard(GROUP, "ИЯ-АН_Оb251", "w1", pinned=False)
    assert [b.text for b in group.inline_keyboard[1]] == ["Неделя", "• Через неделю"]
    assert [b.text for b in group.inline_keyboard[2]] == ["📌 Сделать моей группой"]
    assert len(card_keyboard(GROUP, "ИЯ-АН_Оb251", "w", pinned=True).inline_keyboard) == 2
    english = card_keyboard(TEACHER, "Анисимова О.Н.", "now", pinned=False, lang="en")
    assert [b.text for b in english.inline_keyboard[0]] == ["• Now", "Today", "Tomorrow"]


def test_storage_set_and_delete(tmp_path):
    from storage import Storage
    st = Storage(tmp_path / "bot.db")
    st.set(1, GROUP, "ИЯ-АН_Оb251")
    st.set(2, GROUP, "ИЯ-АН_Оb241")
    st.set(1, GROUP, "ИЯ-АФ_Оb251")  # повторный выбор перезаписывает
    assert st.get(1) == (GROUP, "ИЯ-АФ_Оb251")
    st.delete(1)
    assert st.get(1) is None
    assert st.get(2) == (GROUP, "ИЯ-АН_Оb241")  # других пользователей не трогаем
    st.delete(1)  # повторный /reset не падает
    st.close()


# ---------- админ: статистика и рассылка ----------

def test_stats_and_audience(tmp_path):
    from storage import DAY, Storage
    st = Storage(tmp_path / "bot.db")
    now = 1_800_000_000
    st.touch(1, now - 40 * DAY)  # давно пришёл и пропал
    st.touch(2, now - 3 * DAY)
    st.touch(3, now - 3600)
    st.touch(4, now - 3600)
    st.set(1, GROUP, "ИЯ-АН_Оb251")
    st.set(2, GROUP, "ИЯ-АН_Оb251")
    st.set(3, GROUP, "Г-ИО_Оb261")
    st.set_blocked(4, True)

    s = st.stats(now)
    assert (s.total, s.blocked, s.with_group) == (4, 1, 3)
    assert s.new == {1: 2, 7: 3, 30: 3}
    assert s.active == {1: 2, 7: 3, 30: 3}
    assert s.top_groups == [("ИЯ-АН_Оb251", 2), ("Г-ИО_Оb261", 1)]
    assert s.groups_by_faculty == {"ИЯ": 2, "Г": 1}
    assert st.audience() == [1, 2, 3]  # заблокировавший не получает рассылку

    st.touch(4, now)  # снова написал — значит, разблокировал
    assert st.audience() == [1, 2, 3, 4]
    st.delete(2)  # /reset стирает и активность
    assert st.stats(now).total == 3
    st.close()


def test_old_users_get_into_audience(tmp_path):
    """Пользователи из базы до появления статистики попадают в рассылку, но не в «новые»."""
    import sqlite3
    from storage import Storage
    path = tmp_path / "bot.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL)")
    db.execute("INSERT INTO users VALUES (10, 'group', 'ИЯ-АН_Оb251')")
    db.commit()
    db.close()
    st = Storage(path)
    assert st.audience() == [10]
    assert st.stats().new[30] == 0
    st.close()


def test_to_rich_markdown():
    from admin import to_rich_markdown
    assert to_rich_markdown("/broadcast") == ""
    assert to_rich_markdown("/broadcast@vspu_bot   ") == ""
    # одиночные переносы становятся жёсткими, пустая строка — граница абзаца
    assert to_rich_markdown("/broadcast **Важно**\nвторая\n\nновый абзац") == \
        "**Важно**  \nвторая\n\nновый абзац"
    # форматирование Telegram (html_text) сохраняется, «>» снова работает как цитата, «<» — нет
    assert to_rich_markdown("/broadcast <b>Жирно</b>\n&gt; цитата a &lt; b") == \
        "<b>Жирно</b>  \n> цитата a &lt; b"
    # внутри блоков кода строки не трогаем
    assert to_rich_markdown("/broadcast текст\n```\nx = 1\ny = 2\n```") == \
        "текст\n```\nx = 1\ny = 2\n```"
    assert to_rich_markdown("/broadcast <pre>x = 1\ny = 2</pre>\nпосле") == \
        "<pre>x = 1\ny = 2</pre>\nпосле"  # после блока кода строка и так начинается заново


def test_parse_admin_ids():
    from admin import parse_admin_ids
    assert parse_admin_ids(None) == frozenset()
    assert parse_admin_ids("123, 456,789") == {123, 456, 789}


def test_format_stats(tmp_path):
    from admin import format_stats
    from storage import Storage
    st = Storage(tmp_path / "bot.db")
    st.touch(1)
    st.set(1, GROUP, "ИЯ-АН_Оb251")
    text = format_stats(st)
    assert "Пользователей: <b>1</b> (выбрали группу: 1)" in text
    assert "Новые: сутки 1 · неделя 1 · месяц 1" in text
    assert "1. ИЯ-АН_Оb251 — 1" in text
    st.close()


def test_profile_saved_and_updated(tmp_path):
    from storage import Storage
    st = Storage(tmp_path / "bot.db")
    st.touch(1, username="ivanov", first_name="Иван", last_name="Иванов")
    assert st.profile(1) == ("ivanov", "Иван", "Иванов")
    # сменил имя и убрал username в Telegram — в базе актуальные данные
    st.touch(1, username=None, first_name="Ваня", last_name=None)
    assert st.profile(1) == (None, "Ваня", None)
    assert st.profile(2) is None
    st.delete(1)  # /reset стирает и профиль
    assert st.profile(1) is None
    st.close()


def test_migration_from_previous_version(tmp_path):
    """База прошлой версии (activity без колонок профиля) обновляется без потери данных."""
    import sqlite3
    from storage import Storage
    path = tmp_path / "bot.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE activity (user_id INTEGER PRIMARY KEY, first_seen INTEGER, last_seen INTEGER,
                               blocked INTEGER NOT NULL DEFAULT 0);
        INSERT INTO users VALUES (7, 'group', 'ИЯ-АН_Оb251');
        INSERT INTO activity VALUES (7, 1000, 2000, 1);
    """)
    db.commit()
    db.close()

    st = Storage(path)
    assert st.get(7) == (GROUP, "ИЯ-АН_Оb251")
    assert st.profile(7) == (None, None, None)
    assert st.audience() == []  # blocked сохранился
    st.touch(7, 3000, username="petrov", first_name="Пётр", last_name="Петров")
    assert st.profile(7) == ("petrov", "Пётр", "Петров")
    assert st.audience() == [7]
    st.close()
    Storage(path).close()  # повторный запуск не пытается добавить колонки ещё раз


# ---------- «Через неделю» ----------

def test_next_week_dates():
    assert week_dates(date(2026, 10, 6), weeks_ahead=1)[0] == date(2026, 10, 12)
    assert week_dates(date(2026, 10, 6), weeks_ahead=1)[-1] == date(2026, 10, 18)
    # в воскресенье «Неделя» — уже следующая, а «Через неделю» — та, что после неё
    assert week_dates(date(2026, 10, 11))[0] == date(2026, 10, 12)
    assert week_dates(date(2026, 10, 11), weeks_ahead=1)[0] == date(2026, 10, 19)


def test_rich_next_week(group_schedule):
    html = rich_week(group_schedule, week_dates(date(2026, 10, 6), weeks_ahead=1), "Следующая неделя")
    check_html(html)
    assert html.startswith("<h2>🗓 Следующая неделя: 12–18 октября</h2>")
    assert "<h4>Понедельник, 12 октября</h4>" in html


# ---------- копии расписаний ----------

import asyncio  # noqa: E402

from vspu import Schedule, SiteError  # noqa: E402


def test_schedule_json_roundtrip(group_schedule):
    restored = Schedule.from_json(group_schedule.to_json())
    assert restored.kind == group_schedule.kind and restored.name == group_schedule.name
    assert restored.days == group_schedule.days


def test_saved_copy_note():
    from formatting import saved_copy_note
    # 2026-10-08 11:30 UTC = 14:30 по Москве
    assert saved_copy_note(1791459000) == (
        "⚠️ Сайт расписания сейчас недоступен. Это сохранённая копия "
        "от 8 октября, 14:30 — возможны изменения.")


def test_storage_schedules(tmp_path, group_schedule):
    from storage import Storage
    st = Storage(tmp_path / "bot.db")
    group_schedule.fetched_at = 1000
    st.save_schedule(group_schedule)
    saved, fetched_at = st.load_schedule(GROUP, "ИЯ-АН_Оb251")
    assert fetched_at == 1000 and saved.days == group_schedule.days
    assert st.load_schedule(GROUP, "нет такой") is None

    st.set(1, GROUP, "ИЯ-АН_Оb251")
    st.set(2, GROUP, "ИЯ-АН_Оb251")
    assert st.used_groups() == ["ИЯ-АН_Оb251"] and st.group_in_use("ИЯ-АН_Оb251")
    assert st.prune_schedules() == 0  # группа используется — копию не трогаем
    st.set(1, GROUP, "Г-ИО_Оb261")
    st.delete(2)
    assert st.prune_schedules() == 1  # ИЯ-АН_Оb251 больше ни у кого не выбрана
    assert st.saved_schedules_count() == 0
    st.close()


class FakeClient:
    """Вместо сайта: отдаёт расписание из фикстуры или падает, как недоступный сайт."""

    def __init__(self, schedule, down=False):
        self.schedule_obj, self.down, self.calls = schedule, down, []

    def cached(self, kind, name):
        return None

    async def schedule(self, kind, name):
        self.calls.append((kind, name))
        if self.down:
            raise SiteError("сайт недоступен: timeout")
        return Schedule(kind=kind, name=name, days=self.schedule_obj.days, fetched_at=5000)


@pytest.fixture
def store(tmp_path):
    import schedules
    from storage import Storage
    schedules._saved.clear()
    schedules._site_down_until = 0.0
    st = Storage(tmp_path / "bot.db")
    yield st
    st.close()


def test_get_schedule_saves_only_used_groups(store, group_schedule):
    from schedules import get_schedule
    client = FakeClient(group_schedule)
    store.set(1, GROUP, "ИЯ-АН_Оb251")
    schedule, saved_at = asyncio.run(get_schedule(client, store, GROUP, "ИЯ-АН_Оb251"))
    assert saved_at is None and schedule.name == "ИЯ-АН_Оb251"
    assert store.load_schedule(GROUP, "ИЯ-АН_Оb251")[1] == 5000
    # чужая группа и преподаватель — не сохраняются
    asyncio.run(get_schedule(client, store, GROUP, "Г-ИО_Оb261"))
    asyncio.run(get_schedule(client, store, TEACHER, "Анисимова О.Н."))
    assert store.saved_schedules_count() == 1


def test_get_schedule_falls_back_to_copy(store, group_schedule):
    from schedules import get_schedule
    store.set(1, GROUP, "ИЯ-АН_Оb251")
    asyncio.run(get_schedule(FakeClient(group_schedule), store, GROUP, "ИЯ-АН_Оb251"))

    down = FakeClient(group_schedule, down=True)
    schedule, saved_at = asyncio.run(get_schedule(down, store, GROUP, "ИЯ-АН_Оb251"))
    assert saved_at == 5000
    assert schedule.day(date(2026, 10, 6)).lessons  # копия полноценная
    with pytest.raises(SiteError):  # копии нет — сайт недоступен честно
        asyncio.run(get_schedule(down, store, TEACHER, "Анисимова О.Н."))


def test_render_marks_saved_copy(group_schedule):
    from handlers import render
    rich_html, fallback = render(group_schedule, "d0", saved_at=1791459000)
    assert "Это сохранённая копия от 8 октября, 14:30" in rich_html
    assert "Это сохранённая копия" in fallback[0]
    rich_html, _ = render(group_schedule, "w1")
    assert "сохранённая копия" not in rich_html and "Следующая неделя" in rich_html


def test_refresh_all(store, group_schedule, monkeypatch):
    import schedules
    monkeypatch.setattr(schedules, "REFRESH_PAUSE", 0)
    store.set(1, GROUP, "ИЯ-АН_Оb251")
    store.set(2, GROUP, "Г-ИО_Оb261")
    old = Schedule(kind=GROUP, name="ЕГ-Б_Оb241", days={}, fetched_at=1)
    store.save_schedule(old)  # копия группы, которую никто не выбрал

    client = FakeClient(group_schedule)
    assert asyncio.run(schedules.refresh_all(client, store)) == (2, 0)
    assert store.load_schedule(GROUP, "ЕГ-Б_Оb241") is None
    assert store.saved_schedules_count() == 2

    # сайт лежит: после трёх ошибок подряд обновление прерывается
    for i in range(5):
        store.set(10 + i, GROUP, f"Г-ИВ_Оb2{i}1")
    down = FakeClient(group_schedule, down=True)
    assert asyncio.run(schedules.refresh_all(down, store)) == (0, 3)
    assert len(down.calls) == 3
    assert store.saved_schedules_count() == 2  # старые копии на месте


def test_site_down_pause(store, group_schedule):
    """После неудачи бот не ждёт таймаут сайта снова, а сразу отдаёт копию."""
    from schedules import get_schedule
    store.set(1, GROUP, "ИЯ-АН_Оb251")
    asyncio.run(get_schedule(FakeClient(group_schedule), store, GROUP, "ИЯ-АН_Оb251"))

    down = FakeClient(group_schedule, down=True)
    asyncio.run(get_schedule(down, store, GROUP, "ИЯ-АН_Оb251"))
    asyncio.run(get_schedule(down, store, GROUP, "ИЯ-АН_Оb251"))
    assert len(down.calls) == 1  # второй раз к сайту не ходили
    # для расписания без копии всё же пробуем сайт — показать больше нечего
    with pytest.raises(SiteError):
        asyncio.run(get_schedule(down, store, TEACHER, "Анисимова О.Н."))
    assert len(down.calls) == 2

    import schedules
    schedules._site_down_until = 0.0  # пауза прошла
    up = FakeClient(group_schedule)
    assert asyncio.run(get_schedule(up, store, GROUP, "ИЯ-АН_Оb251"))[1] is None


# ---------- языки ----------

import string  # noqa: E402

from i18n import LANGS, STRINGS, guess_lang, t  # noqa: E402


def _placeholders(value):
    if isinstance(value, list):
        return len(value)
    return {field for _, field, _, _ in string.Formatter().parse(value) if field}


def test_locales_complete():
    """Во всех языках те же ключи и те же подстановки, что в русском."""
    ref = STRINGS["ru"]
    assert len(ref["schedule.weekdays"]) == 7 and len(ref["schedule.months"]) == 12
    for lang in LANGS:
        strings = STRINGS[lang]
        assert set(strings) == set(ref), lang
        for key, value in ref.items():
            assert _placeholders(strings[key]) == _placeholders(value), (lang, key)


def test_guess_lang():
    assert guess_lang("pt-BR") == "pt"
    assert guess_lang("en") == "en"
    assert guess_lang("de") == "ru"
    assert guess_lang(None) == "ru"


def test_rich_day_english_and_portuguese(group_schedule):
    d = date(2026, 10, 5)
    en = rich_day(group_schedule.day(d), d, GROUP, group_schedule.name, lang="en")
    check_html(en)
    assert "<h3>📅 Monday, 5 October</h3>" in en
    assert "<th>#</th><th>Time</th><th>Subject</th><th>Teacher</th><th>Room</th>" in en
    assert "Общая педагогика <i>(lecture)</i>" in en  # название предмета — как на сайте
    assert "<u>Subgroup 2</u>" in en

    pt = rich_day(group_schedule.day(d), d, GROUP, group_schedule.name, lang="pt")
    assert "<h3>📅 Segunda-feira, 5 de outubro</h3>" in pt
    assert "<i>(aula teórica)</i>" in pt and "<u>Subgrupo 1</u>" in pt
    assert "Sem aulas" in rich_day(group_schedule.day(date(2026, 10, 11)), date(2026, 10, 11),
                                   GROUP, "x", lang="pt")


def test_rich_week_titles_translated(group_schedule):
    dates = week_dates(date(2026, 10, 28))
    assert rich_week(group_schedule, dates, lang="en").startswith(
        "<h2>🗓 This week: 26 October – 1 November</h2>")
    assert rich_week(group_schedule, dates, lang="pt").startswith(
        "<h2>🗓 Esta semana: 26 de outubro – 1 de novembro</h2>")


def test_saved_copy_note_translated():
    from formatting import saved_copy_note
    assert "saved copy from 8 October, 14:30" in saved_copy_note(1791459000, "en")
    assert "cópia guardada de 8 de outubro, 14:30" in saved_copy_note(1791459000, "pt")


def test_storage_lang(tmp_path):
    import sqlite3
    from storage import Storage
    path = tmp_path / "bot.db"
    # база прошлой версии: activity без колонки lang
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE activity (user_id INTEGER PRIMARY KEY, first_seen INTEGER, last_seen INTEGER,
                               blocked INTEGER NOT NULL DEFAULT 0);
        INSERT INTO users VALUES (7, 'group', 'ИЯ-АН_Оb251');
        INSERT INTO activity VALUES (7, 1000, 2000, 0);
    """)
    db.commit()
    db.close()
    st = Storage(path)
    assert st.get_lang(7) == "ru"  # старые пользователи остаются на русском
    st.touch(8)
    assert st.get_lang(8) is None  # новый — ещё не выбрал
    st.set_lang(8, "pt")
    st.touch(8)  # обращение не сбрасывает язык
    assert st.get_lang(8) == "pt"
    assert st.stats().langs == {"pt": 1, "ru": 1}
    st.delete(8)
    assert st.get_lang(8) is None
    st.close()


# ---------- «Сейчас» ----------

from datetime import datetime  # noqa: E402

from formatting import TZ, duration, now_status, rich_now  # noqa: E402


def at(day, hh, mm):
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


def test_duration():
    from datetime import timedelta
    assert duration(timedelta(minutes=5)) == "5 мин"
    assert duration(timedelta(minutes=60)) == "1 ч"
    assert duration(timedelta(minutes=65, seconds=10), "en") == "1 h 6 min"  # округляем вверх
    assert duration(timedelta(seconds=20), "pt") == "1 min"


def test_now_status(group_schedule):
    # вторник 6 октября: пары 1–4, 09:00–16:15, третья — по подгруппам
    status, day = now_status(group_schedule, at(6, 8, 0))
    assert status == "Пары начнутся в 09:00, через 1 ч." and day.lessons[0].number == 1

    status, day = now_status(group_schedule, at(6, 9, 30))
    assert status == "Идёт 1 пара · 09:00–10:35, до конца 1 ч 5 мин."

    status, day = now_status(group_schedule, at(6, 10, 40))
    assert status == "Перерыв. Следующая — 2 пара в 10:45, через 5 мин."

    status, day = now_status(group_schedule, at(6, 13, 0))
    assert status.startswith("Идёт 3 пара") and [l.subgroup for l in day.lessons] == ["1 п/г", "2 п/г"]

    status, day = now_status(group_schedule, at(6, 17, 0))
    assert status == "Сегодня пар больше нет. Ближайшая пара — Среда, 7 октября, в 09:00."
    assert day.date == date(2026, 10, 7)

    # суббота без пар → ближайшая в понедельник
    status, day = now_status(group_schedule, at(10, 12, 0), "en")
    assert status == "No classes today. Next class: Monday, 12 October, at 12:40."

    # за пределами расписания с сайта
    status, day = now_status(group_schedule, datetime(2026, 11, 6, 23, 0, tzinfo=TZ), "pt")
    assert status.endswith("Não há aulas nas próximas duas semanas.") and day is None


def test_rich_now(group_schedule):
    html = rich_now(group_schedule, at(6, 13, 0), lang="en")
    check_html(html)
    assert html.startswith("<h3>🕐 Now, 13:00</h3><p>👥 ИЯ-АН_Оb251</p>")
    assert "<p>Class 3 is in progress · 12:40–14:15, 1 h 15 min left.</p>" in html
    assert html.count("<tr>") == 1 + 2  # заголовок + две подгруппы


def test_render_now_period(group_schedule):
    from handlers import render
    rich_html, fallback = render(group_schedule, "now", lang="pt", at=at(6, 10, 40))
    assert "Intervalo. A seguir: Aula 2 às 10:45, daqui a 5 min." in rich_html
    assert "Intervalo" in fallback[0]


# ---------- поиск латиницей ----------

def test_find_latin_transliteration():
    from search import transliterate
    groups, teachers = parse_options(load("index.html"))
    assert transliterate("Щукина Ё.Ю.") == "shchukinaeyu"
    assert find("Anisimova", teachers) == ["Анисимова О.Н."]
    assert find("anisimova o.n.", teachers) == ["Анисимова О.Н."]
    assert "Кузнецов И.М." in find("Kuznetsov", teachers)
    assert find("AN 251", groups) == ["ИЯ-АН_Оb251"]
    assert find("iya an ob251", groups) == ["ИЯ-АН_Оb251"]
    assert find("qwerty", teachers) == []
    assert find("Khrenova", teachers) == find("Hrenova", teachers) == ["Хренова Н.Ф."]
    assert find("an", groups)[:1] == ["ИЯ-АН_Оb221"]  # без ложных «РЯЯНР»
    assert "Г-РЯЯНР_Оa251" not in find("an", groups)


# ---------- поиск преподавателя по предмету ----------

class FakeSite:
    """Сайт для сборки индекса: у всех групп расписание из фикстуры."""

    def __init__(self, schedule, groups, teachers, fail=()):
        self.schedule_obj, self._groups, self._teachers, self.fail = schedule, groups, teachers, set(fail)
        self.calls = []

    def cached(self, kind, name):
        return None

    async def groups(self):
        return self._groups

    async def teachers(self):
        return self._teachers

    async def schedule(self, kind, name, cache=True):
        self.calls.append((name, cache))
        if name in self.fail or "*" in self.fail:
            raise SiteError("сайт недоступен")
        return Schedule(kind=kind, name=name, days=self.schedule_obj.days, fetched_at=7000)


def test_storage_subject_index(store):
    assert store.subject_names() == [] and store.subject_index_updated_at() is None
    store.replace_subject_index({("Философия", "Шипилов А.В.", "ИЯ-АН_Оb251"),
                                 ("Философия", "Шипилов А.В.", "Г-ИО_Оb261")}, now=1000)
    store.replace_subject_index({("История России", "Фурсов В.Н.", "Г-ИВ_Оb241")}, now=2000)
    assert store.subject_names() == ["История России"]  # индекс заменяется целиком
    assert store.subject_rows(["История России"]) == [("История России", "Фурсов В.Н.", "Г-ИВ_Оb241")]
    assert store.subject_index_updated_at() == 2000


def test_rebuild_subject_index(store, group_schedule):
    import schedules
    _, teachers = parse_options(load("index.html"))
    store.set(1, GROUP, "ИЯ-АН_Оb251")
    site = FakeSite(group_schedule, ["ИЯ-АН_Оb251", "Г-ИО_Оb261"], teachers)
    count = asyncio.run(schedules.rebuild_subject_index(site, store, pause=0))
    assert count and all(cache is False for _, cache in site.calls)  # не забиваем кэш в памяти
    rows = store.subject_rows(["Общая педагогика"])
    assert rows == [("Общая педагогика", "Востроилова Е.В.", "Г-ИО_Оb261"),
                    ("Общая педагогика", "Востроилова Е.В.", "ИЯ-АН_Оb251")]
    assert store.load_schedule(GROUP, "ИЯ-АН_Оb251")[1] == 7000  # заодно обновилась копия своей группы
    assert store.load_schedule(GROUP, "Г-ИО_Оb261") is None  # чужую не храним


def test_rebuild_subject_index_keeps_old_when_site_fails(store, group_schedule):
    import schedules
    _, teachers = parse_options(load("index.html"))
    store.replace_subject_index({("Философия", "Шипилов А.В.", "ИЯ-АН_Оb251")}, now=1000)

    groups = [f"Г-{i}" for i in range(8)]
    down = FakeSite(group_schedule, groups, teachers, fail={"*"})
    assert asyncio.run(schedules.rebuild_subject_index(down, store, pause=0)) is None
    assert len(down.calls) == 5  # пять ошибок подряд — бросили обход
    flaky = FakeSite(group_schedule, groups, teachers, fail={"Г-1", "Г-3", "Г-5"})  # 3 из 8 > четверти
    assert asyncio.run(schedules.rebuild_subject_index(flaky, store, pause=0)) is None
    assert store.subject_names() == ["Философия"] and store.subject_index_updated_at() == 1000


def test_subject_hits_order():
    from formatting import subject_hits
    rows = [
        ("Философия", "Шипилов А.В.", "Г-ИО_Оb261"),
        ("Философия", "Шипилов А.В.", "ФМ-МИ_Оb261"),
        ("Философия", "Петров П.П.", "ИЯ-АН_Оb251"),
        ("Философия науки", "Сидоров С.С.", "Г-ИВ_Оb241"),
        ("Философия науки", "Сидоров С.С.", "Г-ИО_Оb221"),
        ("Философия науки", "Сидоров С.С.", "Г-РЛ_Оb231"),
        ("Философия науки", "Сидоров С.С.", "Г-РЛ_Оb232"),
    ]
    hits = subject_hits(rows, my_group="ИЯ-АН_Оb251")
    # предмет моей группы — первым, хотя у «Философии науки» групп больше
    assert [h.subject for h in hits] == ["Философия", "Философия науки"]
    assert hits[0].teachers[0] == ("Петров П.П.", ["ИЯ-АН_Оb251"], True)
    assert hits[0].teachers[1] == ("Шипилов А.В.", ["Г-ИО_Оb261", "ФМ-МИ_Оb261"], False)
    assert [h.subject for h in subject_hits(rows, None)] == ["Философия науки", "Философия"]


def test_rich_subject_search():
    from formatting import format_subject_search, rich_subject_search, subject_hits
    rows = [("Философия", "Шипилов А.В.", f"Г-{i}") for i in range(5)] + \
           [("Философия", "Петров П.П.", "ИЯ-АН_Оb251")]
    hits = subject_hits(rows, "ИЯ-АН_Оb251")
    html = rich_subject_search("филос", hits, total=3, updated_at=1791459000, lang="en")
    check_html(html)
    assert html.startswith("<h3>📚 Subject: «филос»</h3>")
    assert '<td rowspan="2" valign="middle">Философия</td><td>⭐ Петров П.П.</td>' in html
    assert "<td>Г-0, Г-1, Г-2 +2</td>" in html
    assert "<p>⭐ — teaches in your group</p>" in html
    assert "<p>And 2 more — narrow down your search to see them.</p>" in html
    assert "updated 8 October, 14:30" in html
    plain = format_subject_search("филос", hits, 1, None, "ru")
    assert "⭐ Петров П.П. — ИЯ-АН_Оb251" in plain and "Нажми на преподавателя" in plain


# ---------- формат сайта с октября 2026: тип недели под датой ----------

@pytest.fixture(scope="module")
def new_group_schedule():
    return parse_schedule(load("group_2026-10_week_types.html"), GROUP, "ИЯ-АН_Оb251")


def test_parse_week_types(new_group_schedule):
    s = new_group_schedule
    assert len(s.days) == 62 and min(s.days) == date(2026, 10, 12) and max(s.days) == date(2026, 12, 12)
    assert s.day(date(2026, 10, 12)).week == "числитель"
    assert s.day(date(2026, 10, 18)).week == "числитель"
    assert s.day(date(2026, 10, 19)).week == "знаменатель"
    assert s.day(date(2026, 10, 12)).weekday == "ПН"
    assert sum(len(d.lessons) for d in s.days.values()) == 185
    assert s.day(date(2026, 10, 12)).lessons[0].text.startswith("Возрастная и педагогическая психология")
    teacher = parse_schedule(load("teacher_2026-10_week_types.html"), TEACHER, "Анисимова О.Н.")
    assert len(teacher.days) == 62 and teacher.day(date(2026, 10, 26)).week == "числитель"


def test_old_format_still_parses(group_schedule):
    assert group_schedule.day(date(2026, 10, 5)).week == ""  # в старом формате типа недели нет


def test_layout_change_is_an_error_not_empty_schedule():
    """Если сайт снова поменяет вёрстку дат, бот не должен молча показывать «пар нет»."""
    from vspu import SiteError
    broken = load("group_2026-10_week_types.html").replace("-10-2026", ".10.2026").replace("-11-2026", ".11.2026") \
        .replace("-12-2026", ".12.2026")
    with pytest.raises(SiteError):
        parse_schedule(broken, GROUP, "x")


def test_week_type_in_headers(new_group_schedule):
    d = date(2026, 10, 19)
    html = rich_day(new_group_schedule.day(d), d, GROUP, "ИЯ-АН_Оb251")
    assert html.startswith("<h3>📅 Понедельник, 19 октября · знаменатель</h3>")
    en = rich_day(new_group_schedule.day(d), d, GROUP, "ИЯ-АН_Оb251", lang="en")
    assert en.startswith("<h3>📅 Monday, 19 October · denominator week</h3>")
    week = rich_week(new_group_schedule, week_dates(date(2026, 10, 14)), lang="pt")
    assert week.startswith("<h2>🗓 Esta semana: 12–18 de outubro · semana numerador</h2>")
    plain = format_day(new_group_schedule.day(d), d, GROUP, "x")
    assert plain.startswith("📅 <b>Понедельник, 19 октября · знаменатель</b>")


def test_week_type_survives_saved_copy(new_group_schedule):
    import json
    restored = Schedule.from_json(new_group_schedule.to_json())
    assert restored.day(date(2026, 10, 19)).week == "знаменатель"
    # копия, сохранённая до появления типа недели, тоже читается
    old = json.loads(new_group_schedule.to_json())
    for day in old["days"]:
        del day["week"]
    assert Schedule.from_json(json.dumps(old)).day(date(2026, 10, 19)).week == ""


def test_rebuild_subject_index_keeps_old_when_no_subjects(store):
    """Во всех группах ни одной пары (сайт что-то поменял) — хороший индекс не затираем."""
    import schedules
    from vspu import Day
    store.replace_subject_index({("Философия", "Шипилов А.В.", "ИЯ-АН_Оb251")}, now=1000)
    empty = Schedule(GROUP, "x", {date(2026, 10, 12): Day(date(2026, 10, 12), "ПН")})
    site = FakeSite(empty, ["ИЯ-АН_Оb251", "Г-ИО_Оb261"], ["Шипилов А.В."])
    assert asyncio.run(schedules.rebuild_subject_index(site, store, pause=0)) is None
    assert store.subject_names() == ["Философия"]


def test_main_keyboard_always_visible():
    """Клавиатура не сворачивается в значок в Telegram на компьютере."""
    from handlers import main_keyboard
    for lang in LANGS:
        kb = main_keyboard(lang)
        assert kb.is_persistent is True and kb.resize_keyboard is True
        assert [len(row) for row in kb.keyboard] == [3, 2]
