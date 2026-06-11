"""Памʼять контейнера так, як її бачить OOM-кіллер Render (cgroup), а НЕ сума RSS.
Сума RSS python+children рахує shared-сторінки Chromium (спільні бібліотеки,
shared memory між його процесами) ПО КІЛЬКА разів. Факт із логів: сума показала
637.9 MB, а інстанс із лімітом 512 MB вижив — переоблік ~25-40%.
Метрика: working set = memory.current - inactive_file (reclaimable page cache,
ядро віддасть його під тиском — OOM через нього не приходить).
Фолбеки: cgroup v1 → чесна сума RSS (завищує; джерело видно в логах)."""
import os

import psutil

_CG2 = "/sys/fs/cgroup"
_CG1 = "/sys/fs/cgroup/memory"


def _read_int(path: str) -> int:
    with open(path) as f:
        return int(f.read().strip())


def _stat_map(stat_path: str) -> dict:
    m = {}
    try:
        with open(stat_path) as f:
            for line in f:
                key, _, val = line.partition(" ")
                try:
                    m[key] = int(val)
                except ValueError:
                    continue
    except OSError:
        pass
    return m


def rss_breakdown_mb() -> tuple[float, float]:
    """(python_MB, children_MB) — лише для діагностики в логах.
    Дитина може померти між children() і memory_info() → ловимо поштучно."""
    p = psutil.Process(os.getpid())
    own = p.memory_info().rss
    child = 0
    for c in p.children(recursive=True):
        try:
            child += c.memory_info().rss
        except psutil.Error:
            continue
    return own / 1048576, child / 1048576


def used_mb() -> tuple[float, str]:
    """(використано_MB, джерело). Те, від чого реально приходить OOM-кілл:
    used = current - (file - shmem). 'file' = ВЕСЬ page cache, включно з
    mmap'нутими бінарями Chromium (сотні MB) — ядро ВИКИДАЄ його перед OOM
    (калібровка: 'used=433MB' у порожньому боті, бо вираховувався лише
    inactive_file; інстанс 512MB пережив 'rss-суму 638'). shmem сидить
    усередині 'file', але без swap НЕ викидається → лишаємо в used."""
    # cgroup v2 (очікувано на Render)
    try:
        cur = _read_int(f"{_CG2}/memory.current")
        st = _stat_map(f"{_CG2}/memory.stat")
        reclaimable = max(st.get("file", 0) - st.get("shmem", 0), 0)
        return max(cur - reclaimable, 0) / 1048576, "cgroup2"
    except (OSError, ValueError):
        pass
    # cgroup v1 (ключі cache/total_cache, shmem/total_shmem)
    try:
        cur = _read_int(f"{_CG1}/memory.usage_in_bytes")
        st = _stat_map(f"{_CG1}/memory.stat")
        cache = st.get("total_cache", st.get("cache", 0))
        shmem = st.get("total_shmem", st.get("shmem", 0))
        reclaimable = max(cache - shmem, 0)
        return max(cur - reclaimable, 0) / 1048576, "cgroup1"
    except (OSError, ValueError):
        pass
    # Фолбек: сума RSS (завищує через shared-сторінки). Якщо в логах постійно
    # "rss-sum" — cgroupfs недоступний, ліміт треба калібрувати під переоблік.
    own, child = rss_breakdown_mb()
    return own + child, "rss-sum"
