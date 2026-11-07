"""SQLite: выбранная группа, активность пользователей (для /stats и рассылки)
и копии расписаний их групп (на случай, если сайт недоступен)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from vspu import Schedule

DAY = 24 * 60 * 60


@dataclass
class Stats:
    total: int  # все, кто писал боту (минус удалившиеся через /reset)
    blocked: int  # заблокировали бота
    with_group: int
    new: dict[int, int]  # дней → сколько новых за этот период
    active: dict[int, int]  # дней → сколько заходили за этот период
    top_groups: list[tuple[str, int]] = field(default_factory=list)
    groups_by_faculty: dict[str, int] = field(default_factory=dict)
    langs: dict[str, int] = field(default_factory=dict)  # язык → сколько пользователей


class Storage:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                kind    TEXT NOT NULL,   -- 'group' (раньше бывал и 'teacher')
                name    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                user_id    INTEGER PRIMARY KEY,
                first_seen INTEGER,      -- unix-время; NULL у тех, кто пришёл до появления статистики
                last_seen  INTEGER,
                blocked    INTEGER NOT NULL DEFAULT 0
            );
            -- пользователи из версий без статистики тоже должны получать рассылку
            INSERT OR IGNORE INTO activity (user_id) SELECT user_id FROM users;
            -- копии расписаний на случай, если сайт недоступен; только для групп пользователей
            CREATE TABLE IF NOT EXISTS schedules (
                kind       TEXT NOT NULL,
                name       TEXT NOT NULL,
                data       TEXT NOT NULL,     -- Schedule.to_json()
                fetched_at INTEGER NOT NULL,  -- unix-время загрузки с сайта
                PRIMARY KEY (kind, name)
            );
            -- кто какой предмет ведёт в какой группе — для /subject;
            -- собирается из расписаний всех групп, раз в сутки целиком заново
            CREATE TABLE IF NOT EXISTS subject_index (
                subject TEXT NOT NULL,
                teacher TEXT NOT NULL,
                grp     TEXT NOT NULL,
                PRIMARY KEY (subject, teacher, grp)
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Колонки, появившиеся позже: в уже существующей базе добавляем их на месте.
        # Значения — как в профиле Telegram при последнем обращении (NULL, если не указано).
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(activity)")}
        for column in ("username", "first_name", "last_name"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE activity ADD COLUMN {column} TEXT")
        if "lang" not in columns:
            # язык интерфейса: ru / en / pt; NULL — ещё не выбран
            self._db.execute("ALTER TABLE activity ADD COLUMN lang TEXT")
            # кто пользовался ботом до появления языков, читал его по-русски —
            # не меняем им интерфейс внезапно (даже если Telegram у них на английском)
            self._db.execute("UPDATE activity SET lang = 'ru'")
        self._db.commit()

    # ---------- выбор группы ----------

    def get(self, user_id: int) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT kind, name FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set(self, user_id: int, kind: str, name: str) -> None:
        self._db.execute(
            "INSERT INTO users (user_id, kind, name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET kind = excluded.kind, name = excluded.name",
            (user_id, kind, name),
        )
        self._db.commit()

    def delete(self, user_id: int) -> None:
        """Стирает всё, что известно о пользователе (/reset)."""
        self._db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        self._db.execute("DELETE FROM activity WHERE user_id = ?", (user_id,))
        self._db.commit()

    # ---------- активность ----------

    def touch(self, user_id: int, now: int | None = None, *, username: str | None = None,
              first_name: str | None = None, last_name: str | None = None) -> None:
        """Отмечает, что пользователь что-то написал или нажал, и обновляет его
        username, имя и фамилию. Раз пишет — значит, бот у него не заблокирован."""
        now = int(now if now is not None else time.time())
        self._db.execute(
            "INSERT INTO activity (user_id, first_seen, last_seen, blocked, username, first_name, last_name) "
            "VALUES (?, ?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen, blocked = 0, "
            "username = excluded.username, first_name = excluded.first_name, "
            "last_name = excluded.last_name",
            (user_id, now, now, username, first_name, last_name),
        )
        self._db.commit()

    def profile(self, user_id: int) -> tuple[str | None, str | None, str | None] | None:
        """(username, имя, фамилия) или None, если пользователя нет в базе."""
        row = self._db.execute(
            "SELECT username, first_name, last_name FROM activity WHERE user_id = ?", (user_id,)
        ).fetchone()
        return tuple(row) if row else None

    def get_lang(self, user_id: int) -> str | None:
        row = self._db.execute("SELECT lang FROM activity WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_lang(self, user_id: int, lang: str) -> None:
        self._db.execute(
            "INSERT INTO activity (user_id, lang) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang",
            (user_id, lang),
        )
        self._db.commit()

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        self._db.execute(
            "INSERT INTO activity (user_id, blocked) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET blocked = excluded.blocked",
            (user_id, int(blocked)),
        )
        self._db.commit()

    def audience(self) -> list[int]:
        """Кому слать рассылку: все, кто не заблокировал бота."""
        return [r[0] for r in self._db.execute(
            "SELECT user_id FROM activity WHERE blocked = 0 ORDER BY user_id")]

    # ---------- копии расписаний ----------

    def used_groups(self) -> list[str]:
        """Группы, которые хоть у кого-то выбраны своими."""
        return [r[0] for r in self._db.execute(
            "SELECT DISTINCT name FROM users WHERE kind = 'group' ORDER BY name")]

    def group_in_use(self, name: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM users WHERE kind = 'group' AND name = ? LIMIT 1", (name,)
        ).fetchone() is not None

    def save_schedule(self, schedule: Schedule) -> None:
        # время загрузки с сайта, а не записи: расписание могло полежать в кэше в памяти
        now = schedule.fetched_at or int(time.time())
        self._db.execute(
            "INSERT INTO schedules (kind, name, data, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, name) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            (schedule.kind, schedule.name, schedule.to_json(), now),
        )
        self._db.commit()

    def load_schedule(self, kind: str, name: str) -> tuple[Schedule, int] | None:
        """Сохранённая копия и время её загрузки с сайта."""
        row = self._db.execute(
            "SELECT data, fetched_at FROM schedules WHERE kind = ? AND name = ?", (kind, name)
        ).fetchone()
        if row is None:
            return None
        schedule = Schedule.from_json(row[0])
        schedule.fetched_at = row[1]
        return schedule, row[1]

    def prune_schedules(self) -> int:
        """Удаляет копии групп, которые больше никто не выбрал. Возвращает, сколько удалено."""
        cur = self._db.execute(
            "DELETE FROM schedules WHERE NOT (kind = 'group' AND name IN "
            "(SELECT name FROM users WHERE kind = 'group'))"
        )
        self._db.commit()
        return cur.rowcount

    def saved_schedules_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

    # ---------- индекс предметов ----------

    def replace_subject_index(self, rows: set[tuple[str, str, str]], now: int | None = None) -> None:
        """Заменяет индекс целиком: (предмет, преподаватель, группа)."""
        now = int(now if now is not None else time.time())
        with self._db:  # одна транзакция: поиск не увидит полупустой индекс
            self._db.execute("DELETE FROM subject_index")
            self._db.executemany("INSERT INTO subject_index (subject, teacher, grp) VALUES (?, ?, ?)",
                                 sorted(rows))
            self._db.execute("INSERT INTO meta (key, value) VALUES ('subject_index_at', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(now),))

    def subject_index_updated_at(self) -> int | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'subject_index_at'").fetchone()
        return int(row[0]) if row else None

    def subject_names(self) -> list[str]:
        return [r[0] for r in self._db.execute("SELECT DISTINCT subject FROM subject_index ORDER BY subject")]

    def subject_rows(self, subjects: list[str]) -> list[tuple[str, str, str]]:
        if not subjects:
            return []
        marks = ",".join("?" * len(subjects))
        return self._db.execute(
            f"SELECT subject, teacher, grp FROM subject_index WHERE subject IN ({marks}) "
            "ORDER BY subject, teacher, grp", subjects).fetchall()

    # ---------- статистика ----------

    def stats(self, now: int | None = None, periods: tuple[int, ...] = (1, 7, 30)) -> Stats:
        now = int(now if now is not None else time.time())
        one = lambda sql, *args: self._db.execute(sql, args).fetchone()[0]  # noqa: E731

        groups = self._db.execute(
            "SELECT name, COUNT(*) FROM users WHERE kind = 'group' "
            "GROUP BY name ORDER BY COUNT(*) DESC, name"
        ).fetchall()
        by_faculty: dict[str, int] = {}
        for name, count in groups:
            faculty = name.split("-", 1)[0]
            by_faculty[faculty] = by_faculty.get(faculty, 0) + count

        return Stats(
            total=one("SELECT COUNT(*) FROM activity"),
            blocked=one("SELECT COUNT(*) FROM activity WHERE blocked = 1"),
            with_group=sum(count for _, count in groups),
            new={d: one("SELECT COUNT(*) FROM activity WHERE first_seen >= ?", now - d * DAY)
                 for d in periods},
            active={d: one("SELECT COUNT(*) FROM activity WHERE last_seen >= ?", now - d * DAY)
                    for d in periods},
            top_groups=groups[:10],
            groups_by_faculty=dict(sorted(by_faculty.items(), key=lambda kv: (-kv[1], kv[0]))),
            langs=dict(self._db.execute(
                "SELECT lang, COUNT(*) FROM activity WHERE lang IS NOT NULL "
                "GROUP BY lang ORDER BY COUNT(*) DESC, lang").fetchall()),
        )

    def close(self) -> None:
        self._db.close()
