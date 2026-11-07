"""Откуда брать расписание: с сайта, а если он недоступен — из копии в базе.
Здесь же сборка индекса предметов для /subject.

Копии хранятся только для групп, которые хоть у кого-то выбраны своими.
Они обновляются при каждой удачной загрузке с сайта и фоновой задачей
refresh_loop — чтобы копия была у каждой такой группы, даже если её давно
никто не открывал.
"""

from __future__ import annotations

import asyncio
import logging
import time

from formatting import parse_lesson
from storage import Storage
from vspu import GROUP, Schedule, SiteError, VspuClient

log = logging.getLogger(__name__)

REFRESH_INTERVAL = 2 * 60 * 60  # как часто обновлять копии всех используемых групп
REFRESH_PAUSE = 1.0  # пауза между группами, чтобы не нагружать сайт

INDEX_INTERVAL = 24 * 60 * 60  # индекс предметов — раз в сутки
INDEX_PAUSE = 1.5  # обходим все ~300 групп — ещё осторожнее, ~8 минут на круг
INDEX_RETRY = 60 * 60  # если сайт подвёл — следующая попытка через час

# Какая загрузка с сайта уже записана в базу: пока расписание лежит в кэше
# в памяти, не перезаписываем копию при каждом нажатии кнопки.
_saved: dict[str, int] = {}

# Если сайт не ответил, следующие SITE_DOWN_PAUSE секунд не ходим к нему за
# расписаниями, у которых есть копия: иначе каждое нажатие ждало бы таймаут.
SITE_DOWN_PAUSE = 120
_site_down_until = 0.0


async def get_schedule(client: VspuClient, storage: Storage,
                       kind: str, name: str) -> tuple[Schedule, int | None]:
    """Возвращает (расписание, время копии). Время копии — None, если расписание
    свежее с сайта, или unix-время загрузки, если сайт недоступен и показываем
    сохранённую копию. SiteError — если сайт недоступен, а копии нет."""
    global _site_down_until
    if (fresh := client.cached(kind, name)) is not None:
        return fresh, None

    # Сайт только что не ответил: не ждём таймаут снова, сразу отдаём копию
    if time.monotonic() < _site_down_until:
        if (saved := storage.load_schedule(kind, name)) is not None:
            return saved

    try:
        schedule = await client.schedule(kind, name)
    except SiteError as e:
        _site_down_until = time.monotonic() + SITE_DOWN_PAUSE
        saved = storage.load_schedule(kind, name)
        if saved is None:
            raise
        log.warning("Сайт недоступен (%s), отдаю копию %s %s", e, kind, name)
        return saved

    _site_down_until = 0.0

    if kind == GROUP and _saved.get(name) != schedule.fetched_at and storage.group_in_use(name):
        storage.save_schedule(schedule)
        _saved[name] = schedule.fetched_at
    return schedule, None


async def refresh_all(client: VspuClient, storage: Storage) -> tuple[int, int]:
    """Обновляет копии всех используемых групп. Возвращает (обновлено, ошибок)."""
    removed = storage.prune_schedules()
    if removed:
        log.info("Удалены копии расписаний групп, которые больше никто не выбрал: %d", removed)
        _saved.clear()  # иначе группа, выбранная снова, не сохранится до новой загрузки

    ok = failed = 0
    for name in storage.used_groups():
        try:
            schedule = await client.schedule(GROUP, name)
            storage.save_schedule(schedule)
            _saved[name] = schedule.fetched_at
            ok += 1
        except SiteError as e:
            failed += 1
            if ok == 0 and failed >= 3:
                # три ошибки подряд с самого начала — сайт, видимо, лежит; не мучаем его
                log.warning("Обновление копий прервано, сайт недоступен: %s", e)
                break
        await asyncio.sleep(REFRESH_PAUSE)
    return ok, failed


async def rebuild_subject_index(client: VspuClient, storage: Storage,
                                pause: float | None = None) -> int | None:
    """Обходит расписания всех групп и собирает, кто какой предмет где ведёт.
    Возвращает число записей или None, если сайт подвёл и индекс не заменён."""
    pause = INDEX_PAUSE if pause is None else pause
    groups = await client.groups()
    teachers = set(await client.teachers())  # «Учитель 1» и прочие заглушки — мимо
    rows: set[tuple[str, str, str]] = set()
    failed = consecutive = 0

    for name in groups:
        try:
            schedule = await client.schedule(GROUP, name, cache=False)
        except SiteError as e:
            failed += 1
            consecutive += 1
            if consecutive >= 5:
                log.warning("Индекс предметов: сайт перестал отвечать (%s), индекс не обновлён", e)
                return None
        else:
            consecutive = 0
            if storage.group_in_use(name):  # раз уж скачали — обновим и копию
                storage.save_schedule(schedule)
                _saved[name] = schedule.fetched_at
            for day in schedule.days.values():
                for lesson in day.lessons:
                    view = parse_lesson(lesson, GROUP)
                    if view.parsed and view.who in teachers:
                        rows.add((view.subject, view.who, name))
        await asyncio.sleep(pause)

    if failed > len(groups) // 4:
        log.warning("Индекс предметов: не загрузилось %d групп из %d, индекс не обновлён",
                    failed, len(groups))
        return None
    if not rows:
        # во всех группах ни одной пары — скорее всего, сайт что-то поменял; хороший индекс не трогаем
        log.warning("Индекс предметов: не нашлось ни одного предмета, индекс не обновлён")
        return None
    storage.replace_subject_index(rows)
    log.info("Индекс предметов обновлён: %d записей, %d предметов, ошибок %d",
             len(rows), len({r[0] for r in rows}), failed)
    return len(rows)


async def subject_index_loop(client: VspuClient, storage: Storage,
                             interval: float = INDEX_INTERVAL, first_delay: float = 120) -> None:
    """Фоновая задача: индекс предметов раз в сутки. После перезапуска бота
    не пересобирает, если последний индекс ещё свежий."""
    await asyncio.sleep(first_delay)
    while True:
        updated = storage.subject_index_updated_at()
        # пустой индекс (например, затёрт, пока парсер не понимал сайт) — собираем сразу
        wait = (updated + interval - time.time()) if updated and storage.subject_names() else 0
        if wait > 0:
            await asyncio.sleep(wait)
            continue
        try:
            built = await rebuild_subject_index(client, storage)
        except SiteError as e:  # даже списки групп не загрузились
            log.warning("Индекс предметов не обновлён, сайт недоступен: %s", e)
            built = None
        except Exception:
            log.exception("Ошибка при обновлении индекса предметов")
            built = None
        if built is None:
            await asyncio.sleep(INDEX_RETRY)  # сайт лежал — попробуем позже


async def refresh_loop(client: VspuClient, storage: Storage,
                       interval: float = REFRESH_INTERVAL, first_delay: float = 30) -> None:
    """Фоновая задача: сразу после запуска и потом каждые interval секунд."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            ok, failed = await refresh_all(client, storage)
            log.info("Копии расписаний обновлены: %d, ошибок: %d", ok, failed)
        except Exception:  # фоновая задача не должна умирать из-за одной ошибки
            log.exception("Ошибка при обновлении копий расписаний")
        await asyncio.sleep(interval)
