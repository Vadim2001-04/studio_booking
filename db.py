"""Работа с базой данных SQLite: подключение, создание таблиц, тестовые данные."""
import os
import sqlite3

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "booking.db")

STUDIO_CATEGORIES = ["Интерьерная", "Циклорама", "Предметная съёмка", "Видео и подкасты", "Другое"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS studios (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    category       TEXT    NOT NULL,
    address        TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    price_per_hour INTEGER NOT NULL CHECK (price_per_hour > 0),
    open_hour      INTEGER NOT NULL DEFAULT 9,
    close_hour     INTEGER NOT NULL DEFAULT 23,
    is_active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    studio_id  INTEGER NOT NULL REFERENCES studios(id),
    date       TEXT    NOT NULL,              -- 'YYYY-MM-DD'
    hour       INTEGER NOT NULL,              -- слот hour:00–hour+1:00
    price      INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active',  -- active / cancelled
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Защита от двойной брони на уровне БД: один активный слот на студию
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_slot
    ON bookings (studio_id, date, hour) WHERE status = 'active';
"""

SEED_STUDIOS = [
    ("Зал «Лофт»", "Интерьерная", "СПб, ул. Мира, 3",
     "Кирпичные стены, панорамные окна, диван и реквизит. Площадь 80 м².", 2500, 9, 23),
    ("Зал «Белый куб»", "Циклорама", "СПб, Лиговский пр., 50",
     "Белая циклорама 6×4 м, импульсный свет Godox, фоны на выбор.", 3000, 9, 23),
    ("Студия «Предмет»", "Предметная съёмка", "Мурино, Привокзальная пл., 3",
     "Столы для предметки, лайтбокс, свет для карточек маркетплейсов.", 1500, 10, 22),
    ("Студия «Эфир»", "Видео и подкасты", "СПб, пр. Энгельса, 27",
     "Звукоизоляция, 3 микрофона, 2 камеры, постоянный свет.", 3500, 10, 23),
]


def get_db():
    """Одно подключение на запрос; хранится в flask.g."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row  # строки как словари: row["name"]
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Создаёт таблицы и наполняет тестовыми данными при первом запуске."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users WHERE email = ?", ("admin@example.com",)).fetchone():
        conn.execute(
            "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?, ?, ?, 1)",
            ("Администратор", "admin@example.com", generate_password_hash("admin123")),
        )
    if conn.execute("SELECT COUNT(*) FROM studios").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO studios (name, category, address, description, price_per_hour, open_hour, close_hour)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            SEED_STUDIOS,
        )
    conn.commit()
    conn.close()
