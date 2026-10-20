import os
import sqlite3
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, abort
)

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "database.db"

TOTAL_PRICE = 30_000_000
DEPOSIT_PRICE = 5_000_000

PAYMENT_METHODS = {
    "in_person": "پرداخت حضوری",
    "card_to_card": "کارت‌به‌کارت",
    "online": "درگاه پرداخت آنلاین",
}

RESERVATION_STATUSES = {"در انتظار بررسی", "تأیید شده", "رد شده"}

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY", "change-this-secret-key-before-deployment"
)


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_session() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                event_type TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_time TEXT NOT NULL,
                guests INTEGER NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'در انتظار بررسی',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(reservations)")
        }

        migrations = {
            "base_amount": "INTEGER NOT NULL DEFAULT 5000000",
            "discount_code": "TEXT",
            "discount_amount": "INTEGER NOT NULL DEFAULT 0",
            "deposit_amount": "INTEGER NOT NULL DEFAULT 5000000",
            "total_price": "INTEGER NOT NULL DEFAULT 30000000",
            "payment_method": "TEXT NOT NULL DEFAULT 'in_person'",
            "payment_status": "TEXT NOT NULL DEFAULT 'در انتظار پرداخت'",
        }

        for name, definition in migrations.items():
            if name not in columns:
                db.execute(
                    f"ALTER TABLE reservations ADD COLUMN {name} {definition}"
                )

        db.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                discount_amount INTEGER NOT NULL CHECK(discount_amount > 0),
                is_active INTEGER NOT NULL DEFAULT 1,
                used_reservation_id INTEGER UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(used_reservation_id) REFERENCES reservations(id)
            )
        """)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {
        "site_name": "سالن آمفی‌تئاتر الزهرا",
        "total_price": TOTAL_PRICE,
        "deposit_price": DEPOSIT_PRICE,
        "payment_methods": PAYMENT_METHODS,
    }


@app.template_filter("toman")
def toman(value):
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return "۰"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/facilities")
def facilities():
    return render_template("facilities.html")


@app.route("/gallery")
def gallery():
    return render_template("gallery.html")


@app.route("/reservation", methods=["GET", "POST"])
def reservation():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        event_type = request.form.get("event_type", "").strip()
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        description = request.form.get("description", "").strip()
        discount_code = request.form.get("discount_code", "").strip().upper()
        payment_method = request.form.get("payment_method", "in_person").strip()

        try:
            guests = int(request.form.get("guests", "0"))
        except (ValueError, TypeError):
            guests = 0

        if not all([full_name, phone, event_type, event_date, event_time]):
            flash("لطفاً همه فیلدهای ضروری را تکمیل کنید.", "error")
            return render_template("reservation.html")

        if guests < 1 or guests > 400:
            flash("تعداد شرکت‌کنندگان باید بین ۱ تا ۴۰۰ نفر باشد.", "error")
            return render_template("reservation.html")

        if payment_method not in PAYMENT_METHODS:
            flash("روش پرداخت انتخاب‌شده معتبر نیست.", "error")
            return render_template("reservation.html")

        if len(full_name) > 120 or len(phone) > 30 or len(email) > 150:
            flash("طول یکی از اطلاعات واردشده بیش از حد مجاز است.", "error")
            return render_template("reservation.html")

        if len(description) > 3000 or len(discount_code) > 50:
            flash("یکی از فیلدهای واردشده بیش از حد مجاز است.", "error")
            return render_template("reservation.html")

        if len(event_date) != 10 or event_date[4:5] != "-" or event_date[7:8] != "-":
            flash("تاریخ انتخاب‌شده معتبر نیست.", "error")
            return render_template("reservation.html")

        discount_amount = 0

        try:
            with db_session() as db:
                db.execute("BEGIN IMMEDIATE")

                if discount_code:
                    code_row = db.execute("""
                        SELECT id, discount_amount
                        FROM discount_codes
                        WHERE code = ? COLLATE NOCASE
                          AND is_active = 1
                          AND used_reservation_id IS NULL
                    """, (discount_code,)).fetchone()

                    if not code_row:
                        flash(
                            "کد تخفیف نامعتبر، غیرفعال یا قبلاً استفاده‌شده است.",
                            "error"
                        )
                        return render_template("reservation.html")

                    discount_amount = min(
                        int(code_row["discount_amount"]), DEPOSIT_PRICE
                    )

                final_deposit = DEPOSIT_PRICE - discount_amount

                cursor = db.execute("""
                    INSERT INTO reservations (
                        full_name, phone, email, event_type, event_date,
                        event_time, guests, description, status,
                        base_amount, discount_code, discount_amount,
                        deposit_amount, total_price, payment_method,
                        payment_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    full_name, phone, email, event_type, event_date,
                    event_time, guests, description, "در انتظار بررسی",
                    DEPOSIT_PRICE, discount_code or None, discount_amount,
                    final_deposit, TOTAL_PRICE, payment_method,
                    "در انتظار پرداخت"
                ))

                reservation_id = cursor.lastrowid

                if discount_code:
                    updated = db.execute("""
                        UPDATE discount_codes
                        SET used_reservation_id = ?
                        WHERE id = ?
                          AND used_reservation_id IS NULL
                          AND is_active = 1
                    """, (reservation_id, code_row["id"]))

                    if updated.rowcount != 1:
                        raise RuntimeError("Discount code was already used")

        except RuntimeError:
            flash("این کد تخفیف هم‌زمان توسط شخص دیگری استفاده شد؛ کد دیگری وارد کنید.", "error")
            return render_template("reservation.html")
        except sqlite3.Error:
            app.logger.exception("Reservation database error")
            flash("ثبت درخواست با خطا مواجه شد. دوباره تلاش کنید.", "error")
            return render_template("reservation.html")

        flash(
            f"درخواست رزرو ثبت شد. بیعانه پس از تخفیف: "
            f"{final_deposit:,} تومان. پرداخت هنوز تأیید نشده است.",
            "success"
        )
        return redirect(url_for("reservation"))

    return render_template("reservation.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = os.environ.get("ADMIN_USERNAME", "admin")
        password = os.environ.get("ADMIN_PASSWORD", "change-me-now")

        if (
            request.form.get("username", "") == username
            and request.form.get("password", "") == password
        ):
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        flash("نام کاربری یا رمز عبور اشتباه است.", "error")

    return render_template("admin_login.html")


@app.route("/admin")
@admin_required
def admin_dashboard():
    with closing(get_db()) as db:
        reservations = db.execute(
            "SELECT * FROM reservations ORDER BY id DESC"
        ).fetchall()
        discounts = db.execute("""
            SELECT d.*, r.full_name AS used_by_name
            FROM discount_codes d
            LEFT JOIN reservations r ON r.id = d.used_reservation_id
            ORDER BY d.id DESC
        """).fetchall()

    return render_template(
        "admin_dashboard.html",
        reservations=reservations,
        discounts=discounts,
    )


@app.route("/admin/discounts/create", methods=["POST"])
@admin_required
def create_discount():
    code = request.form.get("code", "").strip().upper()
    try:
        amount = int(request.form.get("discount_amount", "0"))
    except (ValueError, TypeError):
        amount = 0

    if not code or len(code) > 50:
        flash("کد تخفیف باید بین ۱ تا ۵۰ نویسه باشد.", "error")
        return redirect(url_for("admin_dashboard"))

    if amount < 1:
        flash("مبلغ تخفیف باید بیشتر از صفر باشد.", "error")
        return redirect(url_for("admin_dashboard"))

    if amount > DEPOSIT_PRICE:
        flash("تخفیف نمی‌تواند از مبلغ بیعانه بیشتر باشد.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        with db_session() as db:
            db.execute("""
                INSERT INTO discount_codes (code, discount_amount)
                VALUES (?, ?)
            """, (code, amount))
        flash("کد تخفیف با موفقیت ساخته شد.", "success")
    except sqlite3.IntegrityError:
        flash("این کد تخفیف از قبل وجود دارد.", "error")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/discounts/<int:discount_id>/toggle", methods=["POST"])
@admin_required
def toggle_discount(discount_id):
    with db_session() as db:
        row = db.execute(
            "SELECT is_active FROM discount_codes WHERE id = ?",
            (discount_id,)
        ).fetchone()

        if row is None:
            abort(404)

        db.execute(
            "UPDATE discount_codes SET is_active = ? WHERE id = ?",
            (0 if row["is_active"] else 1, discount_id)
        )

    flash("وضعیت کد تخفیف به‌روزرسانی شد.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reservation/<int:reservation_id>/status", methods=["POST"])
@admin_required
def update_status(reservation_id):
    status = request.form.get("status", "")

    if status not in RESERVATION_STATUSES:
        abort(400)

    with db_session() as db:
        db.execute(
            "UPDATE reservations SET status = ? WHERE id = ?",
            (status, reservation_id)
        )

    flash("وضعیت درخواست به‌روزرسانی شد.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.errorhandler(404)
def not_found(error):
    return render_template(
        "error.html", code=404, message="صفحه موردنظر پیدا نشد."
    ), 404


@app.errorhandler(500)
def server_error(error):
    return render_template(
        "error.html", code=500, message="خطایی در سرور رخ داده است."
    ), 500


init_db()

if __name__ == "__main__":
    app.run(debug=True)