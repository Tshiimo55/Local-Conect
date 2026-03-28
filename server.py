import base64
import binascii
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOCALCONNECT_DATA_DIR") or BASE_DIR)
DB_PATH = DATA_DIR / "localconnect.db"
UPLOADS_DIR = BASE_DIR / "uploads"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "").strip()
ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
DEFAULT_RATING_DIST = [0, 0, 0, 0, 0]
ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_BUSINESS_IMAGES = 6


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=True)


def json_list(value, default=None):
    fallback = DEFAULT_RATING_DIST if default is None else default
    return json.loads(value or json_dumps(fallback))


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    check_hash, _ = hash_password(password, password_salt)
    return secrets.compare_digest(check_hash, password_hash)


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def allowed_origin(origin: str) -> str:
    if not origin:
        return ""
    if origin in ALLOWED_ORIGINS:
        return origin
    if FRONTEND_ORIGIN and origin == FRONTEND_ORIGIN:
        return origin
    if HOST in {"127.0.0.1", "localhost"} and (
        origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
    ):
        return origin
    return ""


def format_display_date() -> str:
    fmt = "%-d %b %Y" if os.name != "nt" else "%#d %b %Y"
    return datetime.now().strftime(fmt)


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, utc_now()),
    )
    return token


def get_user_by_email(conn: sqlite3.Connection, email: str):
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def business_images(conn: sqlite3.Connection, business_id: int):
    return conn.execute(
        """
        SELECT id, image_path, sort_order
        FROM business_images
        WHERE business_id = ?
        ORDER BY sort_order, id
        """,
        (business_id,),
    ).fetchall()


def image_public_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "url": "/" + (row["image_path"] or "").replace("\\", "/").lstrip("/"),
        "sortOrder": row["sort_order"],
    }


def delete_image_file(image_path: str):
    if not image_path:
        return
    full_path = BASE_DIR / image_path
    try:
        if full_path.exists():
            full_path.unlink()
    except OSError:
        pass


def save_business_images(conn: sqlite3.Connection, business_id: int, images: list[dict]):
    if not images:
        return []

    existing_count = conn.execute(
        "SELECT COUNT(*) AS count FROM business_images WHERE business_id = ?",
        (business_id,),
    ).fetchone()["count"]
    if existing_count + len(images) > MAX_BUSINESS_IMAGES:
        raise ValueError(f"You can upload up to {MAX_BUSINESS_IMAGES} business photos.")

    next_sort_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_sort FROM business_images WHERE business_id = ?",
        (business_id,),
    ).fetchone()["next_sort"]

    upload_dir = UPLOADS_DIR / "businesses" / str(business_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_rows = []

    for offset, image in enumerate(images):
        data_url = image.get("dataUrl") or ""
        if not data_url.startswith("data:") or ";base64," not in data_url:
            raise ValueError("One of the selected photos could not be processed.")

        mime_type = data_url[5:].split(";", 1)[0].strip().lower()
        ext = ALLOWED_IMAGE_MIME_TYPES.get(mime_type)
        if not ext:
            raise ValueError("Only JPG, PNG, WEBP, and GIF images are supported.")

        encoded = data_url.split(",", 1)[1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("One of the selected photos is invalid.")

        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Each photo must be 3 MB or smaller.")

        filename = f"{secrets.token_hex(12)}{ext}"
        relative_path = Path("uploads") / "businesses" / str(business_id) / filename
        (BASE_DIR / relative_path).write_bytes(content)
        cursor = conn.execute(
            """
            INSERT INTO business_images (business_id, image_path, sort_order, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (business_id, str(relative_path).replace("\\", "/"), next_sort_order + offset, utc_now()),
        )
        saved_rows.append(
            {
                "id": cursor.lastrowid,
                "image_path": str(relative_path).replace("\\", "/"),
                "sort_order": next_sort_order + offset,
            }
        )

    return saved_rows


def insert_seed_business(conn: sqlite3.Connection, business: dict, owner_user_id=None):
    linked_owner_id = owner_user_id if business["name"] == "Glam By Zanele" else None
    cursor = conn.execute(
        """
        INSERT INTO businesses (
            owner_user_id, name, category, city, province, phone, email, website,
            address, description, tags_json, featured, rating, review_count, rating_dist_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            linked_owner_id,
            business["name"],
            business["category"],
            business["city"],
            business["province"],
            business["phone"],
            business["email"],
            business["website"],
            business["address"],
            business["description"],
            json_dumps(business["tags"]),
            business["featured"],
            business["rating"],
            business["reviews"],
            json_dumps(business["rating_dist"]),
        ),
    )
    business_id = cursor.lastrowid
    if linked_owner_id:
        conn.execute("UPDATE users SET business_id = ? WHERE id = ?", (business_id, linked_owner_id))

    for sort_order, service in enumerate(business["services"]):
        conn.execute(
            "INSERT INTO services (business_id, sort_order, name, price) VALUES (?, ?, ?, ?)",
            (business_id, sort_order, service[0], service[1]),
        )

    for sort_order, hour in enumerate(business["hours"]):
        conn.execute(
            """
            INSERT INTO hours (business_id, sort_order, day_name, is_open, opens_at, closes_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (business_id, sort_order, hour[0], hour[1], hour[2], hour[3]),
        )

    for review in business["reviews_seed"]:
        conn.execute(
            """
            INSERT INTO reviews (business_id, user_id, reviewer_name, rating, review_text, helpful_count, created_at, display_date)
            VALUES (?, NULL, ?, ?, ?, 0, ?, ?)
            """,
            (business_id, review[0], review[1], review[2], utc_now(), review[3]),
        )


def user_public_dict(row: sqlite3.Row) -> dict:
    first = (row["first_name"] or "").strip()
    last = (row["last_name"] or "").strip()
    name = f"{first} {last}".strip() or row["name"]
    return {
        "id": row["id"],
        "name": name,
        "firstName": first,
        "lastName": last,
        "email": row["email"],
        "phone": row["phone"] or "",
        "province": row["province"] or "",
        "role": row["role"],
        "status": row["status"] or "active",
        "businessId": row["business_id"],
        "createdAt": row["created_at"],
    }


def business_summary_dict(row: sqlite3.Row) -> dict:
    cover = row["cover_image_path"] if "cover_image_path" in row.keys() else ""
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "city": row["city"],
        "province": row["province"],
        "rating": row["rating"],
        "reviews": row["review_count"],
        "featured": bool(row["featured"]),
        "tags": json_list(row["tags_json"], []),
        "coverImage": ("/" + cover.lstrip("/")) if cover else "",
    }


def business_detail_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    services = conn.execute(
        "SELECT name, price FROM services WHERE business_id = ? ORDER BY sort_order, id",
        (row["id"],),
    ).fetchall()
    hours = conn.execute(
        "SELECT day_name, is_open, opens_at, closes_at FROM hours WHERE business_id = ? ORDER BY sort_order, id",
        (row["id"],),
    ).fetchall()
    images = business_images(conn, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "city": row["city"],
        "province": row["province"],
        "phone": row["phone"] or "",
        "email": row["email"] or "",
        "website": row["website"] or "",
        "address": row["address"] or "",
        "description": row["description"] or "",
        "tags": json_list(row["tags_json"], []),
        "featured": bool(row["featured"]),
        "rating": row["rating"],
        "reviews": row["review_count"],
        "ratingDist": json_list(row["rating_dist_json"]),
        "images": [image_public_dict(image) for image in images],
        "services": [{"n": s["name"], "p": s["price"]} for s in services],
        "hours": [
            {
                "d": h["day_name"],
                "open": bool(h["is_open"]),
                "from": h["opens_at"] or "",
                "to": h["closes_at"] or "",
                "t": "Closed" if not h["is_open"] else f'{h["opens_at"]} - {h["closes_at"]}',
            }
            for h in hours
        ],
    }


def seed_database():
    conn = get_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            role TEXT NOT NULL,
            phone TEXT,
            province TEXT,
            status TEXT DEFAULT 'active',
            business_id INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            city TEXT NOT NULL,
            province TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            website TEXT,
            address TEXT,
            description TEXT,
            tags_json TEXT DEFAULT '[]',
            featured INTEGER DEFAULT 0,
            rating REAL DEFAULT 0,
            review_count INTEGER DEFAULT 0,
            rating_dist_json TEXT DEFAULT '[0,0,0,0,0]'
        );

        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,
            name TEXT NOT NULL,
            price TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,
            day_name TEXT NOT NULL,
            is_open INTEGER NOT NULL,
            opens_at TEXT,
            closes_at TEXT
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            user_id INTEGER,
            reviewer_name TEXT NOT NULL,
            rating INTEGER NOT NULL,
            review_text TEXT NOT NULL,
            helpful_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            display_date TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS business_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )

    existing = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    should_seed_users = existing == 0

    demo_users = [
        {
            "first_name": "Demo",
            "last_name": "Customer",
            "email": "customer@demo.com",
            "password": "Demo1234!",
            "role": "customer",
            "phone": "+27 82 000 0000",
            "province": "Gauteng",
            "status": "active",
        },
        {
            "first_name": "Demo",
            "last_name": "Business Owner",
            "email": "owner@demo.com",
            "password": "Demo1234!",
            "role": "business_owner",
            "phone": "+27 11 882 3340",
            "province": "Gauteng",
            "status": "active",
        },
        {
            "first_name": "Admin",
            "last_name": "User",
            "email": "admin@demo.com",
            "password": "Admin1234!",
            "role": "admin",
            "phone": "+27 11 500 1000",
            "province": "Gauteng",
            "status": "active",
        },
    ]

    if should_seed_users:
        for user in demo_users:
            password_hash, password_salt = hash_password(user["password"])
            name = f'{user["first_name"]} {user["last_name"]}'
            conn.execute(
                """
                INSERT INTO users (first_name, last_name, name, email, password_hash, password_salt, role, phone, province, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["first_name"],
                    user["last_name"],
                    name,
                    user["email"],
                    password_hash,
                    password_salt,
                    user["role"],
                    user["phone"],
                    user["province"],
                    user["status"],
                    utc_now(),
                ),
            )

    demo_businesses = [
        {
            "name": "Kingsmen Cuts",
            "category": "Barbers & Hair Salons",
            "city": "Soweto",
            "province": "Gauteng",
            "phone": "+27 11 534 7821",
            "email": "info@kingsmenscuts.co.za",
            "website": "kingsmenscuts.co.za",
            "address": "34 Vilakazi St, Soweto, 1804",
            "description": "Kingsmen Cuts is Soweto's premier barbershop with precision fades, braids, and grooming for all hair types.",
            "tags": ["Fade", "Braids", "Kids Cuts", "Beard Trim", "Colour", "Dreadlocks"],
            "featured": 1,
            "rating": 4.9,
            "reviews": 132,
            "rating_dist": [70, 20, 7, 2, 1],
            "services": [("Classic Fade", "R120"), ("Full Haircut", "R150"), ("Kids Cut", "R80"), ("Beard Trim", "R60")],
            "hours": [("Monday", 1, "08:00", "18:00"), ("Tuesday", 1, "08:00", "18:00"), ("Wednesday", 1, "08:00", "18:00"), ("Thursday", 1, "08:00", "19:00"), ("Friday", 1, "08:00", "19:00"), ("Saturday", 1, "07:00", "17:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Sipho Dlamini", 5, "Best barber in Soweto. The fade was immaculate and they were done in 25 minutes.", "10 Mar 2025"), ("Thabo Nkosi", 5, "Brought my son here for the first time and he left looking sharp.", "2 Mar 2025")],
        },
        {
            "name": "Bright Minds Tutoring",
            "category": "Tutors & Education",
            "city": "Pretoria",
            "province": "Gauteng",
            "phone": "+27 12 341 7729",
            "email": "info@brightminds.co.za",
            "website": "brightminds.co.za",
            "address": "67 Esselen St, Sunnyside, Pretoria, 0002",
            "description": "Bright Minds offers one-on-one and group tutoring for Grades 8-12, fully CAPS aligned.",
            "tags": ["Maths", "Science", "Grade 12", "Accounting", "English", "CAPS"],
            "featured": 1,
            "rating": 4.9,
            "reviews": 103,
            "rating_dist": [80, 14, 4, 1, 1],
            "services": [("1-on-1 Session (1hr)", "R280"), ("Group Session", "R160 pp"), ("Exam Prep Package", "R1 200")],
            "hours": [("Monday", 1, "13:00", "19:00"), ("Tuesday", 1, "13:00", "19:00"), ("Wednesday", 1, "13:00", "19:00"), ("Thursday", 1, "13:00", "19:00"), ("Friday", 1, "13:00", "18:00"), ("Saturday", 1, "08:00", "14:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Ayanda Cele", 5, "My daughter's marks improved from 54% to 78% in one term.", "6 Mar 2025"), ("Lerato Sithole", 5, "Brilliant service. Worth every cent.", "20 Feb 2025")],
        },
        {
            "name": "Mama's Kitchen",
            "category": "Restaurants & Food",
            "city": "Umlazi",
            "province": "KwaZulu-Natal",
            "phone": "+27 31 906 2213",
            "email": "mama@kitchen.co.za",
            "website": "",
            "address": "22 T Section, Umlazi, 4031",
            "description": "Authentic isiZulu home cooking with generous portions and warm service.",
            "tags": ["Umngqusho", "Chakalaka", "Pap", "Takeaway", "Family"],
            "featured": 1,
            "rating": 4.6,
            "reviews": 207,
            "rating_dist": [72, 18, 6, 2, 2],
            "services": [("Plate of Pap & Stew", "R55"), ("Umngqusho", "R65"), ("Family Pot", "R280")],
            "hours": [("Monday", 1, "07:00", "20:00"), ("Tuesday", 1, "07:00", "20:00"), ("Wednesday", 1, "07:00", "20:00"), ("Thursday", 1, "07:00", "20:00"), ("Friday", 1, "07:00", "21:00"), ("Saturday", 1, "07:00", "21:00"), ("Sunday", 1, "08:00", "18:00")],
            "reviews_seed": [("Lerato Sithole", 5, "Umngqusho was absolutely delicious. Feels exactly like home cooking.", "9 Mar 2025"), ("Sipho Dlamini", 5, "The family pot is incredible value.", "28 Feb 2025")],
        },
        {
            "name": "ProDrive Mechanics",
            "category": "Mechanics & Auto Repair",
            "city": "Mitchell's Plain",
            "province": "Western Cape",
            "phone": "+27 21 372 8830",
            "email": "prodrive@mechanics.co.za",
            "website": "prodrivemechanics.co.za",
            "address": "45 AZ Berman Dr, Mitchell's Plain, 7785",
            "description": "ProDrive is a fully equipped auto repair centre offering diagnostics, tyre fitment, oil services, and panel beating at fair prices.",
            "tags": ["Diagnostics", "Tyres", "Oil Service", "Panel Beating", "Brakes", "Electrical"],
            "featured": 1,
            "rating": 4.8,
            "reviews": 89,
            "rating_dist": [65, 20, 10, 3, 2],
            "services": [("Full Service", "R650"), ("Tyre Change (x4)", "R200"), ("Brake Pads", "From R480"), ("Diagnostics Scan", "R150"), ("Oil Change", "R380"), ("Panel Beat Quote", "Free")],
            "hours": [("Monday", 1, "07:30", "17:00"), ("Tuesday", 1, "07:30", "17:00"), ("Wednesday", 1, "07:30", "17:00"), ("Thursday", 1, "07:30", "17:00"), ("Friday", 1, "07:30", "17:00"), ("Saturday", 1, "08:00", "13:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Thabo Nkosi", 4, "Fixed my Golf in one day. Fair price, transparent communication.", "8 Mar 2025"), ("Zanele M.", 5, "They diagnosed an issue two other garages missed. Honest and professional.", "20 Feb 2025")],
        },
        {
            "name": "Cape Sparks Electric",
            "category": "Plumbers & Electricians",
            "city": "Kraaifontein",
            "province": "Western Cape",
            "phone": "+27 21 987 4423",
            "email": "info@capesparks.co.za",
            "website": "capesparks.co.za",
            "address": "3 Industrial Park, Kraaifontein, 7570",
            "description": "Registered electrical contractor for COC certificates, DB board upgrades, and solar installations.",
            "tags": ["COC", "Solar", "DB Boards", "Fault Finding", "Registered"],
            "featured": 1,
            "rating": 4.8,
            "reviews": 72,
            "rating_dist": [60, 28, 8, 2, 2],
            "services": [("COC Certificate", "From R800"), ("DB Board Upgrade", "From R3 500"), ("Fault Finding", "R350")],
            "hours": [("Monday", 1, "07:00", "17:00"), ("Tuesday", 1, "07:00", "17:00"), ("Wednesday", 1, "07:00", "17:00"), ("Thursday", 1, "07:00", "17:00"), ("Friday", 1, "07:00", "17:00"), ("Saturday", 1, "08:00", "13:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Sipho Dlamini", 5, "Solar installation was professional, clean, and done in one day.", "4 Mar 2025")],
        },
        {
            "name": "Braai Nation",
            "category": "Restaurants & Food",
            "city": "Soweto",
            "province": "Gauteng",
            "phone": "+27 11 936 4410",
            "email": "braai@nation.co.za",
            "website": "braainationsoweto.co.za",
            "address": "78 Mooki St, Orlando, Soweto, 1804",
            "description": "Celebrating South African braai culture with quality boerewors, lamb chops, and catering.",
            "tags": ["Boerewors", "Lamb", "Chicken", "Takeaway", "Catering"],
            "featured": 0,
            "rating": 4.8,
            "reviews": 189,
            "rating_dist": [68, 22, 7, 2, 1],
            "services": [("Boerewors Roll", "R45"), ("Lamb Chop Plate", "R95"), ("Mixed Braai Plate", "R120")],
            "hours": [("Monday", 0, "", ""), ("Tuesday", 1, "11:00", "21:00"), ("Wednesday", 1, "11:00", "21:00"), ("Thursday", 1, "11:00", "21:00"), ("Friday", 1, "11:00", "22:00"), ("Saturday", 1, "10:00", "22:00"), ("Sunday", 1, "10:00", "20:00")],
            "reviews_seed": [("Nomsa Khumalo", 5, "Boerewors rolls were massive and the chakalaka was excellent.", "7 Mar 2025")],
        },
        {
            "name": "Glam By Zanele",
            "category": "Beauty & Wellness",
            "city": "Alexandra",
            "province": "Gauteng",
            "phone": "+27 11 882 3340",
            "email": "zanele@glam.co.za",
            "website": "glambyzanele.co.za",
            "address": "15 London Rd, Alexandra, 2090",
            "description": "Alexandra's go-to beauty studio for nails, lash extensions, makeup artistry, and brow shaping.",
            "tags": ["Nails", "Lash Extensions", "Makeup", "Brows", "Waxing", "Gel Nails"],
            "featured": 1,
            "rating": 4.9,
            "reviews": 118,
            "rating_dist": [75, 20, 3, 1, 1],
            "services": [("Gel Manicure", "R180"), ("Acrylic Set", "R280"), ("Lash Extensions", "R350"), ("Full Makeup", "R450"), ("Brow Shape & Tint", "R120"), ("Waxing (full leg)", "R200")],
            "hours": [("Monday", 1, "09:00", "18:00"), ("Tuesday", 1, "09:00", "18:00"), ("Wednesday", 1, "09:00", "18:00"), ("Thursday", 1, "09:00", "19:00"), ("Friday", 1, "09:00", "19:00"), ("Saturday", 1, "08:00", "16:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Nomsa Khumalo", 5, "Zanele is an artist. My gel nails lasted 4 weeks without chipping.", "7 Mar 2025"), ("Demo Customer", 3, "Nails looked great but I waited 40 minutes past my appointment.", "5 Mar 2025")],
        },
        {
            "name": "Sharp Edge Barbershop",
            "category": "Barbers & Hair Salons",
            "city": "Bellville",
            "province": "Western Cape",
            "phone": "+27 21 948 3312",
            "email": "sharp@edge.co.za",
            "website": "sharpedge.co.za",
            "address": "12 Durban Rd, Bellville, 7530",
            "description": "Classic cuts, hot towel shaves, and colour treatments in a relaxed environment.",
            "tags": ["Classic Cut", "Shave", "Colour", "Beard"],
            "featured": 0,
            "rating": 4.7,
            "reviews": 88,
            "rating_dist": [55, 25, 12, 5, 3],
            "services": [("Classic Cut", "R110"), ("Hot Towel Shave", "R90"), ("Beard Sculpt", "R70")],
            "hours": [("Monday", 1, "09:00", "17:00"), ("Tuesday", 1, "09:00", "17:00"), ("Wednesday", 1, "09:00", "17:00"), ("Thursday", 1, "09:00", "18:00"), ("Friday", 1, "09:00", "18:00"), ("Saturday", 1, "08:00", "15:00"), ("Sunday", 0, "", "")],
            "reviews_seed": [("Leon Arendse", 5, "Brilliant shop. The hot towel shave is a must-try.", "5 Mar 2025")],
        },
    ]

    owner_user = get_user_by_email(conn, "owner@demo.com")
    owner_user_id = owner_user["id"] if owner_user else None

    for business in demo_businesses:
        exists = conn.execute("SELECT id FROM businesses WHERE name = ?", (business["name"],)).fetchone()
        if not exists:
            insert_seed_business(conn, business, owner_user_id)

    conn.commit()
    conn.close()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def end_headers(self):
        origin = allowed_origin(self.headers.get("Origin", ""))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        super().end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.split(" ", 1)[1].strip()
        return ""

    def _current_user(self, conn: sqlite3.Connection):
        token = self._bearer_token()
        if not token:
            return None
        return conn.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()

    def _session_payload(self, user_row: sqlite3.Row, token: str):
        data = user_public_dict(user_row)
        data["token"] = token
        return data

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        conn = get_db()
        try:
            if parsed.path == "/api/health":
                return self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "localconnect-backend",
                        "frontendOrigin": FRONTEND_ORIGIN or "",
                    },
                )

            if parsed.path == "/api/session":
                user = self._current_user(conn)
                if not user:
                    return self._send_json(401, {"error": "Not signed in."})
                token = self._bearer_token()
                return self._send_json(200, {"session": self._session_payload(user, token)})

            if parsed.path == "/api/users/me":
                user = self._current_user(conn)
                if not user:
                    return self._send_json(401, {"error": "Not signed in."})
                return self._send_json(200, {"user": user_public_dict(user)})

            if parsed.path == "/api/businesses":
                businesses = conn.execute(
                    """
                    SELECT businesses.*, (
                        SELECT image_path
                        FROM business_images
                        WHERE business_id = businesses.id
                        ORDER BY sort_order, id
                        LIMIT 1
                    ) AS cover_image_path
                    FROM businesses
                    ORDER BY featured DESC, rating DESC, name ASC
                    """
                ).fetchall()
                return self._send_json(200, {"businesses": [business_summary_dict(row) for row in businesses]})

            if parsed.path == "/api/owner/business":
                user = self._current_user(conn)
                if not user or user["role"] != "business_owner" or not user["business_id"]:
                    return self._send_json(403, {"error": "Business owner access required."})
                business = conn.execute("SELECT * FROM businesses WHERE id = ?", (user["business_id"],)).fetchone()
                return self._send_json(200, {"business": business_detail_dict(conn, business)})

            if parsed.path == "/api/reviews":
                business_id = parse_qs(parsed.query).get("business_id", [""])[0]
                if not business_id.isdigit():
                    return self._send_json(400, {"error": "A valid business_id is required."})
                reviews = conn.execute(
                    """
                    SELECT reviewer_name, rating, review_text, helpful_count, display_date
                    FROM reviews
                    WHERE business_id = ?
                    ORDER BY id DESC
                    """,
                    (int(business_id),),
                ).fetchall()
                return self._send_json(
                    200,
                    {
                        "reviews": [
                            {
                                "name": row["reviewer_name"],
                                "rating": row["rating"],
                                "text": row["review_text"],
                                "helpful": row["helpful_count"],
                                "date": row["display_date"],
                            }
                            for row in reviews
                        ]
                    },
                )

            if parsed.path.startswith("/api/businesses/"):
                business_id = parsed.path.split("/")[-1]
                if not business_id.isdigit():
                    return self._send_json(404, {"error": "Business not found."})
                business = conn.execute("SELECT * FROM businesses WHERE id = ?", (int(business_id),)).fetchone()
                if not business:
                    return self._send_json(404, {"error": "Business not found."})
                return self._send_json(200, {"business": business_detail_dict(conn, business)})

            return self._send_json(404, {"error": "Route not found."})
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._send_json(404, {"error": "Route not found."})

        conn = get_db()
        try:
            if parsed.path == "/api/register":
                data = self._read_json()
                first_name = (data.get("firstName") or "").strip()
                last_name = (data.get("lastName") or "").strip()
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                role = data.get("role") or "customer"
                phone = (data.get("phone") or "").strip()
                province = (data.get("province") or "").strip()
                business_name = (data.get("businessName") or "").strip()
                category = (data.get("category") or "").strip()
                description = (data.get("description") or "").strip()
                city = (data.get("city") or "").strip()
                address = (data.get("address") or "").strip()
                website = (data.get("website") or "").strip()
                tags = [part.strip() for part in (data.get("tags") or "").split(",") if part.strip()]
                listing_images = data.get("listingImages") or []

                if not first_name or not last_name or not email or len(password) < 8 or role not in {"customer", "business_owner"}:
                    return self._send_json(400, {"error": "Please complete all required fields."})
                if get_user_by_email(conn, email):
                    return self._send_json(409, {"error": "An account with this email already exists."})
                if role == "business_owner" and (not business_name or not category):
                    return self._send_json(400, {"error": "Business owners must provide a business name and category."})

                password_hash, password_salt = hash_password(password)
                name = f"{first_name} {last_name}".strip()
                cursor = conn.execute(
                    """
                    INSERT INTO users (first_name, last_name, name, email, password_hash, password_salt, role, phone, province, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                    """,
                    (first_name, last_name, name, email, password_hash, password_salt, role, phone, province, utc_now()),
                )
                user_id = cursor.lastrowid

                if role == "business_owner":
                    business_cursor = conn.execute(
                        """
                        INSERT INTO businesses (
                            owner_user_id, name, category, city, province, phone, email, website, address,
                            description, tags_json, featured, rating, review_count, rating_dist_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, '[0,0,0,0,0]')
                        """,
                        (
                            user_id,
                            business_name,
                            category,
                            city,
                            province,
                            phone,
                            email,
                            website,
                            address,
                            description,
                            json_dumps(tags),
                        ),
                    )
                    business_id = business_cursor.lastrowid
                    conn.execute("UPDATE users SET business_id = ? WHERE id = ?", (business_id, user_id))
                    default_hours = [("Monday", 1), ("Tuesday", 1), ("Wednesday", 1), ("Thursday", 1), ("Friday", 1), ("Saturday", 0), ("Sunday", 0)]
                    for sort_order, day_info in enumerate(default_hours):
                        conn.execute(
                            """
                            INSERT INTO hours (business_id, sort_order, day_name, is_open, opens_at, closes_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (business_id, sort_order, day_info[0], day_info[1], "09:00" if day_info[1] else "", "17:00" if day_info[1] else ""),
                        )
                    save_business_images(conn, business_id, listing_images)

                token = create_session(conn, user_id)
                conn.commit()
                user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                return self._send_json(201, {"session": self._session_payload(user, token)})

            if parsed.path == "/api/login":
                data = self._read_json()
                email = (data.get("email") or "").strip().lower()
                password = data.get("password") or ""
                role = data.get("role") or ""
                user = get_user_by_email(conn, email)
                if not user or not verify_password(password, user["password_hash"], user["password_salt"]) or (role and user["role"] != role):
                    return self._send_json(401, {"error": "No account found with those credentials for the selected role."})
                token = create_session(conn, user["id"])
                conn.commit()
                return self._send_json(200, {"session": self._session_payload(user, token)})

            if parsed.path == "/api/logout":
                token = self._bearer_token()
                if token:
                    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                    conn.commit()
                return self._send_json(200, {"ok": True})

            if parsed.path == "/api/reviews":
                user = self._current_user(conn)
                if not user:
                    return self._send_json(401, {"error": "Please sign in to leave a review."})
                data = self._read_json()
                business_id = int(data.get("businessId") or 0)
                rating = int(data.get("rating") or 0)
                text = (data.get("text") or "").strip()
                business = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
                if not business:
                    return self._send_json(404, {"error": "Business not found."})
                if rating < 1 or rating > 5 or not text:
                    return self._send_json(400, {"error": "Please provide a rating and review text."})

                display_date = format_display_date()
                conn.execute(
                    """
                    INSERT INTO reviews (business_id, user_id, reviewer_name, rating, review_text, helpful_count, created_at, display_date)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (business_id, user["id"], user["name"], rating, text, utc_now(), display_date),
                )

                current_count = business["review_count"]
                current_rating = business["rating"]
                new_count = current_count + 1
                new_rating = round(((current_rating * current_count) + rating) / new_count, 1) if new_count else rating
                dist = json_list(business["rating_dist_json"])
                index = 5 - rating
                if 0 <= index < len(dist):
                    dist[index] += 1
                conn.execute(
                    "UPDATE businesses SET rating = ?, review_count = ?, rating_dist_json = ? WHERE id = ?",
                    (new_rating, new_count, json_dumps(dist), business_id),
                )
                conn.commit()
                return self._send_json(201, {"ok": True})

            if parsed.path == "/api/owner/business/images":
                if user := self._current_user(conn):
                    if user["role"] != "business_owner" or not user["business_id"]:
                        return self._send_json(403, {"error": "Business owner access required."})
                    images = self._read_json().get("images") or []
                    if not isinstance(images, list) or not images:
                        return self._send_json(400, {"error": "Please choose at least one photo."})
                    try:
                        saved = save_business_images(conn, user["business_id"], images)
                    except ValueError as error:
                        return self._send_json(400, {"error": str(error)})
                    conn.commit()
                    return self._send_json(201, {"images": [image_public_dict(row) for row in saved]})
                return self._send_json(401, {"error": "Not signed in."})

            return self._send_json(404, {"error": "Route not found."})
        finally:
            conn.close()

    def do_PUT(self):
        parsed = urlparse(self.path)
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                return self._send_json(401, {"error": "Not signed in."})

            if parsed.path == "/api/users/me":
                data = self._read_json()
                first_name = (data.get("firstName") or "").strip()
                last_name = (data.get("lastName") or "").strip()
                email = (data.get("email") or "").strip().lower()
                phone = (data.get("phone") or "").strip()
                province = (data.get("province") or "").strip()
                if not first_name or not last_name or not email:
                    return self._send_json(400, {"error": "Please fill in all required fields."})
                duplicate = conn.execute("SELECT id FROM users WHERE email = ? AND id != ?", (email, user["id"])).fetchone()
                if duplicate:
                    return self._send_json(409, {"error": "That email address is already in use."})
                name = f"{first_name} {last_name}".strip()
                conn.execute(
                    """
                    UPDATE users
                    SET first_name = ?, last_name = ?, name = ?, email = ?, phone = ?, province = ?
                    WHERE id = ?
                    """,
                    (first_name, last_name, name, email, phone, province, user["id"]),
                )
                if user["business_id"]:
                    conn.execute("UPDATE businesses SET email = ?, phone = ?, province = ? WHERE id = ?", (email, phone, province, user["business_id"]))
                conn.commit()
                updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
                return self._send_json(200, {"user": user_public_dict(updated)})

            if parsed.path == "/api/users/me/password":
                data = self._read_json()
                current_password = data.get("currentPassword") or ""
                new_password = data.get("newPassword") or ""
                if not verify_password(current_password, user["password_hash"], user["password_salt"]):
                    return self._send_json(400, {"error": "Incorrect password.", "field": "currentPassword"})
                if len(new_password) < 8:
                    return self._send_json(400, {"error": "Password must be at least 8 characters.", "field": "newPassword"})
                password_hash, password_salt = hash_password(new_password)
                conn.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?", (password_hash, password_salt, user["id"]))
                conn.commit()
                return self._send_json(200, {"ok": True})

            if parsed.path == "/api/owner/business":
                if user["role"] != "business_owner" or not user["business_id"]:
                    return self._send_json(403, {"error": "Business owner access required."})
                data = self._read_json()
                tags = [part.strip() for part in (data.get("tags") or "").split(",") if part.strip()]
                conn.execute(
                    """
                    UPDATE businesses
                    SET name = ?, category = ?, city = ?, province = ?, address = ?, description = ?, phone = ?, email = ?, website = ?, tags_json = ?
                    WHERE id = ?
                    """,
                    (
                        (data.get("name") or "").strip(),
                        (data.get("category") or "").strip(),
                        (data.get("city") or "").strip(),
                        (data.get("province") or "").strip(),
                        (data.get("address") or "").strip(),
                        (data.get("description") or "").strip(),
                        (data.get("phone") or "").strip(),
                        (data.get("email") or "").strip(),
                        (data.get("website") or "").strip(),
                        json_dumps(tags),
                        user["business_id"],
                    ),
                )
                conn.commit()
                business = conn.execute("SELECT * FROM businesses WHERE id = ?", (user["business_id"],)).fetchone()
                return self._send_json(200, {"business": business_detail_dict(conn, business)})

            if parsed.path == "/api/owner/business/services":
                if user["role"] != "business_owner" or not user["business_id"]:
                    return self._send_json(403, {"error": "Business owner access required."})
                services = self._read_json().get("services") or []
                conn.execute("DELETE FROM services WHERE business_id = ?", (user["business_id"],))
                for sort_order, service in enumerate(services):
                    name = (service.get("n") or "").strip()
                    price = (service.get("p") or "").strip()
                    if name or price:
                        conn.execute(
                            "INSERT INTO services (business_id, sort_order, name, price) VALUES (?, ?, ?, ?)",
                            (user["business_id"], sort_order, name, price),
                        )
                conn.commit()
                return self._send_json(200, {"ok": True})

            if parsed.path == "/api/owner/business/hours":
                if user["role"] != "business_owner" or not user["business_id"]:
                    return self._send_json(403, {"error": "Business owner access required."})
                hours = self._read_json().get("hours") or []
                conn.execute("DELETE FROM hours WHERE business_id = ?", (user["business_id"],))
                for sort_order, item in enumerate(hours):
                    conn.execute(
                        """
                        INSERT INTO hours (business_id, sort_order, day_name, is_open, opens_at, closes_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user["business_id"],
                            sort_order,
                            item.get("d") or "",
                            1 if item.get("open") else 0,
                            item.get("from") or "",
                            item.get("to") or "",
                        ),
                    )
                conn.commit()
                return self._send_json(200, {"ok": True})

            return self._send_json(404, {"error": "Route not found."})
        finally:
            conn.close()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        conn = get_db()
        try:
            user = self._current_user(conn)
            if not user:
                return self._send_json(401, {"error": "Not signed in."})

            if parsed.path.startswith("/api/owner/business/images/"):
                if user["role"] != "business_owner" or not user["business_id"]:
                    return self._send_json(403, {"error": "Business owner access required."})
                image_id = parsed.path.rsplit("/", 1)[-1]
                if not image_id.isdigit():
                    return self._send_json(404, {"error": "Photo not found."})
                image = conn.execute(
                    "SELECT * FROM business_images WHERE id = ? AND business_id = ?",
                    (int(image_id), user["business_id"]),
                ).fetchone()
                if not image:
                    return self._send_json(404, {"error": "Photo not found."})
                delete_image_file(image["image_path"])
                conn.execute("DELETE FROM business_images WHERE id = ?", (image["id"],))
                conn.commit()
                return self._send_json(200, {"ok": True})

            if parsed.path != "/api/users/me":
                return self._send_json(404, {"error": "Route not found."})

            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            if user["business_id"]:
                images = conn.execute("SELECT image_path FROM business_images WHERE business_id = ?", (user["business_id"],)).fetchall()
                for image in images:
                    delete_image_file(image["image_path"])
                conn.execute("DELETE FROM business_images WHERE business_id = ?", (user["business_id"],))
                conn.execute("DELETE FROM services WHERE business_id = ?", (user["business_id"],))
                conn.execute("DELETE FROM hours WHERE business_id = ?", (user["business_id"],))
                conn.execute("DELETE FROM reviews WHERE business_id = ?", (user["business_id"],))
                conn.execute("DELETE FROM businesses WHERE id = ?", (user["business_id"],))
            conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            conn.commit()
            return self._send_json(200, {"ok": True})
        finally:
            conn.close()


if __name__ == "__main__":
    seed_database()
    print(f"LocalConnect SA running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
