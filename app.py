import sqlite3
import calendar
import math
import os
import smtplib
import hashlib
from datetime import date, datetime
from email.mime.text import MIMEText

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import timedelta

load_dotenv()
DB_PATH = os.path.abspath('college.db')
FALLBACK_DB_PATH = os.path.abspath('college_recovered.db')
print('DB PATH:', DB_PATH)

app = Flask(__name__)
app.secret_key = "college-survivor-secret"


def get_db():
    # Memory journaling is more reliable here because the project lives in a
    # synced folder and SQLite sidecar files were causing disk I/O issues.
    db = sqlite3.connect(DB_PATH)
    db.execute('PRAGMA journal_mode=MEMORY')
    db.execute('PRAGMA temp_store=MEMORY')
    return db


def table_exists(db, table_name):
    cur = db.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    )
    return cur.fetchone() is not None


def column_exists(db, table_name, column_name):
    cur = db.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cur.fetchall())


def ensure_column(db, table_name, column_name, definition):
    # Lightweight schema patching keeps older databases usable without a full migration tool.
    if not column_exists(db, table_name, column_name):
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def require_login():
    return "user_id" in session


def get_user_min_attendance(user_id, db):
    cur = db.cursor()
    cur.execute("SELECT min_attendance FROM settings WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else 75


def calculate_attendance_percentage(subject_id, db):
    cur = db.cursor()
    cur.execute(
        """
        SELECT attendance_weight
        FROM subjects
        WHERE id = ?
        """,
        (subject_id,),
    )
    row = cur.fetchone()
    weight = row[0] if row else 1

    cur.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE subject_id = ?
          AND status != 'cancelled'
        """,
        (subject_id,),
    )
    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    # Attendance can represent lectures/labs with different hour weights,
    # so percentages are based on weighted hours instead of raw class count.
    total_hours = total * weight
    present_hours = present * weight

    if total_hours == 0:
        return 100

    return round((present_hours / total_hours) * 100, 2)


def classes_can_skip(subject_id, db):
    cur = db.cursor()
    cur.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE subject_id = ? AND status != 'cancelled'
        """,
        (subject_id,),
    )
    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    cur.execute(
        "SELECT attendance_required_percent FROM subjects WHERE id = ?",
        (subject_id,),
    )
    row = cur.fetchone()
    required = row[0] if row and row[0] else 75

    if total == 0:
        return 0

    # Rearranged from the minimum attendance formula so we can show a "safe skips left" number.
    max_absences = int((100 - required) * total / required)
    current_absences = total - present
    return max(0, max_absences - current_absences)


def has_urgent_deadline(subject_id, db):
    cur = db.cursor()
    today = date.today()
    cur.execute(
        """
        SELECT due_date FROM deadlines
        WHERE subject_id = ? AND completed = 0
        """,
        (subject_id,),
    )
    for (due_date,) in cur.fetchall():
        days_left = (date.fromisoformat(due_date) - today).days
        if 0 <= days_left <= 3:
            return True
    return False


def has_assignment_overload(subject_id, db):
    cur = db.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM deadlines
        WHERE subject_id = ? AND type = 'assignment' AND completed = 0
        """,
        (subject_id,),
    )
    return cur.fetchone()[0] > 2


def get_subject_attendance_snapshot(subject_id, db):
    cur = db.cursor()
    cur.execute(
        """
        SELECT attendance_required_percent, attendance_weight, name
        FROM subjects
        WHERE id = ?
        """,
        (subject_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    required, weight, name = row
    required = required or 75
    weight = weight or 1

    cur.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE subject_id = ?
          AND status != 'cancelled'
        """,
        (subject_id,),
    )
    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    total_hours = total * weight
    present_hours = present * weight
    percentage = round((present_hours / total_hours) * 100, 2) if total_hours else 100

    return {
        "subject_id": subject_id,
        "name": name,
        "required": required,
        "weight": weight,
        "total": total,
        "present": present,
        "percentage": percentage,
        "skip_left": classes_can_skip(subject_id, db),
    }


def get_attendance_forecast(subject_id, db):
    snapshot = get_subject_attendance_snapshot(subject_id, db)
    if not snapshot:
        return None

    total = snapshot["total"]
    present = snapshot["present"]
    required = snapshot["required"]
    percentage = snapshot["percentage"]

    if total == 0:
        return {
            **snapshot,
            "status": "fresh",
            "headline": "No attendance trend yet",
            "message": "Your first few present classes will build your safety buffer.",
            "misses_until_risk": 0,
            "classes_to_recover": 0,
        }

    misses_until_risk = max(0, math.floor((100 * present / required) - total))
    if percentage < required:
        classes_to_recover = max(
            0,
            math.ceil(((required * total) - (100 * present)) / (100 - required)),
        )
        headline = f"Need {classes_to_recover} straight present classes to recover"
        message = "Focus on the next few classes. Every attended class now has a big effect."
        status = "recover"
    else:
        classes_to_recover = 0
        if misses_until_risk == 0:
            headline = "You are right on the attendance boundary"
            message = "Missing even one more class would push this subject below target."
            status = "edge"
        elif misses_until_risk == 1:
            headline = "You can miss 1 more class safely"
            message = "One absence still keeps you safe, but the next one creates risk."
            status = "tight"
        else:
            headline = f"You can miss {misses_until_risk} more classes safely"
            message = "You still have a buffer, but keep logging attendance to protect it."
            status = "safe"

    return {
        **snapshot,
        "status": status,
        "headline": headline,
        "message": message,
        "misses_until_risk": misses_until_risk,
        "classes_to_recover": classes_to_recover,
    }


def get_exam_countdown(user_id, db, limit=4):
    cur = db.cursor()
    today = date.today()
    cur.execute(
        """
        SELECT deadlines.id,
               deadlines.title,
               deadlines.due_date,
               deadlines.priority,
               subjects.name
        FROM deadlines
        JOIN subjects ON deadlines.subject_id = subjects.id
        WHERE subjects.user_id = ?
          AND deadlines.completed = 0
          AND LOWER(COALESCE(deadlines.type, '')) = 'exam'
          AND deadlines.due_date >= ?
        ORDER BY deadlines.due_date
        LIMIT ?
        """,
        (user_id, today.isoformat(), limit),
    )

    exams = []
    for exam_id, title, due_date, priority, subject_name in cur.fetchall():
        due = date.fromisoformat(due_date)
        days_left = (due - today).days
        if days_left <= 1:
            urgency = "urgent"
            countdown = "Tomorrow" if days_left == 1 else "Today"
        elif days_left <= 3:
            urgency = "soon"
            countdown = f"In {days_left} days"
        else:
            urgency = "planned"
            countdown = f"In {days_left} days"

        exams.append(
            {
                "id": exam_id,
                "title": title,
                "due_date": due_date,
                "priority": priority or "medium",
                "subject": subject_name,
                "days_left": days_left,
                "countdown": countdown,
                "urgency": urgency,
            }
        )

    return exams


def make_study_session_key(day_key, item_type, subject, title):
    raw = f"{day_key}|{item_type}|{subject}|{title}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def build_study_plan(user_id, db):
    cur = db.cursor()
    today = date.today()
    plan_by_date = {}
    subject_names = {}
    cur.execute(
        """
        SELECT session_key, completed
        FROM study_plan_progress
        WHERE user_id = ?
        """,
        (user_id,),
    )
    progress_map = {session_key: bool(completed) for session_key, completed in cur.fetchall()}

    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (user_id,))
    for subject_id, name in cur.fetchall():
        subject_names[subject_id] = name

    for offset in range(7):
        current_day = today + timedelta(days=offset)
        plan_by_date[current_day.isoformat()] = {
            "date": current_day.isoformat(),
            "label": current_day.strftime("%a, %d %b"),
            "items": [],
        }

    for subject_id in subject_names:
        forecast = get_attendance_forecast(subject_id, db)
        if not forecast:
            continue
        if forecast["status"] in {"recover", "edge", "tight"}:
            plan_by_date[today.isoformat()]["items"].append(
                {
                    "type": "attendance",
                    "subject": forecast["name"],
                    "duration": "45 min",
                    "title": "Attendance recovery review",
                    "note": forecast["headline"],
                    "day_key": today.isoformat(),
                }
            )

    cur.execute(
        """
        SELECT timetable.subject_id, timetable.weekday, timetable.class_date, timetable.is_extra
        FROM timetable
        JOIN subjects ON subjects.id = timetable.subject_id
        WHERE timetable.user_id = ?
        """,
        (user_id,),
    )
    timetable_rows = cur.fetchall()

    for offset in range(7):
        current_day = today + timedelta(days=offset)
        day_key = current_day.isoformat()
        weekday = current_day.weekday()
        for subject_id, saved_weekday, class_date, is_extra in timetable_rows:
            is_match = (is_extra == 0 and saved_weekday == weekday) or (
                is_extra == 1 and class_date == day_key
            )
            if is_match:
                plan_by_date[day_key]["items"].append(
                    {
                        "type": "class",
                        "subject": subject_names.get(subject_id, "Subject"),
                        "duration": "30 min",
                        "title": "Class follow-up",
                        "note": "Review notes and update attendance after class.",
                        "day_key": day_key,
                    }
                )

    cur.execute(
        """
        SELECT deadlines.title,
               deadlines.due_date,
               deadlines.type,
               deadlines.priority,
               subjects.name
        FROM deadlines
        JOIN subjects ON deadlines.subject_id = subjects.id
        WHERE subjects.user_id = ?
          AND deadlines.completed = 0
          AND deadlines.due_date BETWEEN ? AND ?
        ORDER BY deadlines.due_date
        """,
        (user_id, today.isoformat(), (today + timedelta(days=7)).isoformat()),
    )
    for title, due_date, deadline_type, priority, subject_name in cur.fetchall():
        due = date.fromisoformat(due_date)
        prep_day = max(today, due - timedelta(days=2 if (deadline_type or "").lower() == "exam" else 1))
        duration = "90 min" if (deadline_type or "").lower() == "exam" else "60 min"
        item_type = "exam" if (deadline_type or "").lower() == "exam" else "deadline"
        if prep_day.isoformat() in plan_by_date:
            plan_by_date[prep_day.isoformat()]["items"].append(
                {
                    "type": item_type,
                    "subject": subject_name,
                    "duration": duration,
                    "title": title,
                    "note": f"{(deadline_type or 'task').title()} due on {due_date} ({priority or 'medium'} priority).",
                    "day_key": prep_day.isoformat(),
                }
            )

    plan = [plan_by_date[key] for key in sorted(plan_by_date)]
    completed_sessions = 0
    total_sessions = 0
    for day in plan:
        for item in day["items"]:
            key = make_study_session_key(day["date"], item["type"], item["subject"], item["title"])
            item["session_key"] = key
            item["completed"] = progress_map.get(key, False)
            total_sessions += 1
            if item["completed"]:
                completed_sessions += 1
        day["items"].sort(key=lambda item: (item["completed"], item["type"], item["title"]))

    busy_days = sum(1 for day in plan if day["items"])

    return {
        "days": plan,
        "busy_days": busy_days,
        "total_sessions": total_sessions,
        "completed_sessions": completed_sessions,
        "pending_sessions": total_sessions - completed_sessions,
    }


@app.route("/")
def home():
    return redirect("/dashboard")


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
    recovery_subjects = 0
    for subject_id in subject_ids:
        forecast = get_attendance_forecast(subject_id, db)
        if not forecast:
            continue
        pct = forecast["percentage"]
        attendance_values.append(pct)
        if pct < 80:
            subjects_at_risk += 1
        if forecast["status"] in {"recover", "edge"}:
            recovery_subjects += 1

    overall_attendance = round(sum(attendance_values) / len(attendance_values), 1) if attendance_values else 0
    safe_subjects = len(subject_ids) - subjects_at_risk

    cur.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE date >= date('now', '-6 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    total, present = cur.fetchone()
    present = present or 0
    weekly_attendance = round((present / total) * 100) if total else 0

    cur.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE date BETWEEN date('now', '-13 days') AND date('now', '-7 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    last_total, last_present = cur.fetchone()
    last_present = last_present or 0
    last_week_attendance = round((last_present / last_total) * 100) if last_total else weekly_attendance

    if overall_attendance == 0:
        attendance_insight = "No attendance data yet"
    elif overall_attendance < 75:
        attendance_insight = "Attendance is critically low"
    elif overall_attendance < 80:
        attendance_insight = "Attendance needs attention"
    elif weekly_attendance > last_week_attendance:
        attendance_insight = "You attended more classes than last week"
    elif weekly_attendance < last_week_attendance:
        attendance_insight = "Attendance dropped compared to last week"
    else:
        attendance_insight = "Attendance stayed the same as last week"

    cur.execute(
        """
        SELECT COUNT(*) FROM deadlines
        WHERE completed = 0
          AND due_date BETWEEN date('now') AND date('now', '+7 days')
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    urgent_deadlines = cur.fetchone()[0] or 0

    cur.execute(
        """
        SELECT COUNT(*) FROM timetable
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
          AND (
                weekday = ?
                OR (is_extra = 1 AND class_date = ?)
              )
        """,
        (user_id, weekday, today.isoformat()),
    )
    result = cur.fetchone()
    todays_classes = result[0] if result else 0

    cur.execute(
        """
        SELECT date,
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)
        FROM attendance
        WHERE date >= date('now', '-6 days')
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        GROUP BY date ORDER BY date
        """,
        (user_id,),
    )
    attendance_trend = [round(row[1]) for row in cur.fetchall() if row[1] is not None]

    exam_countdown = get_exam_countdown(user_id, db, limit=3)
    study_plan = build_study_plan(user_id, db)
    next_study_day = next(
        (day for day in study_plan["days"] if any(not item["completed"] for item in day["items"])),
        None,
    )

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
        attendance_trend=attendance_trend,
        exam_countdown=exam_countdown,
        recovery_subjects=recovery_subjects,
        study_plan=study_plan,
        next_study_day=next_study_day,
    )


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

    min_required = get_user_min_attendance(user_id, db)

    cur.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END)
        FROM attendance
        WHERE strftime('%Y', date) = ? AND strftime('%m', date) = ?
          AND status != 'cancelled'
          AND subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (str(year), f"{month:02d}", user_id),
    )
    total, present = cur.fetchone()
    total = total or 0
    present = present or 0

    monthly_attendance = round((present / total) * 100) if total > 0 else 0
    required_presents = math.ceil(total * min_required / 100)
    can_miss = max(0, total - required_presents)
    month_status = "Attendance at risk" if monthly_attendance < min_required else "Attendance safe"

    today_str = today.isoformat()

    # A class can appear either from the recurring weekday timetable or as a one-off extra class.
    cur.execute(
        """
        SELECT DISTINCT subjects.id, subjects.name
        FROM timetable
        JOIN subjects ON timetable.subject_id = subjects.id
        WHERE subjects.user_id = ?
          AND (
                timetable.weekday = ?
                OR (timetable.is_extra = 1 AND timetable.class_date = ?)
              )
        ORDER BY subjects.name
        """,
        (user_id, weekday_index, today_str),
    )
    today_subjects = cur.fetchall()

    subjects = []
    for subject_id, name in today_subjects:
        forecast = get_attendance_forecast(subject_id, db)
        attendance_pct = forecast["percentage"] if forecast else calculate_attendance_percentage(subject_id, db)
        skip_left = forecast["skip_left"] if forecast else classes_can_skip(subject_id, db)
        cur.execute("SELECT date, status FROM attendance WHERE subject_id = ?", (subject_id,))
        attendance_map = {attendance_date: status for attendance_date, status in cur.fetchall()}
        subjects.append(
            {
                "id": subject_id,
                "name": name,
                "attendance": attendance_pct,
                "skip_left": skip_left,
                "attendance_map": attendance_map,
                "forecast": forecast,
            }
        )

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
        prev_month_name=calendar.month_name[prev_month],
        next_month=next_month,
        next_year=next_year,
        next_month_name=calendar.month_name[next_month],
        weekday=weekday_name,
        monthly_attendance=monthly_attendance,
        month_status=month_status,
        min_required=min_required,
        can_miss=can_miss,
    )


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
    # Replacing the row keeps attendance idempotent for a given subject/date pair.
    cur.execute("DELETE FROM attendance WHERE subject_id = ? AND date = ?", (subject_id, date_val))
    cur.execute(
        "INSERT INTO attendance (subject_id, date, status) VALUES (?, ?, ?)",
        (subject_id, date_val, status),
    )
    db.commit()
    db.close()
    return "", 204


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

    cur.execute(
        """
        SELECT 1
        FROM timetable
        WHERE subject_id = ?
          AND (
                weekday = ?
                OR (is_extra = 1 AND class_date = ?)
              )
        """,
        (subject_id, weekday, today_str),
    )
    if not cur.fetchone():
        db.close()
        return redirect("/attendance")

    cur.execute("SELECT id FROM attendance WHERE subject_id = ? AND date = ?", (subject_id, today_str))
    existing = cur.fetchone()

    if existing:
        cur.execute(
            "UPDATE attendance SET status = ? WHERE subject_id = ? AND date = ?",
            (status, subject_id, today_str),
        )
    else:
        cur.execute(
            "INSERT INTO attendance (subject_id, date, status) VALUES (?, ?, ?)",
            (subject_id, today_str, status),
        )

    db.commit()
    db.close()
    return redirect("/attendance")


@app.route("/attendance-calendar/<int:subject_id>")
def attendance_calendar(subject_id):
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM subjects WHERE id = ? AND user_id = ?",
        (subject_id, session["user_id"]),
    )
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
        month=today.month,
    )


@app.route("/deadlines")
def deadlines():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT deadlines.id,
               deadlines.title,
               deadlines.due_date,
               deadlines.type,
               deadlines.priority,
               deadlines.completed,
               subjects.name
        FROM deadlines
        JOIN subjects ON deadlines.subject_id = subjects.id
        WHERE subjects.user_id = ?
        ORDER BY deadlines.due_date
        """,
        (user_id,),
    )
    deadlines_list = cur.fetchall()
    exam_countdown = get_exam_countdown(user_id, db, limit=6)
    db.close()
    return render_template("deadlines.html", deadlines=deadlines_list, exam_countdown=exam_countdown)


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
        priority = request.form.get("priority", "medium")

        # Double-check ownership server-side so a crafted request cannot attach deadlines to another user's subject.
        cur.execute(
            """
            SELECT 1
            FROM subjects
            WHERE id = ? AND user_id = ?
            """,
            (subject_id, user_id),
        )
        if not cur.fetchone():
            db.close()
            return redirect("/deadlines")

        cur.execute(
            """
            INSERT INTO deadlines (subject_id, title, due_date, type, priority, completed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (subject_id, title, due_date, deadline_type, priority),
        )

        db.commit()
        db.close()
        return redirect("/deadlines")

    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (user_id,))
    subjects = cur.fetchall()
    db.close()

    return render_template("add_deadline.html", subjects=subjects)


@app.route("/deadlines/<int:deadline_id>/toggle", methods=["GET", "POST"])
def toggle_deadline(deadline_id):
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    # Toggling is enough here because the same button handles both "done" and "pending" states.
    cur.execute(
        """
        UPDATE deadlines
        SET completed = CASE completed WHEN 1 THEN 0 ELSE 1 END
        WHERE id = ?
          AND subject_id IN (
              SELECT id FROM subjects WHERE user_id = ?
          )
        """,
        (deadline_id, user_id),
    )
    db.commit()
    db.close()
    return redirect("/deadlines")


@app.route("/deadlines/<int:deadline_id>/delete", methods=["GET", "POST"])
def delete_deadline(deadline_id):
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    # The subquery keeps deletes scoped to deadlines owned by the logged-in user.
    cur.execute(
        """
        DELETE FROM deadlines
        WHERE id = ?
          AND subject_id IN (
              SELECT id FROM subjects WHERE user_id = ?
          )
        """,
        (deadline_id, user_id),
    )
    db.commit()
    db.close()
    return redirect("/deadlines")


def send_email(to_email, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        server.sendmail(msg["From"], [to_email], msg.as_string())


@app.route("/send-weekly-report")
def send_weekly_report():
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT email, name FROM users WHERE email IS NOT NULL")
    users = cur.fetchall()

    for email, name in users:
        subject = "Your Weekly Attendance Report"
        body = f"""
Hello {name},

Here is your weekly attendance summary from College Survivor.

Keep pushing
"""
        send_email(email, subject, body)

    db.close()
    return "Weekly reports sent successfully!"


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


@app.route("/subjects")
def view_subjects():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, name, credits, attendance_required_percent, attendance_weight
        FROM subjects WHERE user_id = ?
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    db.close()

    subjects = [
        {"id": row[0], "name": row[1], "credits": row[2], "required": row[3], "weight": row[4]}
        for row in rows
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
        weight = request.form.get("attendance_weight", 1)
        cur.execute(
            """
            INSERT INTO subjects (
                user_id, name, credits, attendance_required_percent, attendance_weight, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, credits, attendance_req, weight, date.today().isoformat()),
        )
        db.commit()
        db.close()
        return redirect("/subjects")

    db.close()
    return render_template("add_subject.html")


@app.route("/edit-subject/<int:subject_id>", methods=["GET", "POST"])
def edit_subject(subject_id):
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form["name"]
        credits = request.form["credits"]
        attendance_req = request.form["attendance_required"]
        weight = request.form.get("attendance_weight", 1)
        cur.execute(
            """
            UPDATE subjects
            SET name = ?, credits = ?, attendance_required_percent = ?, attendance_weight = ?
            WHERE id = ? AND user_id = ?
            """,
            (name, credits, attendance_req, weight, subject_id, user_id),
        )
        db.commit()
        db.close()
        return redirect("/subjects")

    cur.execute(
        """
        SELECT id, name, credits, attendance_required_percent, attendance_weight
        FROM subjects
        WHERE id = ? AND user_id = ?
        """,
        (subject_id, user_id),
    )
    subject = cur.fetchone()
    db.close()

    if not subject:
        return redirect("/subjects")

    return render_template("edit_subject.html", subject=subject)


@app.route("/delete-subject/<int:subject_id>")
def delete_subject(subject_id):
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT 1 FROM subjects WHERE id = ? AND user_id = ?", (subject_id, session["user_id"]))
    if not cur.fetchone():
        db.close()
        return redirect("/subjects")

    cur.execute("DELETE FROM attendance WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM timetable WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM deadlines WHERE subject_id = ?", (subject_id,))
    cur.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    db.commit()
    db.close()
    return redirect("/subjects")


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
        cur.execute(
            "DELETE FROM timetable WHERE subject_id = ? AND user_id = ? AND is_extra = 0",
            (subject_id, user_id),
        )
        for day in weekdays:
            cur.execute(
                "INSERT INTO timetable (subject_id, weekday, user_id, is_extra) VALUES (?, ?, ?, 0)",
                (subject_id, int(day), user_id),
            )
        db.commit()
        db.close()
        return redirect("/timetable")

    cur.execute("SELECT id, name FROM subjects WHERE user_id = ?", (user_id,))
    subjects = cur.fetchall()

    cur.execute("SELECT subject_id, weekday FROM timetable WHERE user_id = ? AND is_extra = 0", (user_id,))
    timetable_map = {}
    for subject_id, weekday in cur.fetchall():
        timetable_map.setdefault(subject_id, []).append(weekday)

    db.close()
    return render_template("timetable.html", subjects=subjects, timetable_map=timetable_map)


@app.route("/study-planner")
def study_planner():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    plan = build_study_plan(user_id, db)
    exam_countdown = get_exam_countdown(user_id, db, limit=4)
    db.close()

    return render_template(
        "study_planner.html",
        plan=plan,
        exam_countdown=exam_countdown,
    )


@app.route("/study-planner/toggle", methods=["POST"])
def toggle_study_planner_item():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    session_key = request.form.get("session_key")
    completed = 1 if request.form.get("completed") == "1" else 0
    if not session_key:
        return redirect("/study-planner")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO study_plan_progress (user_id, session_key, completed, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, session_key)
        DO UPDATE SET completed = excluded.completed, updated_at = excluded.updated_at
        """,
        (user_id, session_key, completed, datetime.now().isoformat(timespec="seconds")),
    )
    db.commit()
    db.close()
    return redirect("/study-planner")


@app.route("/study-planner/reset", methods=["POST"])
def reset_study_planner_progress():
    if not require_login():
        return redirect("/login")

    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM study_plan_progress WHERE user_id = ?", (session["user_id"],))
    db.commit()
    db.close()
    return redirect("/study-planner")


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        new_name = request.form.get("name")
        new_email = request.form.get("email")
        cur.execute(
            "UPDATE users SET name = ?, email = ? WHERE id = ?",
            (new_name, new_email, user_id),
        )
        db.commit()

    cur.execute("SELECT name, email, created_at FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()

    cur.execute("SELECT COUNT(*) FROM subjects WHERE user_id = ?", (user_id,))
    total_subjects = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*) FROM deadlines
        WHERE subject_id IN (
            SELECT id FROM subjects WHERE user_id = ?
        )
        """,
        (user_id,),
    )
    total_deadlines = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*) FROM attendance
        WHERE subject_id IN (
            SELECT id FROM subjects WHERE user_id = ?
        )
        AND status != 'cancelled'
        """,
        (user_id,),
    )
    total_attendance = cur.fetchone()[0]

    cur.execute(
        """
        SELECT date,
               SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)
        FROM attendance
        WHERE status != 'cancelled'
          AND subject_id IN (
              SELECT id FROM subjects WHERE user_id = ?
          )
        GROUP BY date
        ORDER BY date
        """,
        (user_id,),
    )
    attendance_trend = [round(row[1]) for row in cur.fetchall() if row[1] is not None]

    db.close()

    return render_template(
        "profile.html",
        user=user,
        total_subjects=total_subjects,
        total_deadlines=total_deadlines,
        total_attendance=total_attendance,
        attendance_trend=attendance_trend,
    )


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
            db.close()
            return "Missing form data"

        if password != confirm:
            db.close()
            return "Passwords do not match"

        hashed_password = generate_password_hash(password)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur.execute("SELECT id FROM users WHERE name = ?", (name,))
        existing = cur.fetchone()
        if existing:
            db.close()
            return "User already exists"

        cur.execute(
            "INSERT INTO users (name, email, password, created_at) VALUES (?, ?, ?, ?)",
            (name, email, hashed_password, created_at),
        )
        db.commit()
        db.close()
        return redirect("/login")

    db.close()
    return render_template("register.html")


@app.route("/add-extra-class", methods=["POST"])
def add_extra_class():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    subject_id = request.form["subject_id"]
    class_date = request.form["class_date"]

    db = get_db()
    cur = db.cursor()

    cur.execute(
        """
        INSERT INTO timetable (subject_id, weekday, user_id, is_extra, class_date)
        VALUES (?, ?, ?, 1, ?)
        """,
        (subject_id, -1, user_id, class_date),
    )

    db.commit()
    db.close()

    return redirect("/attendance")


@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    cur = db.cursor()

    if request.method == "POST":
        name = request.form.get("name")
        password = request.form.get("password")

        cur.execute("SELECT id, password FROM users WHERE name = ?", (name,))
        user = cur.fetchone()

        if user and check_password_hash(user[1], password):
            session["user_id"] = user[0]
            db.close()
            return redirect("/dashboard")

        db.close()
        return "Invalid username or password"

    db.close()
    return render_template("login.html")


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        new_password = request.form.get("password", "")

        if not username or not new_password:
            return render_template("forgot_password.html", error="Please fill in all fields.")

        hashed_password = generate_password_hash(new_password)
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE users SET password = ? WHERE name = ?", (hashed_password, username))
        db.commit()
        db.close()
        return redirect("/login")

    return render_template("forgot_password.html")


@app.route("/delete-account", methods=["POST"])
def delete_account():
    if not require_login():
        return redirect("/login")

    user_id = session["user_id"]
    db = get_db()
    cur = db.cursor()

    cur.execute(
        """
        DELETE FROM attendance
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    cur.execute(
        """
        DELETE FROM deadlines
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    cur.execute(
        """
        DELETE FROM timetable
        WHERE subject_id IN (SELECT id FROM subjects WHERE user_id = ?)
        """,
        (user_id,),
    )
    cur.execute("DELETE FROM subjects WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM settings WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM click_log WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    db.close()
    session.clear()
    return redirect("/register")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def database_usable(path):
    try:
        probe = sqlite3.connect(path)
        probe.execute("PRAGMA schema_version")
        probe.close()
        return True
    except sqlite3.Error:
        return False


def ensure_database_ready():
    global DB_PATH

    if not os.path.exists(DB_PATH):
        return None

    if database_usable(DB_PATH):
        return None

    backup_path = f"{DB_PATH}.corrupt-{datetime.now():%Y%m%d-%H%M%S}.bak"
    try:
        # Best case: preserve the broken file so it can still be inspected or recovered later.
        os.replace(DB_PATH, backup_path)
        return f"Corrupt primary database moved to: {backup_path}"
    except OSError:
        # Fallback: switch to a fresh database path if the synced folder refuses the rename.
        DB_PATH = os.path.abspath(f"college_recovered_{datetime.now():%Y%m%d_%H%M%S}.db")
        return f"Primary database is unreadable. Using fallback database at: {DB_PATH}"


def init_db():
    backup_path = ensure_database_ready()
    if backup_path:
        print(f"Database recovery: {backup_path}")

    db = get_db()
    with open("schema.sql", "r", encoding="utf-8") as schema_file:
        db.executescript(schema_file.read())

    # These guards let the app open both fresh databases and older local copies without manual SQL fixes.
    ensure_column(db, "subjects", "attendance_weight", "INTEGER DEFAULT 1")
    ensure_column(db, "timetable", "user_id", "INTEGER")
    ensure_column(db, "timetable", "is_extra", "INTEGER DEFAULT 0")
    ensure_column(db, "timetable", "class_date", "TEXT")
    ensure_column(db, "deadlines", "priority", "TEXT DEFAULT 'medium'")

    if table_exists(db, "subject"):
        db.execute(
            """
            INSERT OR IGNORE INTO subjects (
                id, user_id, name, credits, attendance_required_percent, attendance_weight, created_at
            )
            SELECT id, user_id, name, credits, attendance_required_percent, attendance_weight, created_at
            FROM subject
            """
        )

    if table_exists(db, "deadline"):
        db.execute(
            """
            INSERT OR IGNORE INTO deadlines (
                id, subject_id, title, due_date, type, completed
            )
            SELECT id, subject_id, title, due_date, type, completed
            FROM deadline
            """
        )

    db.execute(
        """
        UPDATE timetable
        SET user_id = (
            SELECT subjects.user_id
            FROM subjects
            WHERE subjects.id = timetable.subject_id
        )
        WHERE user_id IS NULL
        """
    )

    db.commit()
    db.close()


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)

