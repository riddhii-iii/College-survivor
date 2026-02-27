CREATE TABLE user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT UNIQUE,
    password_hash TEXT,
    created_at TEXT
);

CREATE TABLE subject (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    credits INTEGER,
    attendance_required_percent INTEGER,
    created_at TEXT
);

CREATE TABLE attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER,
    date TEXT,
    status TEXT
);

CREATE TABLE deadline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER,
    title TEXT,
    due_date TEXT,
    type TEXT,
    priority TEXT,
    completed INTEGER
);
