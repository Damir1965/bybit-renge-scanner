"""
Автоочистка диска под 1 ГБ persistent disk.
- Удаляет логи старше CLEANUP_LOG_DAYS
- Удаляет CSV старше CLEANUP_CSV_DAYS
- Раз в CLEANUP_DB_VACUUM_DAYS делает VACUUM SQLite
"""
import logging
import os
import sqlite3
import time

import config as cfg

log = logging.getLogger("cleanup")

_last_vacuum = 0.0


def cleanup_logs():
    if not os.path.isdir(cfg.LOG_DIR):
        return
    cutoff = time.time() - cfg.CLEANUP_LOG_DAYS * 86400
    for fname in os.listdir(cfg.LOG_DIR):
        fpath = os.path.join(cfg.LOG_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                log.info(f"Удалён старый лог: {fname}")
        except Exception as e:
            log.warning(f"Не удалось удалить {fname}: {e}")


def cleanup_csv():
    if not os.path.isdir(cfg.CSV_DIR):
        return
    cutoff = time.time() - cfg.CLEANUP_CSV_DAYS * 86400
    removed = 0
    for fname in os.listdir(cfg.CSV_DIR):
        fpath = os.path.join(cfg.CSV_DIR, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed += 1
        except Exception as e:
            log.warning(f"Не удалось удалить {fname}: {e}")
    if removed:
        log.info(f"Удалено старых CSV: {removed}")


def vacuum_db():
    global _last_vacuum
    now = time.time()
    if (now - _last_vacuum) < cfg.CLEANUP_DB_VACUUM_DAYS * 86400:
        return
    if not os.path.exists(cfg.DATABASE_PATH):
        return
    try:
        conn = sqlite3.connect(cfg.DATABASE_PATH)
        conn.execute("VACUUM")
        conn.close()
        _last_vacuum = now
        log.info("SQLite VACUUM выполнен")
    except Exception as e:
        log.warning(f"VACUUM ошибка: {e}")


def run_cleanup():
    if not cfg.CLEANUP_ENABLED:
        return
    cleanup_logs()
    cleanup_csv()
    vacuum_db()


def disk_usage_mb() -> float:
    """Сколько МБ занимает /data."""
    if not os.path.isdir(cfg.DATA_DIR):
        return 0.0
    total = 0
    for root, _, files in os.walk(cfg.DATA_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return round(total / (1024 * 1024), 2)
