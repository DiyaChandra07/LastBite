from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from pathlib import Path
from datetime import date, datetime, timedelta
from werkzeug.utils import secure_filename
import sqlite3
import os
import re

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "lastbite.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.secret_key = os.environ.get("LASTBITE_SECRET", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groceries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 1,
                unit TEXT NOT NULL DEFAULT 'item',
                category TEXT NOT NULL DEFAULT 'Other',
                expiry_date TEXT,
                expiry_confirmed INTEGER NOT NULL DEFAULT 0,
                image_filename TEXT,
                reminder_days INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_explicit_date(text):
    """Extract only unambiguous numeric ISO or day/month/year dates.
    Returns a date string only for a complete, valid date; never infers a year."""
    if not text:
        return None
    candidates = re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]\d{4})(?!\d)", text)
    for raw in candidates:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return date.fromisoformat(raw).isoformat()
            # Ambiguous day/month ordering: do not guess.
            parts = re.split(r"[./-]", raw)
            first, second, year = map(int, parts)
            if first > 12 and second <= 12:
                return date(year, second, first).isoformat()  # DD/MM/YYYY is clear
            if second > 12 and first <= 12:
                return date(year, first, second).isoformat()  # MM/DD/YYYY is clear
            # Both <= 12: ambiguous; leave unfilled for user confirmation.
        except (ValueError, TypeError):
            continue
    return None


def read_image_text(path):
    """Optional OCR. If Tesseract or pytesseract isn't installed, keep manual review available."""
    try:
        import pytesseract
        from PIL import Image
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return ""


def item_status(item, today=None):
    today = today or date.today()
    if not item["expiry_date"] or not item["expiry_confirmed"]:
        return {"label": "Date needs confirmation", "key": "unknown", "days": None}
    try:
        expiry = date.fromisoformat(item["expiry_date"])
    except ValueError:
        return {"label": "Date needs confirmation", "key": "unknown", "days": None}
    days = (expiry - today).days
    if days < 0:
        return {"label": f"Past listed date · {abs(days)}d ago", "key": "past", "days": days}
    if days == 0:
        return {"label": "Listed date is today", "key": "today", "days": 0}
    if days <= 3:
        return {"label": f"Use soon · {days}d left", "key": "soon", "days": days}
    return {"label": f"{days}d left", "key": "ok", "days": days}


def all_items():
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT * FROM groceries
            ORDER BY CASE WHEN expiry_date IS NULL OR expiry_confirmed = 0 THEN 1 ELSE 0 END,
                     expiry_date ASC, name COLLATE NOCASE ASC
        """).fetchall()
    return [dict(row) for row in rows]


@app.context_processor
def helpers():
    return {"item_status": item_status, "today_iso": date.today().isoformat()}


@app.route("/")
def index():
    items = all_items()
    annotated = []
    today = date.today()
    for item in items:
        item["status"] = item_status(item, today)
        annotated.append(item)
    search = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "All")
    if search:
        annotated = [i for i in annotated if search in i["name"].lower()]
    if category != "All":
        annotated = [i for i in annotated if i["category"] == category]
    reminders = []
    for item in all_items():
        if not item["expiry_date"] or not item["expiry_confirmed"]:
            continue
        try:
            exp = date.fromisoformat(item["expiry_date"])
        except ValueError:
            continue
        days = (exp - today).days
        reminder_days = int(item["reminder_days"])
        if days < 0:
            continue
        if days == 0 or days <= reminder_days:
            reminders.append({"item": item, "days": days})
    return render_template(
        "index.html",
        items=annotated,
        total=len(all_items()),
        attention=sum(1 for i in all_items() if item_status(i)["key"] in {"unknown", "past", "today", "soon"}),
        reminders=reminders,
        categories=["All", "Fruits", "Vegetables", "Dairy", "Grains", "Meat & Seafood", "Bakery", "Frozen", "Other"],
        selected_category=category,
        search_query=request.args.get("q", ""),
    )


@app.route("/scan", methods=["GET", "POST"])
def scan():
    extracted = {"name": "", "expiry_date": "", "ocr_text": "", "image_filename": ""}
    scan_note = None
    if request.method == "POST":
        photo = request.files.get("photo")
        if not photo or not photo.filename:
            flash("Choose a product photo first.", "error")
            return redirect(url_for("scan"))
        if not allowed_file(photo.filename):
            flash("Please upload a JPG, PNG or WEBP image.", "error")
            return redirect(url_for("scan"))
        filename = secure_filename(photo.filename)
        unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{filename}"
        path = UPLOAD_DIR / unique_name
        photo.save(path)
        ocr_text = read_image_text(path)
        detected_date = parse_explicit_date(ocr_text)
        extracted.update({
            "ocr_text": ocr_text,
            "expiry_date": detected_date or "",
            "image_filename": unique_name,
        })
        if detected_date:
            scan_note = "A complete date was detected. Verify it against the package before saving."
        elif ocr_text:
            scan_note = "Text was read, but no unambiguous complete date was found. Enter the date yourself only if you can read it on the package."
        else:
            scan_note = "Automatic text reading is unavailable or found no readable text. You can still review the photo and enter details manually."
        return render_template("scan.html", extracted=extracted, scan_note=scan_note)
    return render_template("scan.html", extracted=extracted, scan_note=None)


@app.route("/save-scan", methods=["POST"])
def save_scan():
    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()
    confirmed = request.form.get("expiry_confirmed") == "yes" and bool(raw_expiry)
    quantity_raw = request.form.get("quantity", "1")
    category = request.form.get("category", "Other")
    unit = request.form.get("unit", "item")
    image_filename = request.form.get("image_filename", "").strip()
    try:
        quantity = float(quantity_raw)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be a number greater than zero.", "error")
        return redirect(url_for("scan"))
    expiry = None
    if raw_expiry and confirmed:
        try:
            expiry = date.fromisoformat(raw_expiry).isoformat()
        except ValueError:
            flash("Please choose a valid date.", "error")
            return redirect(url_for("scan"))
    # A date is never treated as confirmed merely because OCR returned it.
    if not name:
        flash("Please enter the product name.", "error")
        return redirect(url_for("scan"))
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO groceries
            (name, quantity, unit, category, expiry_date, expiry_confirmed, image_filename, reminder_days, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (name, quantity, unit, category, expiry, int(bool(expiry and confirmed)), image_filename or None, datetime.now().isoformat(timespec="seconds")))
    flash(f"{name} added to My Fridge.", "success")
    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
def add_item():
    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()
    expiry_confirmed = request.form.get("expiry_confirmed") == "yes" and bool(raw_expiry)
    try:
        quantity = float(request.form.get("quantity", "1"))
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be greater than zero.", "error")
        return redirect(url_for("index"))
    expiry = None
    if raw_expiry and expiry_confirmed:
        try:
            expiry = date.fromisoformat(raw_expiry).isoformat()
        except ValueError:
            flash("Please enter a valid date.", "error")
            return redirect(url_for("index"))
    if not name:
        flash("Enter a product name.", "error")
        return redirect(url_for("index"))
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO groceries
            (name, quantity, unit, category, expiry_date, expiry_confirmed, reminder_days, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, (name, quantity, request.form.get("unit", "item"), request.form.get("category", "Other"),
              expiry, int(bool(expiry and expiry_confirmed)), datetime.now().isoformat(timespec="seconds")))
    flash(f"{name} added to My Fridge.", "success")
    return redirect(url_for("index"))


@app.route("/item/<int:item_id>/update", methods=["POST"])
def update_item(item_id):
    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()
    confirmed = request.form.get("expiry_confirmed") == "yes" and bool(raw_expiry)
    try:
        quantity = float(request.form.get("quantity", "1"))
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be greater than zero.", "error")
        return redirect(url_for("index"))
    expiry = None
    if raw_expiry and confirmed:
        try:
            expiry = date.fromisoformat(raw_expiry).isoformat()
        except ValueError:
            flash("Please choose a valid date.", "error")
            return redirect(url_for("index"))
    with connect_db() as conn:
        conn.execute("""
            UPDATE groceries SET name=?, quantity=?, unit=?, category=?, expiry_date=?, expiry_confirmed=?, reminder_days=?
            WHERE id=?
        """, (name, quantity, request.form.get("unit", "item"), request.form.get("category", "Other"),
              expiry, int(bool(expiry and confirmed)), int(request.form.get("reminder_days", "1")), item_id))
    flash("Product updated. Reminder schedule recalculated.", "success")
    return redirect(url_for("index"))


@app.route("/item/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    with connect_db() as conn:
        row = conn.execute("SELECT image_filename FROM groceries WHERE id=?", (item_id,)).fetchone()
        conn.execute("DELETE FROM groceries WHERE id=?", (item_id,))
    if row and row["image_filename"]:
        try:
            (UPLOAD_DIR / row["image_filename"]).unlink(missing_ok=True)
        except OSError:
            pass
    flash("Product removed from My Fridge.", "success")
    return redirect(url_for("index"))


@app.route("/seed-demo", methods=["POST"])
def seed_demo():
    today = date.today()
    samples = [
        ("Milk", 1, "litre", "Dairy", today + timedelta(days=1), 1, 1),
        ("Spinach", 1, "bunch", "Vegetables", today + timedelta(days=2), 1, 1),
        ("Rice", 1, "packet", "Grains", today + timedelta(days=180), 1, 3),
        ("Yogurt", 2, "cups", "Dairy", today - timedelta(days=1), 1, 1),
        ("Mystery leftovers", 1, "container", "Other", None, 0, 1),
    ]
    with connect_db() as conn:
        for name, qty, unit, category, exp, confirmed, reminder in samples:
            exists = conn.execute("SELECT id FROM groceries WHERE name=?", (name,)).fetchone()
            if not exists:
                conn.execute("""
                    INSERT INTO groceries
                    (name, quantity, unit, category, expiry_date, expiry_confirmed, reminder_days, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, qty, unit, category, exp.isoformat() if exp else None, confirmed, reminder, datetime.now().isoformat(timespec="seconds")))
    flash("Demo products added. The mystery item intentionally has no guessed expiry date.", "success")
    return redirect(url_for("index"))


@app.route("/clear-demo", methods=["POST"])
def clear_all():
    with connect_db() as conn:
        rows = conn.execute("SELECT image_filename FROM groceries").fetchall()
        conn.execute("DELETE FROM groceries")
    for row in rows:
        if row["image_filename"]:
            try:
                (UPLOAD_DIR / row["image_filename"]).unlink(missing_ok=True)
            except OSError:
                pass
    flash("All fridge items cleared.", "success")
    return redirect(url_for("index"))


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "LastBite",
        "short_name": "LastBite",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f7f8f4",
        "theme_color": "#19765a",
        "icons": []
    })


@app.route("/service-worker.js")
def service_worker():
    return app.send_static_file("service-worker.js")


init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
