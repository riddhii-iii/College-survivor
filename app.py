import sqlite3
import calendar
import math
import os
from datetime import date
from flask import Flask, render_template, redirect, request, session
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
from email.mime.text import MIMEText
import os
from dotenv import load_dotenv

load_dotenv()
print("DB PATH:", os.path.abspath("college.db"))

app = Flask(__name__)
app.secret_key = "college-survivor-secret"


# -------------------- DB --------------------

def get_db():
    return sqlite3.connect("college.db")


# -------------------- AUTH --------------------

def require_login():
    return "user_id" in session


# -------------------- HELPERS --------------------

def calculate_attendance_percentage(subject_id, db):
    cur = db.cursor()

    # Get subject weight
    cur.execute("""
        SELECT attendance_weight
        FROM subjects
        WHERE id = ?
    """, (subject_id,))
    
    row = cur.fetchone()
    weight = row[0] if row else 1

    # Exclude cancelled classes
    cur.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE subject_id = ?
          AND status != 'cancelled'
    """, (subject_id,))

    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    total_hours = total * weight
    present_hours = present * weight

    if total_hours == 0:
        return 100

    return round((present_hours / total_hours) * 100, 2)


def classes_can_skip(subject_id, db):
    cur = db.cursor()

    # FIX: exclude cancelled classes here too
    cur.execute("""
        SELECT COUNT(*), SUM(CASE WHEN status='present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE subject_id = ? AND status != 'cancelled'
    """, (subject_id,))
    total, present = cur.fetchone()
    present = present or 0

    cur.execute("SELECT attendance_required_percent FROM subjects WHERE id = ?", (subject_id,))
    row = cur.fetchone()
    required = row[0] if row else 75

    if total == 0:
        return 0
    max_absences = int((100 - required) * total / required)
    current_absences = total - present
    return max(0, max_absences - current_absences)


def has_urgent_deadline(subject_id, db):
    cur = db.cursor()
    today = date.today()
    cur.execute("""
        SELECT due_date FROM deadlines
        WHERE subject_id = ? AND completed = 0
    """, (subject_id,))
    for (due_date,) in cur.fetchall():
        days_left = (date.fromisoformat(due_date) - today).days
        if 0 <= days_left <= 3:
            return True
    return False


def has_assignment_overload(subject_id, db):
    cur = db.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM deadlines
        WHERE subject_id = ? AND type = 'assignment' AND completed = 0
    """, (subject_id,))
    return cur.fetchone()[0] > 2


# -------------------- ROUTES --------------------

@app.route("/")
def home():
    return redirect("/dashboard")


# -------- DASHBOARD --------

@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    today = date.today()
    weekday = today.weekday()

    cur.execute("SELECT id FROM subjects WHERE user_id = ?", (user_id,))
    subject_ids = [row[0] for row in cur.fetchall()]

    subjects_at_risk = 0
    attendance_values = []
    for subject_id in subject_ids:
        pct = calculate_attendance_percentage(subject_id, db)
        attendance_values.append(pct)
        if pct < 80:
            subjects_at_risk += 1

    overall_attendance = round(sum(attendance_values) / len(attendance_values), 1) if attendance_values else 0
    safe_subjects = len(subject_ids) - subjects_at_risk

    # Weekly attendance (exclude cancelled)
    cur.execute("""
        SELECT COUNT(*), SUM(CASE WHEN status='present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE date >= date('now', '-6 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    total, present = cur.fetchone()
    present = present or 0
    weekly_attendance = round((present / total) * 100) if total else 0

    # Last week attendance (exclude cancelled)
    cur.execute("""
        SELECT COUNT(*), SUM(CASE WHEN status='present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE date BETWEEN date('now', '-13 days') AND date('now', '-7 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    last_total, last_present = cur.fetchone()
    last_present = last_present or 0
    last_week_attendance = round((last_present / last_total) * 100) if last_total else weekly_attendance

    # Insight
    if overall_attendance == 0:
        attendance_insight = "No attendance data yet"
    elif overall_attendance < 75:
        attendance_insight = "⚠ Attendance is critically low"
    elif overall_attendance < 80:
        attendance_insight = "Attendance needs attention"
    elif weekly_attendance > last_week_attendance:
        attendance_insight = "📈 You attended more classes than last week"
    elif weekly_attendance < last_week_attendance:
        attendance_insight = "⚠ Attendance dropped compared to last week"
    else:
        attendance_insight = "➖ Attendance stayed the same as last week"

    # Urgent deadlines
    cur.execute("""
        SELECT COUNT(*) FROM deadlines
        WHERE completed = 0
          AND due_date BETWEEN date('now') AND date('now', '+7 days')
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    urgent_deadlines = cur.fetchone()[0] or 0

    # Today's classes
    cur.execute("""
        SELECT COUNT(*) FROM timetable
        WHERE weekday = ? AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (weekday, user_id))
    result = cur.fetchone()
    todays_classes = result[0] if result else 0

    # Attendance trend sparkline (exclude cancelled)
    cur.execute("""
        SELECT date,
               SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)
        FROM attendance
        WHERE date >= date('now', '-6 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        GROUP BY date ORDER BY date
    """, (user_id,))
    attendance_trend = [round(row[1]) for row in cur.fetchall() if row[1] is not None]

    db.close()

    return render_template(
        "dashboard.html",
        subjects_at_risk=subjects_at_risk,
        urgent_deadlines=urgent_deadlines,
        todays_classes=todays_classes,
        safe_subjects=safe_subjects,
        overall_attendance=overall_attendance,
        weekly_attendance=weekly_attendance,
        attendance_insight=attendance_insight,
        attendance_trend=attendance_trend
    )


# -------- ATTENDANCE --------

@app.route("/attendance")
def attendance():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    month_name = calendar.month_name[month]
    month_calendar = calendar.monthcalendar(year, month)

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    weekday_index = today.weekday()
    weekday_name = calendar.day_name[weekday_index]

    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT min_attendance FROM settings WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    min_required = row[0] if row and row[0] else 75

    # Monthly attendance (exclude cancelled)
    cur.execute("""
        SELECT COUNT(*), SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE strftime('%Y', date) = ? AND strftime('%m', date) = ?
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (str(year), f"{month:02d}", user_id))
    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    monthly_attendance = round((present / total) * 100) if total > 0 else 0
    required_presents = math.ceil(total * min_required / 100)
    can_miss = max(0, total - required_presents)
    month_status = "⚠ Attendance at risk" if monthly_attendance < min_required else "✅ Attendance safe"

    today_str = date.today().isoformat()

    cur.execute("""
    SELECT subject.id, subject.name
    FROM timetable
    JOIN subject ON timetable.subject_id = subject.id
    WHERE subject.user_id = ?
      AND (
            timetable.weekday = ?
            OR (timetable.is_extra = 1 AND timetable.class_date = ?)
          )
""", (user_id, weekday_index, today_str))
    (weekday_index, user_id)
    today_subjects = cur.fetchall()

    subjects = []
    for subject_id, name in today_subjects:
        attendance_pct = calculate_attendance_percentage(subject_id, db)
        skip_left = classes_can_skip(subject_id, db)
        cur.execute("SELECT date, status FROM attendance WHERE subject_id = ?", (subject_id,))
        attendance_map = {d: s for d, s in cur.fetchall()}
        subjects.append({
            "id": subject_id,
            "name": name,
            "attendance": attendance_pct,
            "skip_left": skip_left,
            "attendance_map": attendance_map
        })

    db.close()

    return render_template(
        "attendance.html",
        subjects=subjects,
        calendar=month_calendar,
        month_name=month_name,
        year=year,
        month=month,
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year,
        weekday=weekday_name,
        monthly_attendance=monthly_attendance,
        month_status=month_status,
        min_required=min_required,
        can_miss=can_miss
    )


# -------- MARK ATTENDANCE (AJAX) --------

@app.route("/mark-attendance", methods=["POST"])
def mark_attendance_ajax():
    if not require_login():
        return "", 401

    data = request.json
    subject_id = data["subject_id"]
    date_val = data["date"]
    status = data["status"]

    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM attendance WHERE subject_id = ? AND date = ?", (subject_id, date_val))
    cur.execute("INSERT INTO attendance (subject_id, date, status) VALUES (?, ?, ?)", (subject_id, date_val, status))
    db.commit()
    db.close()
    return "", 204


# -------- MARK ATTENDANCE (BUTTON) --------

@app.route("/mark/<int:subject_id>/<status>")
def mark_attendance(subject_id, status):
    if not require_login():
        return redirect("/login")

    if status not in ["present", "absent", "cancelled"]:
        return redirect("/attendance")

    db = get_db()
    cur = db.cursor()
    today_str = date.today().isoformat()
    weekday = date.today().weekday()

    cur.execute("SELECT 1 FROM timetable WHERE subject_id = ? AND weekday = ?", (subject_id, weekday))
    if not cur.fetchone():
        db.close()
        return redirect("/attendance")

    cur.execute("SELECT id FROM attendance WHERE subject_id = ? AND date = ?", (subject_id, today_str))
    existing = cur.fetchone()

    if existing:
        cur.execute("UPDATE attendance SET status = ? WHERE subject_id = ? AND date = ?", (status, subject_id, today_str))
    else:
        cur.execute("INSERT INTO attendance (subject_id, date, status) VALUES (?, ?, ?)", (subject_id, today_str, status))

    db.commit()
    db.close()
    return redirect("/attendance")


# -------- ATTENDANCE CALENDAR (PER SUBJECT) --------

@app.route("/attendance-calendar/<int:subject_id>")
def attendance_calendar(subject_id):
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT name FROM subjects WHERE id = ?", (subject_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        return redirect("/subjects")
    subject_name = row[0]

    cur.execute("SELECT date, status FROM attendance WHERE subject_id = ?", (subject_id,))
    attendance_map = {rec_date: rec_status for rec_date, rec_status in cur.fetchall()}
    db.close()

    today = date.today()
    cal = calendar.monthcalendar(today.year, today.month)

    return render_template(
        "attendance_calendar.html",
        subject_name=subject_name,
        calendar=cal,
        attendance_map=attendance_map,
        year=today.year,
        month=today.month
    )


# -------- DEADLINES --------

@app.route("/deadlines")
def deadlines():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT deadline.id, deadline.title, deadline.due_date, deadline.completed, subject.name
        FROM deadlines
        JOIN subject ON deadline.subject_id = subject.id
        WHERE subject.user_id = ?
        ORDER BY deadline.due_date
    """, (user_id,))
    deadlines_list = cur.fetchall()
    db.close()
    return render_template("deadlines.html", deadlines=deadlines_list)

#---------- Add Deadline --------
@app.route("/add-deadline", methods=["GET", "POST"])
def add_deadline():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        subject_id = request.form["subject_id"]
        title = request.form["title"]
        due_date = request.form["due_date"]
        deadline_type = request.form["type"]

        cur.execute("""
            INSERT INTO deadline (subject_id, title, due_date, type, completed)
            VALUES (?, ?, ?, ?, 0)
        """, (subject_id, title, due_date, deadline_type))

        db.commit()
        db.close()

        return redirect("/deadlines")

    # GET request — show form
    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (user_id,))
    subjects = cur.fetchall()
    db.close()

    return render_template("add_deadline.html", subjects=subjects)

#--------- EMAIL REMINDER --------
def send_email(to_email, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        server.sendmail(msg["From"], [to_email], msg.as_string())

#---------WEEKLY REPORT EMAIL --------
@app.route("/send-weekly-report")
def send_weekly_report():
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT email, name FROM users WHERE email IS NOT NULL")
    users = cur.fetchall()

    for email, name in users:
        # Simple demo report
        subject = "Your Weekly Attendance Report 📊"
        body = f"""
Hello {name},

Here is your weekly attendance summary from College Survivor.

Keep pushing 💪
"""

        send_email(email, subject, body)

    return "Weekly reports sent successfully!"
# -------- WEEKLY DANGER --------

@app.route("/weekly-danger")
def weekly_danger():
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (session["user_id"],))
    subjects = cur.fetchall()

    danger_list = []
    for subject_id, name in subjects:
        reasons = []
        if calculate_attendance_percentage(subject_id, db) <= 80:
            reasons.append("Low attendance")
        if has_urgent_deadline(subject_id, db):
            reasons.append("Urgent deadline")
        if has_assignment_overload(subject_id, db):
            reasons.append("Assignment overload")
        if reasons:
            danger_list.append({"subject": name, "reasons": reasons})

    db.close()
    return render_template("weekly_danger.html", danger_list=danger_list)


# -------- SUBJECTS --------

@app.route("/subjects")
def view_subjects():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, credits, attendance_required_percent, attendance_weight
        FROM subjects WHERE user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    db.close()

    subjects = [
        {"id": r[0], "name": r[1], "credits": r[2], "required": r[3], "weight": r[4]}
        for r in rows
    ]
    return render_template("subjects.html", subjects=subjects)


@app.route("/add-subject", methods=["GET", "POST"])
def add_subject():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form["name"]
        credits = request.form["credits"]
        attendance_req = request.form["attendance_required"]
        weight = request.form["attendance_weight"]
        cur.execute("""
            INSERT INTO subjects (user_id, name, credits, attendance_required_percent, attendance_weight, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, name, credits, attendance_req, weight, date.today().isoformat()))
        db.commit()
        db.close()
        return redirect("/subjects")

    db.close()
    return render_template("add_subject.html")


@app.route("/edit-subject/<int:subject_id>", methods=["GET", "POST"])
def edit_subject(subject_id):
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form["name"]
        credits = request.form["credits"]
        attendance_req = request.form["attendance_required"]
        weight = request.form["attendance_weight"]
        cur.execute("""
            UPDATE subject
            SET name = ?, credits = ?, attendance_required_percent = ?, attendance_weight = ?
            WHERE id = ?
        """, (name, credits, attendance_req, weight, subject_id))
        db.commit()
        db.close()
        return redirect("/subjects")

    cur.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,))
    subject = cur.fetchone()
    db.close()
    return render_template("edit_subject.html", subject=subject)


@app.route("/delete-subject/<int:subject_id>")
def delete_subject(subject_id):
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM attendance WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM timetable WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM deadlines WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    db.commit()
    db.close()
    return redirect("/subjects")


# -------- TIMETABLE --------

@app.route("/timetable", methods=["GET", "POST"])
def timetable():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        subject_id = request.form["subject_id"]
        weekdays = request.form.getlist("weekdays")
        cur.execute("DELETE FROM timetable WHERE subject_id = ? AND user_id = ?", (subject_id, user_id))
        for day in weekdays:
            cur.execute(
                "INSERT INTO timetable (subject_id, weekday, user_id) VALUES (?, ?, ?)",
                (subject_id, int(day), user_id)
            )
        db.commit()
        db.close()
        return redirect("/timetable")

    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (user_id,))
    subjects = cur.fetchall()

    cur.execute("SELECT subject_id, weekday FROM timetable WHERE user_id = ?", (user_id,))
    timetable_map = {}
    for subject_id, weekday in cur.fetchall():
        timetable_map.setdefault(subject_id, []).append(weekday)

    db.close()
    return render_template("timetable.html", subjects=subjects, timetable_map=timetable_map)


# -------- PROFILE --------

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    # ================= UPDATE USER INFO =================
    if request.method == "POST":
        new_name = request.form.get("name")
        new_email = request.form.get("email")

        cur.execute(
            "UPDATE users SET name = ?, email = ? WHERE id = ?",
            (new_name, new_email, user_id)
        )
        db.commit()

    # ================= FETCH USER INFO =================
    cur.execute(
        "SELECT name, email, created_at FROM users WHERE id = ?",
        (user_id,)
    )
    user = cur.fetchone()

    # ================= ACTIVITY SUMMARY =================
    cur.execute(
        "SELECT COUNT(*) FROM subjects WHERE user_id = ?",
        (user_id,)
    )
    total_subjects = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM deadlines
        WHERE subject_id IN (
            SELECT id FROM subjects WHERE user_id = ?
        )
    """, (user_id,))
    total_deadlines = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM attendance
        WHERE subject_id IN (
            SELECT id FROM subjects WHERE user_id = ?
        )
        AND status != 'cancelled'
    """, (user_id,))
    total_attendance = cur.fetchone()[0]

    # ================= ATTENDANCE TREND =================
    cur.execute("""
        SELECT date,
               SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)
        FROM attendance
        WHERE status != 'cancelled'
          AND subject_id IN (
              SELECT id FROM subjects WHERE user_id = ?
          )
        GROUP BY date
        ORDER BY date
    """, (user_id,))

    attendance_trend = [
        round(row[1]) for row in cur.fetchall()
        if row[1] is not None
    ]

    db.close()

    return render_template(
        "profile.html",
        user=user,
        total_subjects=total_subjects,
        total_deadlines=total_deadlines,
        total_attendance=total_attendance,
        attendance_trend=attendance_trend
    )


# -------- CLICK ANALYTICS --------

@app.route("/log-click/<page>")
def log_click(page):
    if not require_login():
        return "", 401

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    cur.execute("INSERT INTO click_log (user_id, page) VALUES (?, ?)", (user_id, page))
    db.commit()
    db.close()
    return "", 204


# -------- REGISTER --------

from datetime import datetime
from werkzeug.security import generate_password_hash

@app.route("/register", methods=["GET", "POST"])
def register():
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        confirm = request.form.get("confirm")

        if not name or not password or not confirm:
            return "Missing form data"

        if password != confirm:
            return "Passwords do not match"

        hashed_password = generate_password_hash(password)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 🔎 Check duplicate manually first
        cur.execute("SELECT id FROM users WHERE name = ?", (name,))
        existing = cur.fetchone()
        if existing:
            db.close()
            return "User already exists"

        # ✅ Proper insert
        cur.execute(
            "INSERT INTO users (name, email, password, created_at) VALUES (?, ?, ?, ?)",
            (name, email, hashed_password, created_at)
        )
        db.commit()
        db.close()
        return redirect("/login")

    db.close()
    return render_template("register.html")

#----------Scheduled classes----------
@app.route("/add-extra-class", methods=["POST"])
def add_extra_class():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    subject_id = request.form["subject_id"]
    class_date = request.form["class_date"]

    db = get_db()
    cur = db.cursor()

    cur.execute("""
        INSERT INTO timetable (subject_id, weekday, user_id, is_extra, class_date)
        VALUES (?, ?, ?, 1, ?)
    """, (
        subject_id,
        -1,   # weekday not used for extra
        user_id,
        class_date
    ))

    db.commit()
    db.close()

    return redirect("/attendance")


# -------- LOGIN --------

@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")

        cur.execute(
            "SELECT id, password FROM users WHERE name = ?",
            (name,)
        )
        user = cur.fetchone()

        if user and check_password_hash(user[1], password):
            session["user_id"] = user[0]
            db.close()
            return redirect("/dashboard")

        db.close()
        return "Invalid username or password"

    db.close()
    return render_template("login.html")

# -------- FORGOT PASSWORD --------

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("name", "").strip()
        new_password = request.form.get("password", "")

        if not username or not new_password:
            return render_template("forgot_password.html", error="Please fill in all fields.")

        hashed_password = generate_password_hash(new_password)
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE users SET password = ? WHERE username = ?", (hashed_password, username))
        db.commit()
        db.close()
        return redirect("/login")

    return render_template("forgot_password.html")


# -------- DELETE ACCOUNT --------

@app.route("/delete-account", methods=["POST"])
def delete_account():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        DELETE FROM attendance
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    cur.execute("""
        DELETE FROM deadlines
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    cur.execute("""
        DELETE FROM timetable
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
    """, (user_id,))
    cur.execute("DELETE FROM subjects WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    db.close()
    session.clear()
    return redirect("/register")


# -------- LOGOUT --------

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


import os

def init_db():
    db = get_db()
    with open("schema.sql", "r") as f:
        db.executescript(f.read())
    db.commit()

#-----temp-----
@app.route("/initdb")
def initialize_database():
    db = get_db()
    with open("schema.sql", "r") as f:
        db.executescript(f.read())
    db.commit()
    return "Database initialized!"


# -------------------- RUN --------------------


if __name__ == "__main__":
    app.run(debug=True)