import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "ramadan_expenses.db"

MEAL_TYPES = ["Sehri", "Iftar"]
CATEGORIES = ["Sehri", "Iftar", "Misc"]
GROUP_USERS = ["Me", "Israr", "Ahmed", "Arfat", "Mustfain"]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-change-this-secret-in-production"

    @app.before_request
    def before_request() -> None:
        get_db()

    @app.teardown_appcontext
    def close_connection(exception=None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def dashboard():
        db = get_db()
        payer_id = get_user_id_by_name(db, "Me")

        total_spent = db.execute(
            "SELECT COALESCE(SUM(actual_price), 0) AS total FROM orders"
        ).fetchone()["total"]

        owed_rows = db.execute(
            """
            SELECT u.name, COALESCE(SUM(oc.share_amount), 0) AS amount_owed
            FROM order_consumers oc
            JOIN orders o ON o.id = oc.order_id
            JOIN users u ON u.id = oc.user_id
            WHERE o.payer_user_id = ? AND u.name != 'Me'
            GROUP BY u.id
            ORDER BY u.name
            """,
            (payer_id,),
        ).fetchall()

        history_rows = db.execute(
            """
            SELECT dl.id,
                   dl.day_number,
                   dl.meal_type,
                   dl.log_date,
                   COUNT(o.id) AS item_count,
                   COALESCE(SUM(o.actual_price), 0) AS total_amount
            FROM daily_logs dl
            LEFT JOIN orders o ON o.daily_log_id = dl.id
            GROUP BY dl.id
            ORDER BY dl.day_number ASC,
                     CASE dl.meal_type WHEN 'Sehri' THEN 1 ELSE 2 END ASC
            """
        ).fetchall()

        return render_template(
            "dashboard.html",
            total_spent=total_spent,
            owed_rows=owed_rows,
            history_rows=history_rows,
        )

    @app.route("/log/new", methods=["GET", "POST"])
    def new_log():
        db = get_db()
        users = rows_to_dicts(db.execute("SELECT id, name FROM users ORDER BY id").fetchall())
        catalog_items = rows_to_dicts(
            db.execute(
                "SELECT id, name, default_price, category FROM catalog_items ORDER BY name"
            ).fetchall()
        )

        if request.method == "POST":
            day_number = int(request.form.get("day_number", 0))
            meal_type = request.form.get("meal_type", "")
            log_date = request.form.get("log_date") or str(date.today())
            payload_raw = request.form.get("orders_payload", "[]")

            if day_number < 1 or day_number > 30:
                flash("Day number must be between 1 and 30.", "danger")
                return redirect(url_for("new_log"))
            if meal_type not in MEAL_TYPES:
                flash("Please choose Sehri or Iftar.", "danger")
                return redirect(url_for("new_log"))

            try:
                orders = json.loads(payload_raw)
                if not isinstance(orders, list) or not orders:
                    raise ValueError("No orders")
            except (json.JSONDecodeError, ValueError):
                flash("Please add at least one valid item.", "danger")
                return redirect(url_for("new_log"))

            log_id = upsert_daily_log(db, day_number, meal_type, log_date)

            db.execute(
                "DELETE FROM order_consumers WHERE order_id IN (SELECT id FROM orders WHERE daily_log_id = ?)",
                (log_id,),
            )
            db.execute("DELETE FROM orders WHERE daily_log_id = ?", (log_id,))

            inserted_count = 0
            for item in orders:
                try:
                    item_name = str(item["item_name"]).strip()
                    actual_price = float(item["actual_price"])
                    payer_user_id = int(item["payer_user_id"])
                    split_type = str(item["split_type"])
                    consumer_ids = [int(x) for x in item["consumer_ids"]]
                    catalog_item_id = (
                        int(item["catalog_item_id"])
                        if item.get("catalog_item_id")
                        else None
                    )
                except (KeyError, TypeError, ValueError):
                    continue

                if split_type not in {"equal", "individual"}:
                    continue
                if not item_name or actual_price <= 0 or not consumer_ids:
                    continue

                if split_type == "individual":
                    consumer_ids = [consumer_ids[0]]
                else:
                    consumer_ids = sorted(set(consumer_ids))

                order_cursor = db.execute(
                    """
                    INSERT INTO orders (daily_log_id, catalog_item_id, item_name, actual_price, payer_user_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (log_id, catalog_item_id, item_name, actual_price, payer_user_id),
                )
                order_id = order_cursor.lastrowid

                share = round(actual_price / len(consumer_ids), 2)
                remainder = round(actual_price - (share * len(consumer_ids)), 2)

                for idx, consumer_id in enumerate(consumer_ids):
                    user_share = share + (remainder if idx == 0 else 0)
                    db.execute(
                        """
                        INSERT INTO order_consumers (order_id, user_id, share_amount)
                        VALUES (?, ?, ?)
                        """,
                        (order_id, consumer_id, user_share),
                    )

                inserted_count += 1

            db.commit()
            if inserted_count == 0:
                flash("No valid item rows were submitted.", "danger")
                return redirect(url_for("new_log"))

            flash("Meal log saved successfully.", "success")
            return redirect(url_for("dashboard"))

        return render_template(
            "new_log.html",
            users=users,
            catalog_items=catalog_items,
            meal_types=MEAL_TYPES,
        )

    @app.route("/catalog", methods=["GET", "POST"])
    def catalog():
        db = get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            try:
                default_price = float(request.form.get("default_price", "0"))
            except ValueError:
                default_price = -1

            if not name or category not in CATEGORIES or default_price < 0:
                flash("Please provide valid item name, category, and default price.", "danger")
                return redirect(url_for("catalog"))

            db.execute(
                "INSERT INTO catalog_items (name, default_price, category) VALUES (?, ?, ?)",
                (name, default_price, category),
            )
            db.commit()
            flash("Catalog item added.", "success")
            return redirect(url_for("catalog"))

        items = db.execute(
            "SELECT id, name, default_price, category FROM catalog_items ORDER BY name"
        ).fetchall()
        return render_template("catalog.html", items=items, categories=CATEGORIES)

    @app.route("/catalog/<int:item_id>/update", methods=["POST"])
    def update_catalog_item(item_id: int):
        db = get_db()
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        try:
            default_price = float(request.form.get("default_price", "0"))
        except ValueError:
            default_price = -1

        if not name or category not in CATEGORIES or default_price < 0:
            flash("Invalid catalog item details.", "danger")
            return redirect(url_for("catalog"))

        db.execute(
            """
            UPDATE catalog_items
            SET name = ?, default_price = ?, category = ?
            WHERE id = ?
            """,
            (name, default_price, category, item_id),
        )
        db.commit()
        flash("Catalog item updated.", "success")
        return redirect(url_for("catalog"))

    @app.route("/catalog/<int:item_id>/delete", methods=["POST"])
    def delete_catalog_item(item_id: int):
        db = get_db()
        db.execute("DELETE FROM catalog_items WHERE id = ?", (item_id,))
        db.commit()
        flash("Catalog item deleted.", "success")
        return redirect(url_for("catalog"))

    @app.route("/history/<int:log_id>")
    def log_details(log_id: int):
        db = get_db()
        log_row = db.execute(
            "SELECT id, day_number, meal_type, log_date FROM daily_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        if log_row is None:
            flash("Log not found.", "danger")
            return redirect(url_for("dashboard"))

        orders = db.execute(
            """
            SELECT o.id, o.item_name, o.actual_price, payer.name AS payer_name
            FROM orders o
            JOIN users payer ON payer.id = o.payer_user_id
            WHERE o.daily_log_id = ?
            ORDER BY o.id
            """,
            (log_id,),
        ).fetchall()

        order_breakdown = []
        for order in orders:
            consumers = db.execute(
                """
                SELECT u.name, oc.share_amount
                FROM order_consumers oc
                JOIN users u ON u.id = oc.user_id
                WHERE oc.order_id = ?
                ORDER BY u.name
                """,
                (order["id"],),
            ).fetchall()
            order_breakdown.append({"order": order, "consumers": consumers})

        return render_template(
            "history_detail.html", log_row=log_row, order_breakdown=order_breakdown
        )

    init_db(app)
    return app


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def get_user_id_by_name(db: sqlite3.Connection, name: str) -> int:
    row = db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else 1


def upsert_daily_log(db: sqlite3.Connection, day_number: int, meal_type: str, log_date: str) -> int:
    existing = db.execute(
        "SELECT id FROM daily_logs WHERE day_number = ? AND meal_type = ?",
        (day_number, meal_type),
    ).fetchone()

    if existing:
        db.execute(
            "UPDATE daily_logs SET log_date = ? WHERE id = ?", (log_date, existing["id"])
        )
        return existing["id"]

    cursor = db.execute(
        "INSERT INTO daily_logs (day_number, meal_type, log_date) VALUES (?, ?, ?)",
        (day_number, meal_type, log_date),
    )
    return cursor.lastrowid


def init_db(app: Flask) -> None:
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS catalog_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                default_price REAL NOT NULL DEFAULT 0,
                category TEXT NOT NULL CHECK (category IN ('Sehri', 'Iftar', 'Misc'))
            );

            CREATE TABLE IF NOT EXISTS daily_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_number INTEGER NOT NULL CHECK (day_number BETWEEN 1 AND 30),
                meal_type TEXT NOT NULL CHECK (meal_type IN ('Sehri', 'Iftar')),
                log_date TEXT NOT NULL,
                UNIQUE(day_number, meal_type)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                daily_log_id INTEGER NOT NULL,
                catalog_item_id INTEGER,
                item_name TEXT NOT NULL,
                actual_price REAL NOT NULL CHECK (actual_price > 0),
                payer_user_id INTEGER NOT NULL,
                FOREIGN KEY (daily_log_id) REFERENCES daily_logs(id) ON DELETE CASCADE,
                FOREIGN KEY (catalog_item_id) REFERENCES catalog_items(id) ON DELETE SET NULL,
                FOREIGN KEY (payer_user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS order_consumers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                share_amount REAL NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )

        for user_name in GROUP_USERS:
            db.execute("INSERT OR IGNORE INTO users (name) VALUES (?)", (user_name,))

        default_catalog = [
            ("Chicken Biryani", 600, "Iftar"),
            ("Samosa", 30, "Iftar"),
            ("Paratha", 25, "Sehri"),
            ("Water", 100, "Misc"),
            ("Delivery Fee", 150, "Misc"),
        ]
        db.executemany(
            "INSERT OR IGNORE INTO catalog_items (name, default_price, category) VALUES (?, ?, ?)",
            default_catalog,
        )
        db.commit()


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
