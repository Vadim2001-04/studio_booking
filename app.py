"""Система бронирования фотостудий — MVP на Flask."""
import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import STUDIO_CATEGORIES, close_db, get_db, init_db

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.teardown_appcontext(close_db)

BOOKING_DAYS_AHEAD = 14  # на сколько дней вперёд можно бронировать


# ---------- Вспомогательное ----------

@app.before_request
def load_current_user():
    """Перед каждым запросом достаём текущего пользователя из сессии в g.user."""
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            flash("Войдите, чтобы продолжить.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def slot_start(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def slot_label(hour):
    return f"{hour:02d}:00–{hour + 1:02d}:00"


def get_studio_or_404(studio_id):
    studio = get_db().execute("SELECT * FROM studios WHERE id = ?", (studio_id,)).fetchone()
    if studio is None:
        abort(404)
    return studio


@app.template_filter("ru_date")
def ru_date(value):
    d = value if isinstance(value, date) else parse_date(value)
    return d.strftime("%d.%m.%Y") if d else value


@app.context_processor
def inject_helpers():
    return {"slot_label": slot_label, "current_user": g.get("user")}


# ---------- Каталог ----------

@app.route("/")
def index():
    category = request.args.get("category", "")
    sql = "SELECT * FROM studios WHERE is_active = 1"
    params = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    studios = get_db().execute(sql + " ORDER BY name", params).fetchall()
    return render_template("index.html", studios=studios, categories=STUDIO_CATEGORIES, selected_category=category)


@app.route("/studio/<int:studio_id>")
def studio_detail(studio_id):
    studio = get_studio_or_404(studio_id)
    if not studio["is_active"] and not (g.user and g.user["is_admin"]):
        abort(404)

    today = date.today()
    selected = parse_date(request.args.get("date")) or today
    if selected < today or selected > today + timedelta(days=BOOKING_DAYS_AHEAD):
        selected = today

    rows = get_db().execute(
        "SELECT hour FROM bookings WHERE studio_id = ? AND date = ? AND status = 'active'",
        (studio_id, selected.isoformat()),
    ).fetchall()
    booked = {r["hour"] for r in rows}

    now = datetime.now()
    slots = []
    for h in range(studio["open_hour"], studio["close_hour"]):
        if h in booked:
            state = "booked"
        elif slot_start(selected, h) <= now:
            state = "past"
        else:
            state = "free"
        slots.append({"hour": h, "label": slot_label(h), "state": state})

    days = [today + timedelta(days=i) for i in range(BOOKING_DAYS_AHEAD + 1)]
    return render_template("studio.html", studio=studio, slots=slots, selected=selected, days=days)


# ---------- Бронирование ----------

@app.route("/studio/<int:studio_id>/book", methods=["POST"])
@login_required
def book(studio_id):
    studio = get_studio_or_404(studio_id)
    booking_date = parse_date(request.form.get("date"))
    try:
        hour = int(request.form.get("hour", ""))
    except ValueError:
        hour = None

    back = redirect(url_for("studio_detail", studio_id=studio_id,
                            date=(booking_date or date.today()).isoformat()))

    if not studio["is_active"]:
        flash("Студия сейчас недоступна для бронирования.", "danger")
        return back
    if booking_date is None or hour is None or not (studio["open_hour"] <= hour < studio["close_hour"]):
        flash("Некорректные дата или время.", "danger")
        return back
    if booking_date > date.today() + timedelta(days=BOOKING_DAYS_AHEAD):
        flash(f"Бронировать можно не дальше чем на {BOOKING_DAYS_AHEAD} дней вперёд.", "danger")
        return back
    if slot_start(booking_date, hour) <= datetime.now():
        flash("Нельзя забронировать время в прошлом.", "danger")
        return back

    db = get_db()
    try:
        db.execute(
            "INSERT INTO bookings (user_id, studio_id, date, hour, price) VALUES (?, ?, ?, ?, ?)",
            (g.user["id"], studio_id, booking_date.isoformat(), hour, studio["price_per_hour"]),
        )
        db.commit()
    except sqlite3.IntegrityError:  # сработал уникальный индекс — слот уже занят
        flash("Этот слот уже занят.", "danger")
        return back

    flash(f"Готово! {studio['name']}, {booking_date:%d.%m.%Y} {slot_label(hour)}.", "success")
    return back


@app.route("/my-bookings")
@login_required
def my_bookings():
    rows = get_db().execute(
        """SELECT b.*, v.name AS studio_name, v.address
           FROM bookings b JOIN studios v ON v.id = b.studio_id
           WHERE b.user_id = ? ORDER BY b.date DESC, b.hour DESC""",
        (g.user["id"],),
    ).fetchall()
    now = datetime.now()
    bookings = [
        dict(r, is_future=slot_start(parse_date(r["date"]), r["hour"]) > now) for r in rows
    ]
    return render_template("my_bookings.html", bookings=bookings)


@app.route("/booking/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id):
    db = get_db()
    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        abort(404)
    is_admin = bool(g.user["is_admin"])
    if booking["user_id"] != g.user["id"] and not is_admin:
        abort(403)

    if booking["status"] != "active":
        flash("Бронь уже отменена.", "warning")
    elif slot_start(parse_date(booking["date"]), booking["hour"]) <= datetime.now() and not is_admin:
        flash("Нельзя отменить бронь, которая уже началась или прошла.", "danger")
    else:
        db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        db.commit()
        flash("Бронь отменена.", "success")
    next_url = request.form.get("next", "")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = url_for("my_bookings")
    return redirect(next_url)


# ---------- Регистрация и вход ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        if not name or "@" not in email or len(password) < 6:
            flash("Заполните все поля корректно, пароль — минимум 6 символов.", "danger")
        elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            flash("Пользователь с таким email уже существует.", "danger")
        else:
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (name, email, generate_password_hash(password)),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            flash("Регистрация прошла успешно!", "success")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
        flash("Неверный email или пароль.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Админ-панель ----------

@app.route("/admin/studios")
@admin_required
def admin_studios():
    studios = get_db().execute("SELECT * FROM studios ORDER BY id").fetchall()
    return render_template("admin/studios.html", studios=studios)


def studio_from_form():
    """Читает форму студии. Возвращает (данные, ошибка)."""
    f = request.form
    data = {
        "name": f.get("name", "").strip(),
        "category": f.get("category", ""),
        "address": f.get("address", "").strip(),
        "description": f.get("description", "").strip(),
        "price_per_hour": f.get("price_per_hour", ""),
        "open_hour": f.get("open_hour", ""),
        "close_hour": f.get("close_hour", ""),
        "is_active": 1 if f.get("is_active") == "on" else 0,
    }
    try:
        for key in ("price_per_hour", "open_hour", "close_hour"):
            data[key] = int(data[key])
    except ValueError:
        return data, "Цена и часы работы должны быть целыми числами."
    if not data["name"] or not data["address"]:
        return data, "Название и адрес обязательны."
    if data["category"] not in STUDIO_CATEGORIES:
        return data, "Выберите тип студии из списка."
    if data["price_per_hour"] <= 0:
        return data, "Цена должна быть больше нуля."
    if not (0 <= data["open_hour"] < data["close_hour"] <= 24):
        return data, "Часы работы: 0 ≤ открытие < закрытие ≤ 24."
    return data, None


@app.route("/admin/studios/new", methods=["GET", "POST"])
@admin_required
def admin_studio_new():
    studio = {"name": "", "category": STUDIO_CATEGORIES[0], "address": "", "description": "",
             "price_per_hour": "", "open_hour": 8, "close_hour": 22, "is_active": 1}
    if request.method == "POST":
        studio, error = studio_from_form()
        if error:
            flash(error, "danger")
        else:
            db = get_db()
            db.execute(
                """INSERT INTO studios (name, category, address, description,
                       price_per_hour, open_hour, close_hour, is_active)
                   VALUES (:name, :category, :address, :description,
                       :price_per_hour, :open_hour, :close_hour, :is_active)""",
                studio,
            )
            db.commit()
            flash("Студия добавлена.", "success")
            return redirect(url_for("admin_studios"))
    return render_template("admin/studio_form.html", studio=studio, categories=STUDIO_CATEGORIES, is_new=True)


@app.route("/admin/studios/<int:studio_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_studio_edit(studio_id):
    studio = dict(get_studio_or_404(studio_id))
    if request.method == "POST":
        data, error = studio_from_form()
        if error:
            flash(error, "danger")
            studio.update(data)
        else:
            data["id"] = studio_id
            db = get_db()
            db.execute(
                """UPDATE studios SET name = :name, category = :category, address = :address,
                       description = :description, price_per_hour = :price_per_hour,
                       open_hour = :open_hour, close_hour = :close_hour, is_active = :is_active
                   WHERE id = :id""",
                data,
            )
            db.commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("admin_studios"))
    return render_template("admin/studio_form.html", studio=studio, categories=STUDIO_CATEGORIES, is_new=False)


@app.route("/admin/bookings")
@admin_required
def admin_bookings():
    studio_id = request.args.get("studio_id", type=int)
    status = request.args.get("status", "")
    sql = """SELECT b.*, v.name AS studio_name, u.name AS user_name, u.email AS user_email
             FROM bookings b
             JOIN studios v ON v.id = b.studio_id
             JOIN users  u ON u.id = b.user_id
             WHERE 1 = 1"""
    params = []
    if studio_id:
        sql += " AND b.studio_id = ?"
        params.append(studio_id)
    if status in ("active", "cancelled"):
        sql += " AND b.status = ?"
        params.append(status)
    db = get_db()
    bookings = db.execute(sql + " ORDER BY b.date DESC, b.hour DESC", params).fetchall()
    restudio = sum(b["price"] for b in bookings if b["status"] == "active")
    studios = db.execute("SELECT id, name FROM studios ORDER BY name").fetchall()
    return render_template("admin/bookings.html", bookings=bookings, studios=studios,
                           studio_id=studio_id, status=status, restudio=restudio)


# ---------- Ошибки ----------

@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403, message="Доступ запрещён."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, message="Страница не найдена."), 404


init_db()

if __name__ == "__main__":
    app.run(debug=True)
