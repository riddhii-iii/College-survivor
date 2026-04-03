#  College Survivor

**Stay organized. Stay ahead.**

College Survivor is a smart academic tracker designed to help students manage attendance, deadlines, and stay in control of their college life.

---

##  Features

*  **Attendance Tracking**
  Monitor subject-wise attendance with real-time percentage insights.

*  **Skip Calculator**
  Know how many classes you can safely skip without falling below required attendance.

*  **Deadline Management**
  Track assignments and upcoming deadlines in one place.

*  **Dashboard Overview**
  Get a quick summary of attendance, risk subjects, and productivity.

*  **Authentication System**
  Secure login and registration with password hashing.

---

##  Tech Stack

* **Backend:** Python, Flask
* **Database:** SQLite
* **Frontend:** HTML, CSS, JavaScript
* **Authentication:** Werkzeug Security

---

##  Project Structure

```
├── app.py
├── schema.sql
├── templates/
├── static/
└── README.md
```

---

##  Setup & Installation

1. **Clone the repository**

```bash
git clone <your-repo-link>
cd <your-repo-folder>
```

2. **Create virtual environment (optional but recommended)**

```bash
python -m venv venv
venv\Scripts\activate   # Windows
```

3. **Install dependencies**

```bash
pip install flask python-dotenv
```

4. **Initialize database**

```bash
sqlite3 college.db < schema.sql
```

5. **Run the application**

```bash
python app.py
```

6. Open in browser:

```
http://127.0.0.1:5000/
```

---

## What I Learned

* Building a full-stack web application using Flask
* Designing and managing relational databases
* Debugging real-world issues (data persistence, authentication bugs)
* Structuring scalable and maintainable code
* Handling edge cases and improving user experience

---

##  Future Improvements

* Email reminders for deadlines
* Advanced analytics & insights
* UI/UX enhancements
* Deployment (Render / Railway / AWS)

---

##  Contributing

Contributions, suggestions, and feedback are welcome!

---

##  Contact

Feel free to connect or reach out for feedback or collaboration.

---

⭐ If you found this project useful, consider giving it a star!
