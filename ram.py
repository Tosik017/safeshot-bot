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


def _inactive_file(stat_path: str) -> int:
    try:
        with open(stat_path) as f:
            for line in f:
                key, _, val = line.partition(" ")
                if key in ("inactive_file", "total_inactive_file"):
                    return int(val)
    except (OSError, ValueError):
        pass
    return 0


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
    """(використано_MB, джерело). Те, від чого реально приходить OOM-кілл."""
    # cgroup v2 (очікувано на Render)
    try:
        cur = _read_int(f"{_CG2}/memory.current")
        used = max(cur - _inactive_file(f"{_CG2}/memory.stat"), 0)
        return used / 1048576, "cgroup2"
    except (OSError, ValueError):
        pass
    # cgroup v1
    try:
        cur = _read_int(f"{_CG1}/memory.usage_in_bytes")
        used = max(cur - _inactive_file(f"{_CG1}/memory.stat"), 0)
        return used / 1048576, "cgroup1"
    except (OSError, ValueError):
        pass
    # Фолбек: сума RSS (завищує через shared-сторінки). Якщо в логах постійно
    # "rss-sum" — cgroupfs недоступний, ліміт треба калібрувати під переоблік.
    own, child = rss_breakdown_mb()
    return own + child, "rss-sum"
