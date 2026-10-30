
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from pathlib import Path
from datetime import date, datetime, timedelta
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv
import sqlite3
import os
import re
import json
import base64
import mimetypes
import logging

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "lastbite.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
load_dotenv(BASE_DIR / ".env")

app.secret_key = os.getenv("LASTBITE_SECRET", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
RECIPE_MODEL = os.getenv(
    "NVIDIA_MODEL",
    "nvidia/nemotron-3.5-super-vl-120b-a12b"
)

# Try the smaller vision model first.
# If NVIDIA returns 404, automatically try the 90B vision model.
VISION_MODELS = [
    os.getenv("NVIDIA_VISION_MODEL", "meta/llama-3.2-90b-vision-instruct")
]

# Remove duplicate model names.
VISION_MODELS = list(dict.fromkeys(VISION_MODELS))

print("\n========== LASTBITE CONFIGURATION ==========")
print("Vision models:", VISION_MODELS)
print("Recipe model:", RECIPE_MODEL)
print("NVIDIA API key configured:", bool(NVIDIA_API_KEY))
print("============================================\n")

client = None

if NVIDIA_API_KEY:
    client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY,
        timeout=120.0,
        max_retries=0
    )


def get_nvidia_client():
    if client is None:
        raise RuntimeError(
            "NVIDIA_API_KEY is missing. Check your .env file."
        )
    return client


# ============================================================
# DATABASE
# ============================================================

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


# ============================================================
# GENERAL HELPERS
# ============================================================

def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def item_status(item, today=None):
    today = today or date.today()

    if not item["expiry_date"] or not item["expiry_confirmed"]:
        return {
            "label": "Date needs confirmation",
            "key": "unknown",
            "days": None
        }

    try:
        expiry = date.fromisoformat(item["expiry_date"])
    except (ValueError, TypeError):
        return {
            "label": "Date needs confirmation",
            "key": "unknown",
            "days": None
        }

    days = (expiry - today).days

    if days < 0:
        return {
            "label": f"Past listed date · {abs(days)}d ago",
            "key": "past",
            "days": days
        }

    if days == 0:
        return {
            "label": "Listed date is today",
            "key": "today",
            "days": 0
        }

    if days <= 3:
        return {
            "label": f"Use soon · {days}d left",
            "key": "soon",
            "days": days
        }

    return {
        "label": f"{days}d left",
        "key": "ok",
        "days": days
    }


def all_items():
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT *
            FROM groceries
            ORDER BY
                CASE
                    WHEN expiry_date IS NULL OR expiry_confirmed = 0
                    THEN 1 ELSE 0
                END,
                expiry_date ASC,
                name COLLATE NOCASE ASC
        """).fetchall()

    return [dict(row) for row in rows]


@app.context_processor
def helpers():
    return {
        "item_status": item_status,
        "today_iso": date.today().isoformat()
    }


# ============================================================
# DATE EXTRACTION
# ============================================================

def parse_explicit_date(text):
    """
    Only accepts complete, unambiguous dates.
    Does not guess between DD/MM and MM/DD.
    """

    if not text:
        return None

    candidates = re.findall(
        r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]\d{4})(?!\d)",
        text
    )

    for raw in candidates:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return date.fromisoformat(raw).isoformat()

            first, second, year = map(
                int, re.split(r"[./-]", raw)
            )

            if first > 12 and second <= 12:
                return date(year, second, first).isoformat()

            if second > 12 and first <= 12:
                return date(year, first, second).isoformat()

            # Ambiguous date: do not guess.

        except (ValueError, TypeError):
            continue

    return None


def parse_labeled_expiry_date(text):
    if not text:
        return None

    expiry_label = re.compile(
        r"\b(EXP(?:IRY)?|EXPIRES|USE\s*BY)\b",
        re.IGNORECASE
    )

    date_pattern = re.compile(
        r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}[./-]\d{2}[./-]\d{4})(?!\d)"
    )

    lines = text.splitlines()

    for index, line in enumerate(lines):
        if not expiry_label.search(line):
            continue

        candidate_lines = [line]

        if index + 1 < len(lines):
            candidate_lines.append(lines[index + 1])

        for candidate_line in candidate_lines:
            for candidate in date_pattern.findall(candidate_line):
                parsed = parse_explicit_date(candidate)

                if parsed:
                    return parsed

    return None


# ============================================================
# OCR
# ============================================================

def read_image_text(path):
    """
    Extract printed text using Tesseract.
    OCR failure does not stop the rest of the application.
    """

    try:
        import pytesseract
        from PIL import Image, ImageOps, ImageEnhance, ImageFilter

        tesseract_path = Path(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )

        if tesseract_path.exists():
            pytesseract.pytesseract.tesseract_cmd = str(
                tesseract_path
            )

        with Image.open(path) as source:
            image = source.convert("RGB")

        image = image.resize(
            (image.width * 3, image.height * 3),
            Image.Resampling.LANCZOS
        )

        gray = ImageOps.grayscale(image)
        gray = ImageOps.autocontrast(gray)
        gray = ImageEnhance.Contrast(gray).enhance(1.8)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))

        threshold = gray.point(
            lambda pixel: 255 if pixel > 150 else 0
        )

        outputs = []

        for processed, config in (
            (gray, "--oem 3 --psm 6"),
            (threshold, "--oem 3 --psm 11")
        ):
            extracted = pytesseract.image_to_string(
                processed,
                config=config
            ).strip()

            if extracted and extracted not in outputs:
                outputs.append(extracted)

        combined = "\n\n".join(outputs)

        app.logger.info(
            "OCR extracted text: %s",
            combined[:4000]
        )

        return combined

    except Exception:
        app.logger.exception(
            "OCR failed. Continuing without OCR."
        )
        return ""


# ============================================================
# NVIDIA ERROR HELPER
# ============================================================

def print_nvidia_error(error, model_name):
    app.logger.error(
        "NVIDIA request failed using model %s: %r",
        model_name,
        error
    )

    print("\n========== NVIDIA API ERROR ==========")
    print("Model:", model_name)
    print("Error:", repr(error))

    response = getattr(error, "response", None)

    if response is not None:
        try:
            print("Status:", response.status_code)
            print("Response body:", response.text)
        except Exception:
            pass

    print("======================================\n")


# ============================================================
# NVIDIA VISION SCANNER
# ============================================================

def scan_product_with_ai(image_path):
    """
    Identify a food product using NVIDIA vision models.

    First attempts the 11B model.
    If that returns 404, attempts the 90B model.

    Never invents an expiry date.
    """

    nvidia = get_nvidia_client()
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(
            f"Image does not exist: {image_path}"
        )

    if image_path.stat().st_size == 0:
        raise ValueError("Uploaded image is empty.")

    extension = image_path.suffix.lower()

    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp"
    }

    mime_type = mime_types.get(extension)

    if not mime_type:
        mime_type = mimetypes.guess_type(
            image_path.name
        )[0]

    if mime_type not in (
        "image/jpeg",
        "image/png",
        "image/webp"
    ):
        raise ValueError(
            f"Unsupported image format: {mime_type}"
        )

    image_bytes = image_path.read_bytes()

    image_base64 = base64.b64encode(
        image_bytes
    ).decode("utf-8")

    image_url = (
        f"data:{mime_type};base64,{image_base64}"
    )

    system_prompt = """
You are LastBite's food-package label reader.

Inspect the entire food package image.

Identify:
- Product name
- Quantity
- Unit
- Food category
- Clearly printed date labels

Return exactly one JSON object:

{
    "name": null,
    "quantity": null,
    "unit": "item",
    "category": "Other",
    "expiry_date": null,
    "date_type": "unknown",
    "confidence": "low"
}

Allowed date_type values:
expiry, best_before, manufactured, unknown.

STRICT DATE RULES:

1. Only populate expiry_date when an explicit EXP,
   EXPIRY, EXPIRES or USE BY date is clearly visible.

2. Never treat MFG, MFD or PKD as expiry dates.

3. Never calculate an expiry date from a manufacturing
   date or a duration.

4. Never guess missing digits or years.

5. If the date is unclear, return null.

6. If the date format is ambiguous, return null.

7. Use YYYY-MM-DD only when the complete date is clear.

8. BEST BEFORE is not the same as EXPIRY.
   Set date_type to best_before and expiry_date to null.

9. Return JSON only. No Markdown or explanations.

Confidence must be high, medium or low.
"""

    user_prompt = """
Read this food package carefully.

Identify the product name and visible quantity.
Inspect small text near package seals and edges.
Look for EXP, EXPIRY, EXPIRES and USE BY labels.
Distinguish these from manufacturing and best-before dates.

Return the requested JSON object only.
"""

    last_error = None

    for model_name in VISION_MODELS:

        try:
            app.logger.info(
                "Trying NVIDIA vision model: %s",
                model_name
            )

            response = nvidia.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_url
                                }
                            }
                        ]
                    }
                ],
                temperature=0.1,
                max_tokens=1200
            )

            raw = response.choices[0].message.content

            if not raw:
                raise ValueError(
                    "Vision model returned an empty response."
                )

            raw = raw.strip()

            app.logger.info(
                "Vision response from %s: %s",
                model_name,
                raw[:2000]
            )

            # Remove Markdown code fences if present.
            cleaned = raw

            if cleaned.startswith("```"):
                lines = cleaned.splitlines()

                if lines and lines[0].startswith("```"):
                    lines = lines[1:]

                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]

                cleaned = "\n".join(lines).strip()

            start = cleaned.find("{")
            end = cleaned.rfind("}")

            if start < 0 or end <= start:
                raise ValueError(
                    "Vision model did not return a JSON object."
                )

            result = json.loads(
                cleaned[start:end + 1]
            )

            if not isinstance(result, dict):
                raise ValueError(
                    "Invalid vision model response."
                )

            defaults = {
                "name": None,
                "quantity": None,
                "unit": "item",
                "category": "Other",
                "expiry_date": None,
                "date_type": "unknown",
                "confidence": "low"
            }

            for key, value in defaults.items():
                result.setdefault(key, value)

            # Validate date strictly.
            if (
                result.get("date_type") != "expiry"
                or not result.get("expiry_date")
            ):
                result["expiry_date"] = None

            else:
                try:
                    result["expiry_date"] = date.fromisoformat(
                        str(result["expiry_date"])
                    ).isoformat()

                except (TypeError, ValueError):
                    result["expiry_date"] = None
                    result["date_type"] = "unknown"

            category_map = {
                "fruit": "Fruits",
                "fruits": "Fruits",
                "vegetable": "Vegetables",
                "vegetables": "Vegetables",
                "dairy": "Dairy",
                "grain": "Grains",
                "grains": "Grains",
                "bakery": "Bakery",
                "frozen": "Frozen",
                "meat": "Meat & Seafood",
                "seafood": "Meat & Seafood",
                "meat & seafood": "Meat & Seafood"
            }

            result["category"] = category_map.get(
                str(result.get("category") or "Other")
                .strip()
                .lower(),
                "Other"
            )

            if result.get("name") is not None:
                result["name"] = str(
                    result["name"]
                ).strip()[:120]

            result["_model_used"] = model_name

            return result

        except Exception as error:
            last_error = error
            print_nvidia_error(error, model_name)

            # Only try the next model for a 404.
            # Other errors should be reported directly.
            if getattr(error, "status_code", None) != 404:
                raise

            app.logger.warning(
                "Model %s returned 404. Trying next vision model.",
                model_name
            )

    raise RuntimeError(
        "All configured NVIDIA vision models failed. "
        f"Last error: {last_error}"
    )


# ============================================================
# AI RECIPE RESCUE
# ============================================================

@app.route("/api/generate-recipe", methods=["POST"])
def generate_recipe():

    data = request.get_json(silent=True) or {}
    ingredients = data.get("ingredients", [])

    if not isinstance(ingredients, list):
        return jsonify({
            "error": "Ingredients must be a list."
        }), 400

    ingredients = [
        str(item).strip()[:80]
        for item in ingredients[:30]
        if str(item).strip()
    ]

    if not ingredients:
        return jsonify({
            "error": "Please provide ingredients."
        }), 400

    try:
        nvidia = get_nvidia_client()

        if not RECIPE_MODEL:
            raise RuntimeError(
                "NVIDIA_MODEL is missing from your .env file."
            )

        response = nvidia.chat.completions.create(
            model=RECIPE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": """
You are LastBite, a food-waste reduction assistant.

Create one practical recipe using the provided ingredients.

Return ONLY valid JSON in this structure:

{
    "name": "Recipe name",
    "description": "Short description",
    "cooking_time": "20 minutes",
    "servings": 2,
    "ingredients": [
        "1 cup cooked rice",
        "1 carrot"
    ],
    "steps": [
        "Wash and chop the vegetables.",
        "Heat oil in a pan."
    ],
    "storage_advice": "Store leftovers in a covered container in the refrigerator."
}

Rules:
- No Markdown or code fences.
- Do not write outside the JSON object.
- Include quantities wherever possible.
- Give clear beginner-friendly cooking instructions.
- Use the available ingredients as the main ingredients.
- Do not invent unavailable ingredients except basic seasonings.
- Never recommend using spoiled, moldy or unsafe food.
- Do not assume food is safe based only on its date.
"""
                },
                {
                    "role": "user",
                    "content": (
                        "Create a food rescue recipe using: "
                        + ", ".join(ingredients)
                    )
                }
            ],
            temperature=0.3,
            max_tokens=2048
        )

        raw = response.choices[0].message.content

        if not raw:
            raise ValueError("AI returned no recipe.")

        cleaned = raw.strip()

        if cleaned.startswith("```"):
            lines = cleaned.splitlines()

            if lines and lines[0].startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            cleaned = "\n".join(lines).strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start < 0 or end <= start:
            raise ValueError(
                "AI did not return valid JSON."
            )

        recipe = json.loads(cleaned[start:end + 1])

        required_fields = [
            "name",
            "description",
            "cooking_time",
            "servings",
            "ingredients",
            "steps",
            "storage_advice"
        ]

        for field in required_fields:
            if field not in recipe:
                raise ValueError(
                    f"Missing recipe field: {field}"
                )

        if (
            not isinstance(recipe["ingredients"], list)
            or not isinstance(recipe["steps"], list)
            or not recipe["ingredients"]
            or not recipe["steps"]
        ):
            raise ValueError("Recipe is incomplete.")

        return jsonify({"recipe": recipe})

    except Exception as error:
        app.logger.exception(
            "NVIDIA recipe generation failed."
        )

        return jsonify({
            "error": str(error)
        }), 502


# ============================================================
# HOME PAGE / MY FRIDGE
# ============================================================

@app.route("/")
def index():

    items = all_items()
    today = date.today()

    annotated = []

    for item in items:
        item["status"] = item_status(item, today)
        annotated.append(item)

    search = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "All")

    if search:
        annotated = [
            item for item in annotated
            if search in item["name"].lower()
        ]

    if category != "All":
        annotated = [
            item for item in annotated
            if item["category"] == category
        ]

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

        if 0 <= days <= reminder_days:
            reminders.append({
                "item": item,
                "days": days
            })

    all_groceries = all_items()

    return render_template(
        "index.html",
        items=annotated,
        total=len(all_groceries),
        attention=sum(
            1 for item in all_groceries
            if item_status(item)["key"] in {
                "unknown", "past", "today", "soon"
            }
        ),
        reminders=reminders,
        categories=[
            "All",
            "Fruits",
            "Vegetables",
            "Dairy",
            "Grains",
            "Meat & Seafood",
            "Bakery",
            "Frozen",
            "Other"
        ],
        selected_category=category,
        search_query=request.args.get("q", "")
    )


# ============================================================
# SMARTSCAN
# ============================================================

@app.route("/scan", methods=["GET", "POST"])
def scan():

    extracted = {
        "name": "",
        "expiry_date": "",
        "ocr_text": "",
        "image_filename": "",
        "quantity": 1,
        "unit": "item",
        "category": "Other"
    }

    if request.method == "GET":
        return render_template(
            "scan.html",
            extracted=extracted,
            scan_note=None
        )

    uploaded_photos = [
        photo for photo in request.files.getlist("photos")
        if photo and photo.filename
    ]

    if not uploaded_photos:
        flash("Please add at least one photo.", "error")
        return redirect(url_for("scan"))

    if len(uploaded_photos) > 5:
        flash("Maximum 5 photos allowed.", "error")
        return redirect(url_for("scan"))

    saved_paths = []

    try:
        # Save main photos.
        for photo in uploaded_photos:

            if not allowed_file(photo.filename):
                flash(
                    "Only JPG, PNG or WEBP images are allowed.",
                    "error"
                )
                return redirect(url_for("scan"))

            safe_name = secure_filename(photo.filename)

            unique_name = (
                f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_"
                f"{safe_name}"
            )

            image_path = UPLOAD_DIR / unique_name
            photo.save(image_path)
            saved_paths.append(image_path)

        # Save optional date close-up.
        date_photo = request.files.get("date_photo")

        if date_photo and date_photo.filename:

            if not allowed_file(date_photo.filename):
                flash(
                    "Date photo must be JPG, PNG or WEBP.",
                    "error"
                )
                return redirect(url_for("scan"))

            safe_name = secure_filename(date_photo.filename)

            date_name = (
                f"date_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_"
                f"{safe_name}"
            )

            date_path = UPLOAD_DIR / date_name
            date_photo.save(date_path)
            saved_paths.append(date_path)

        # ----------------------------------------------------
        # VISION SCAN
        # ----------------------------------------------------

        ai_result = {}
        ai_errors = []

        for image_path in saved_paths:

            try:
                result = scan_product_with_ai(image_path)

                for field in (
                    "name",
                    "quantity",
                    "unit",
                    "category"
                ):
                    if (
                        ai_result.get(field) in (None, "")
                        and result.get(field) not in (None, "")
                    ):
                        ai_result[field] = result[field]

                if (
                    not ai_result.get("expiry_date")
                    and result.get("date_type") == "expiry"
                    and result.get("expiry_date")
                ):
                    ai_result["expiry_date"] = result["expiry_date"]
                    ai_result["date_type"] = "expiry"

            except Exception as error:
                app.logger.exception(
                    "AI scanning failed for %s",
                    image_path.name
                )

                ai_errors.append(str(error))

        # ----------------------------------------------------
        # OCR
        # ----------------------------------------------------

        ocr_parts = []

        for image_path in saved_paths:
            text = read_image_text(image_path)

            if text:
                ocr_parts.append(text)

        ocr_text = "\n\n".join(ocr_parts)

        # ----------------------------------------------------
        # DATE DETECTION
        # ----------------------------------------------------

        detected_date = parse_labeled_expiry_date(ocr_text)

        ai_date = ai_result.get("expiry_date")

        if ai_date:
            try:
                detected_date = date.fromisoformat(
                    str(ai_date)
                ).isoformat()
            except ValueError:
                pass

        # ----------------------------------------------------
        # PREPARE RESULTS
        # ----------------------------------------------------

        extracted.update({
            "name": str(
                ai_result.get("name") or ""
            )[:120],

            "expiry_date": detected_date or "",
            "ocr_text": ocr_text,
            "image_filename": saved_paths[0].name,
            "quantity": ai_result.get("quantity") or 1,
            "unit": ai_result.get("unit") or "item",
            "category": ai_result.get("category") or "Other"
        })

        # ----------------------------------------------------
        # USER MESSAGE
        # ----------------------------------------------------

        if ai_errors and not ai_result.get("name"):
            scan_note = (
                "AI product recognition failed. "
                "You can still enter the product details manually. "
                "Check the VS Code terminal for the technical error."
            )

        elif not detected_date:
            scan_note = (
                "Product scan completed. No unambiguous EXP/USE BY "
                "date was detected. Check the package and enter "
                "the date yourself if it is readable."
            )

        else:
            scan_note = (
                "Scan completed. Please verify the product and "
                "printed date against the package before saving."
            )

        return render_template(
            "scan.html",
            extracted=extracted,
            scan_note=scan_note
        )

    except Exception:
        app.logger.exception("Unexpected scanner error.")

        flash(
            "The scanner encountered an error. "
            "Please try again or enter the details manually.",
            "error"
        )

        return redirect(url_for("scan"))


# ============================================================
# SAVE SCANNED PRODUCT
# ============================================================

@app.route("/save-scan", methods=["POST"])
def save_scan():

    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()

    confirmed = (
        request.form.get("expiry_confirmed") == "yes"
        and bool(raw_expiry)
    )

    try:
        quantity = float(request.form.get("quantity", "1"))

        if quantity <= 0:
            raise ValueError

    except ValueError:
        flash(
            "Quantity must be a number greater than zero.",
            "error"
        )
        return redirect(url_for("scan"))

    expiry = None

    if raw_expiry and confirmed:
        try:
            expiry = date.fromisoformat(raw_expiry).isoformat()
        except ValueError:
            flash("Please choose a valid date.", "error")
            return redirect(url_for("scan"))

    if not name:
        flash("Please enter the product name.", "error")
        return redirect(url_for("scan"))

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO groceries
            (
                name, quantity, unit, category,
                expiry_date, expiry_confirmed,
                image_filename, reminder_days, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (
            name,
            quantity,
            request.form.get("unit", "item"),
            request.form.get("category", "Other"),
            expiry,
            int(bool(expiry and confirmed)),
            request.form.get("image_filename", "").strip() or None,
            datetime.now().isoformat(timespec="seconds")
        ))

    flash(f"{name} added to My Fridge.", "success")
    return redirect(url_for("index"))


# ============================================================
# MANUALLY ADD PRODUCT
# ============================================================

@app.route("/add", methods=["POST"])
def add_item():

    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()

    confirmed = (
        request.form.get("expiry_confirmed") == "yes"
        and bool(raw_expiry)
    )

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
            flash("Please enter a valid date.", "error")
            return redirect(url_for("index"))

    if not name:
        flash("Enter a product name.", "error")
        return redirect(url_for("index"))

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO groceries
            (
                name, quantity, unit, category,
                expiry_date, expiry_confirmed,
                reminder_days, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """, (
            name,
            quantity,
            request.form.get("unit", "item"),
            request.form.get("category", "Other"),
            expiry,
            int(bool(expiry and confirmed)),
            datetime.now().isoformat(timespec="seconds")
        ))

    flash(f"{name} added to My Fridge.", "success")
    return redirect(url_for("index"))


# ============================================================
# EDIT PRODUCT
# ============================================================

@app.route("/item/<int:item_id>/update", methods=["POST"])
def update_item(item_id):

    name = request.form.get("name", "").strip()
    raw_expiry = request.form.get("expiry_date", "").strip()

    confirmed = (
        request.form.get("expiry_confirmed") == "yes"
        and bool(raw_expiry)
    )

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

    if not name:
        flash("Product name cannot be empty.", "error")
        return redirect(url_for("index"))

    try:
        reminder_days = int(
            request.form.get("reminder_days", "1")
        )
        reminder_days = max(0, min(reminder_days, 365))
    except ValueError:
        reminder_days = 1

    with connect_db() as conn:
        conn.execute("""
            UPDATE groceries
            SET
                name=?,
                quantity=?,
                unit=?,
                category=?,
                expiry_date=?,
                expiry_confirmed=?,
                reminder_days=?
            WHERE id=?
        """, (
            name,
            quantity,
            request.form.get("unit", "item"),
            request.form.get("category", "Other"),
            expiry,
            int(bool(expiry and confirmed)),
            reminder_days,
            item_id
        ))

    flash(
        "Product updated. Reminder schedule recalculated.",
        "success"
    )
    return redirect(url_for("index"))


# ============================================================
# DELETE PRODUCT
# ============================================================

@app.route("/item/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):

    with connect_db() as conn:
        row = conn.execute(
            "SELECT image_filename FROM groceries WHERE id=?",
            (item_id,)
        ).fetchone()

        conn.execute(
            "DELETE FROM groceries WHERE id=?",
            (item_id,)
        )

    if row and row["image_filename"]:
        try:
            (UPLOAD_DIR / row["image_filename"]).unlink(
                missing_ok=True
            )
        except OSError:
            pass

    flash("Product removed from My Fridge.", "success")
    return redirect(url_for("index"))


# ============================================================
# DEMO DATA
# ============================================================

@app.route("/seed-demo", methods=["POST"])
def seed_demo():

    today = date.today()

    samples = [
        ("Milk", 1, "litre", "Dairy",
         today + timedelta(days=1), 1, 1),

        ("Spinach", 1, "bunch", "Vegetables",
         today + timedelta(days=2), 1, 1),

        ("Rice", 1, "packet", "Grains",
         today + timedelta(days=180), 1, 3),

        ("Yogurt", 2, "cups", "Dairy",
         today - timedelta(days=1), 1, 1),

        ("Mystery leftovers", 1, "container", "Other",
         None, 0, 1)
    ]

    with connect_db() as conn:
        for (
            name, qty, unit, category,
            exp, confirmed, reminder
        ) in samples:

            exists = conn.execute(
                "SELECT id FROM groceries WHERE name=?",
                (name,)
            ).fetchone()

            if not exists:
                conn.execute("""
                    INSERT INTO groceries
                    (
                        name, quantity, unit, category,
                        expiry_date, expiry_confirmed,
                        reminder_days, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    name,
                    qty,
                    unit,
                    category,
                    exp.isoformat() if exp else None,
                    confirmed,
                    reminder,
                    datetime.now().isoformat(timespec="seconds")
                ))

    flash(
        "Demo products added. The mystery item has no guessed date.",
        "success"
    )
    return redirect(url_for("index"))


# ============================================================
# CLEAR PRODUCTS
# ============================================================

@app.route("/clear-demo", methods=["POST"])
def clear_all():

    with connect_db() as conn:
        rows = conn.execute(
            "SELECT image_filename FROM groceries"
        ).fetchall()

        conn.execute("DELETE FROM groceries")

    for row in rows:
        if row["image_filename"]:
            try:
                (UPLOAD_DIR / row["image_filename"]).unlink(
                    missing_ok=True
                )
            except OSError:
                pass

    flash("All fridge items cleared.", "success")
    return redirect(url_for("index"))


# ============================================================
# PWA
# ============================================================

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


# ============================================================
# START APPLICATION
# ============================================================

init_db()

if __name__ == "__main__":
    app.run(
        debug=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
