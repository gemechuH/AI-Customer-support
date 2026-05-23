from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn
import os
import json
import sqlite3
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

app = FastAPI(title="AI Voice Assistant API")

# --- Config ---
CREDENTIALS_FILE = "ai-customer-support-for-dental-97534c20c8ce.json"
CALENDAR_ID = "gemechuhunduma20@gmail.com"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
BUSINESS_START = 9   # 9 AM
BUSINESS_END = 17    # 5 PM

# --- Google Calendar Setup ---
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)

# --- SQLite Database Setup ---
def init_db():
    conn = sqlite3.connect("appointments.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id TEXT UNIQUE,
            patient_name TEXT,
            phone TEXT,
            appointment_time TEXT,
            booked_at TEXT
        )
    """)
    # Add appointment_id column if upgrading from old DB
    try:
        conn.execute("ALTER TABLE appointments ADD COLUMN appointment_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

def generate_appointment_id():
    # Clear letters only (no I, O, Q, V, Z — confusing on phone)
    letters = "ABCDEFGHJKMNPRSTUWXY"
    # Clear digits only (no 0, 1 — look like O and I)
    digits = "23456789"
    code = (
        random.choice(letters) +
        random.choice(letters) +
        random.choice(digits) +
        random.choice(digits)
    )
    return code

def spell_id(appt_id: str) -> str:
    return " - ".join(list(appt_id))

init_db()

# --- Get busy times from Google Calendar ---
def fetch_busy_times():
    service = get_calendar_service()
    now = datetime.utcnow()
    week_later = now + timedelta(days=7)
    body = {
        "timeMin": now.isoformat() + "Z",
        "timeMax": week_later.isoformat() + "Z",
        "items": [{"id": CALENDAR_ID}]
    }
    result = service.freebusy().query(body=body).execute()
    return result["calendars"][CALENDAR_ID]["busy"], now, week_later

# --- Check availability for a specific day ---
def check_day_availability(day_name: str, busy_times: list, now: datetime, week_later: datetime):
    day_slots = []
    current = now.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    if current < now:
        current += timedelta(days=1)

    while current <= week_later:
        if current.strftime("%A").lower() == day_name.lower() and current.weekday() < 5:
            slot_end = current + timedelta(hours=1)
            is_busy = any(
                datetime.fromisoformat(b["start"].replace("Z", "")) < slot_end and
                datetime.fromisoformat(b["end"].replace("Z", "")) > current
                for b in busy_times
            )
            day_slots.append((current.strftime("%I:%M %p"), is_busy))
        current += timedelta(hours=1)

    return day_slots

# --- Main calendar check function ---
def get_available_slots(requested_day: str = None):
    try:
        busy_times, now, week_later = fetch_busy_times()

        # If a specific day was requested
        if requested_day:
            day_slots = check_day_availability(requested_day, busy_times, now, week_later)

            if not day_slots:
                return f"{requested_day} is not a working day or is outside this week. Please choose Monday to Friday."

            open_slots = [t for t, busy in day_slots if not busy]
            closed_slots = [t for t, busy in day_slots if busy]

            if not open_slots:
                # Fully closed day
                # Find next available days
                available_days = []
                check = now.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
                if check < now:
                    check += timedelta(days=1)
                seen_days = set()
                while check <= week_later and len(available_days) < 3:
                    dname = check.strftime("%A")
                    if dname.lower() != requested_day.lower() and check.weekday() < 5 and dname not in seen_days:
                        slot_end = check + timedelta(hours=1)
                        is_busy = any(
                            datetime.fromisoformat(b["start"].replace("Z", "")) < slot_end and
                            datetime.fromisoformat(b["end"].replace("Z", "")) > check
                            for b in busy_times
                        )
                        if not is_busy:
                            available_days.append(f"{dname} at {check.strftime('%I:%M %p')}")
                            seen_days.add(dname)
                    check += timedelta(hours=1)

                msg = f"{requested_day} is fully closed."
                if available_days:
                    msg += f" Next available times are: {', '.join(available_days)}."
                return msg

            elif len(closed_slots) > 0:
                # Partially open
                return (
                    f"{requested_day} is partially available. "
                    f"Open slots: {', '.join(open_slots)}. "
                    f"Booked slots: {', '.join(closed_slots)}."
                )
            else:
                # Fully open
                return f"{requested_day} is fully open. Available times: {', '.join(open_slots)}."

        # No specific day — return next 5 available slots across the week
        available = []
        closed_days = {}
        current = now.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
        if current < now:
            current += timedelta(days=1)

        day_slot_counts = {}

        while current <= week_later:
            if current.weekday() < 5:
                dname = current.strftime("%A")
                slot_end = current + timedelta(hours=1)
                is_busy = any(
                    datetime.fromisoformat(b["start"].replace("Z", "")) < slot_end and
                    datetime.fromisoformat(b["end"].replace("Z", "")) > current
                    for b in busy_times
                )
                if dname not in day_slot_counts:
                    day_slot_counts[dname] = {"total": 0, "busy": 0}
                day_slot_counts[dname]["total"] += 1
                if is_busy:
                    day_slot_counts[dname]["busy"] += 1
                elif len(available) < 5:
                    available.append(f"{dname} {current.strftime('%I:%M %p')}")
            current += timedelta(hours=1)

        for day, counts in day_slot_counts.items():
            if counts["total"] > 0 and counts["busy"] == counts["total"]:
                closed_days[day] = True

        parts = []
        if closed_days:
            parts.append(f"{', '.join(closed_days.keys())} {'is' if len(closed_days) == 1 else 'are'} fully closed this week.")
        if available:
            parts.append(f"Available times: {', '.join(available)}.")
        else:
            parts.append("No available slots this week.")

        return " ".join(parts)

    except Exception as e:
        print(f"Calendar error: {e}")
        return "Available times: Tuesday 2:00 PM, Wednesday 11:00 AM, Thursday 10:00 AM."

# --- Cancel appointment ---
def cancel_appointment_from_calendar(appointment_id: str):
    try:
        conn = sqlite3.connect("appointments.db")
        row = conn.execute(
            "SELECT id, patient_name, appointment_time FROM appointments WHERE appointment_id = ?",
            (appointment_id.upper(),)
        ).fetchone()

        if not row:
            conn.close()
            return False, "No appointment found for this ID."

        db_id, name, appt_time = row
        conn.execute("DELETE FROM appointments WHERE id = ?", (db_id,))
        conn.commit()
        conn.close()

        # Delete from Google Calendar
        service = get_calendar_service()
        now = datetime.utcnow()
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat() + "Z",
            q=name,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        for event in events_result.get("items", []):
            if name.lower() in event.get("summary", "").lower():
                service.events().delete(calendarId=CALENDAR_ID, eventId=event["id"]).execute()
                print(f"🗑 Deleted calendar event for {name}")
                break

        print(f"✅ Cancelled: {name} | {appt_time} | ID: {appointment_id}")
        return True, appt_time

    except Exception as e:
        print(f"Cancel error: {e}")
        return False, str(e)

# --- Get existing appointment ---
def get_patient_appointment(appointment_id: str):
    try:
        conn = sqlite3.connect("appointments.db")
        row = conn.execute(
            "SELECT appointment_id, patient_name, appointment_time FROM appointments WHERE appointment_id = ?",
            (appointment_id.upper(),)
        ).fetchone()
        conn.close()
        if row:
            return True, row[1], row[2]
        return False, None, None
    except Exception as e:
        print(f"Lookup error: {e}")
        return False, None, None

# --- Reschedule appointment ---
def reschedule_appointment(appointment_id: str, new_time: str):
    try:
        conn = sqlite3.connect("appointments.db")
        row = conn.execute(
            "SELECT id, patient_name, phone, appointment_time FROM appointments WHERE appointment_id = ?",
            (appointment_id.upper(),)
        ).fetchone()

        if not row:
            conn.close()
            return False, "No appointment found for this ID."

        db_id, name, phone, old_time = row
        conn.execute(
            "UPDATE appointments SET appointment_time = ?, booked_at = ? WHERE id = ?",
            (new_time, datetime.utcnow().isoformat(), db_id)
        )
        conn.commit()
        conn.close()

        # Delete old Google Calendar event
        service = get_calendar_service()
        now = datetime.utcnow()
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now.isoformat() + "Z",
            q=name,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        for event in events_result.get("items", []):
            if name.lower() in event.get("summary", "").lower():
                service.events().delete(calendarId=CALENDAR_ID, eventId=event["id"]).execute()
                break

        # Create new Google Calendar event
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        parts = new_time.split()
        if len(parts) >= 3:
            day_name = parts[0]
            time_part = parts[1] + " " + parts[2]
            target_weekday = days.index(day_name) if day_name in days else 0
            days_ahead = (target_weekday - now.weekday()) % 7 or 7
            event_date = now + timedelta(days=days_ahead)
            event_start = datetime.strptime(
                f"{event_date.strftime('%Y-%m-%d')} {time_part}", "%Y-%m-%d %I:%M %p"
            )
        else:
            event_start = now + timedelta(hours=1)

        event_end = event_start + timedelta(hours=1)
        event = {
            "summary": f"Dental Appointment - {name}",
            "description": f"Patient: {name}\nPhone: {phone}\nAppointment ID: {appointment_id}\nRescheduled from: {old_time}",
            "start": {"dateTime": event_start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": event_end.isoformat(), "timeZone": "UTC"},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"✅ Rescheduled {name} from {old_time} to {new_time} | ID: {appointment_id}")
        return True, old_time

    except Exception as e:
        print(f"Reschedule error: {e}")
        return False, str(e)

# --- Book appointment ---
def book_appointment_on_calendar(patient_name: str, phone: str, appointment_time: str):
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        parts = appointment_time.split()
        if len(parts) >= 3:
            day_name = parts[0]
            time_part = parts[1] + " " + parts[2]
            target_weekday = days.index(day_name) if day_name in days else 0
            days_ahead = (target_weekday - now.weekday()) % 7 or 7
            event_date = now + timedelta(days=days_ahead)
            event_start = datetime.strptime(
                f"{event_date.strftime('%Y-%m-%d')} {time_part}", "%Y-%m-%d %I:%M %p"
            )
        else:
            event_start = now + timedelta(hours=1)

        event_end = event_start + timedelta(hours=1)
        event = {
            "summary": f"Dental Appointment - {patient_name}",
            "description": f"Patient: {patient_name}\nPhone: {phone}",
            "start": {"dateTime": event_start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": event_end.isoformat(), "timeZone": "UTC"},
        }
        appt_id = generate_appointment_id()
        event = {
            "summary": f"Dental Appointment - {patient_name}",
            "description": f"Patient: {patient_name}\nPhone: {phone}\nAppointment ID: {appt_id}",
            "start": {"dateTime": event_start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": event_end.isoformat(), "timeZone": "UTC"},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        conn = sqlite3.connect("appointments.db")
        conn.execute(
            "INSERT INTO appointments (appointment_id, patient_name, phone, appointment_time, booked_at) VALUES (?, ?, ?, ?, ?)",
            (appt_id, patient_name, phone, appointment_time, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
        print(f"📅 Booked: {patient_name} at {appointment_time} | ID: {appt_id}")
        return True, appt_id
    except Exception as e:
        print(f"Booking error: {e}")
        return False, None

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def root():
    conn = sqlite3.connect("appointments.db")
    rows = conn.execute("SELECT * FROM appointments ORDER BY booked_at DESC").fetchall()
    conn.close()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    total = len(rows)
    today_count = sum(1 for r in rows if r[4] and r[4].startswith(today))

    rows_html = ""
    for i, r in enumerate(rows):
        booked_raw = r[5] or ""
        try:
            booked_fmt = datetime.fromisoformat(booked_raw).strftime("%b %d, %Y %I:%M %p")
        except Exception:
            booked_fmt = booked_raw
        badge = "badge-today" if booked_raw.startswith(today) else "badge-past"
        label = "Today" if booked_raw.startswith(today) else "Booked"
        appt_id = r[1] or "—"
        rows_html += f"""
        <tr>
            <td><span class="row-num">{i + 1}</span></td>
            <td><span class="id-badge">{appt_id}</span></td>
            <td><div class="patient-name">{r[2] or "—"}</div></td>
            <td><span class="phone-badge">📞 {r[3] or "—"}</span></td>
            <td><span class="time-badge">🗓 {r[4] or "—"}</span></td>
            <td><span class="status-badge {badge}">{label}</span></td>
            <td><span class="date-text">{booked_fmt}</span></td>
        </tr>"""

    empty_state = "" if rows else """
        <tr><td colspan="6">
            <div class="empty-state">
                <div class="empty-icon">🦷</div>
                <div>No appointments yet</div>
                <div class="empty-sub">Appointments will appear here after patients call</div>
            </div>
        </td></tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Oneofi Dental Clinic — Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #0f1117;
            color: #e2e8f0;
            min-height: 100vh;
        }}
        .topbar {{
            background: linear-gradient(135deg, #1a1f2e 0%, #16213e 100%);
            border-bottom: 1px solid #2d3748;
            padding: 0 32px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            height: 64px;
            position: sticky;
            top: 0;
            z-index: 100;
            backdrop-filter: blur(10px);
        }}
        .logo {{ display: flex; align-items: center; gap: 12px; }}
        .logo-icon {{
            width: 36px; height: 36px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            border-radius: 10px;
            display: flex; align-items: center; justify-content: center;
            font-size: 18px;
        }}
        .logo-text {{ font-size: 17px; font-weight: 700; color: #fff; }}
        .logo-sub {{ font-size: 11px; color: #718096; margin-top: 1px; }}
        .topbar-right {{ display: flex; align-items: center; gap: 16px; }}
        .live-dot {{
            width: 8px; height: 8px; border-radius: 50%;
            background: #48bb78;
            box-shadow: 0 0 8px #48bb78;
            animation: pulse 2s infinite;
        }}
        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.4; }}
        }}
        .live-text {{ font-size: 12px; color: #48bb78; font-weight: 500; }}
        .refresh-btn {{
            background: #2d3748; border: 1px solid #4a5568;
            color: #a0aec0; padding: 6px 14px; border-radius: 8px;
            font-size: 12px; cursor: pointer; transition: all 0.2s;
            text-decoration: none; display: flex; align-items: center; gap: 6px;
        }}
        .refresh-btn:hover {{ background: #4a5568; color: #fff; }}

        .main {{ padding: 32px; max-width: 1200px; margin: 0 auto; }}

        .page-header {{ margin-bottom: 28px; }}
        .page-title {{ font-size: 26px; font-weight: 700; color: #fff; }}
        .page-sub {{ font-size: 14px; color: #718096; margin-top: 4px; }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 28px;
        }}
        .stat-card {{
            background: #1a1f2e;
            border: 1px solid #2d3748;
            border-radius: 14px;
            padding: 20px 24px;
            transition: transform 0.2s;
        }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-label {{ font-size: 12px; color: #718096; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }}
        .stat-value {{ font-size: 36px; font-weight: 800; margin-top: 6px; }}
        .stat-icon {{ font-size: 22px; margin-bottom: 8px; }}
        .stat-total .stat-value {{ color: #667eea; }}
        .stat-today .stat-value {{ color: #48bb78; }}
        .stat-api .stat-value {{ color: #ed8936; font-size: 14px; margin-top: 10px; }}

        .table-card {{
            background: #1a1f2e;
            border: 1px solid #2d3748;
            border-radius: 16px;
            overflow: hidden;
        }}
        .table-header {{
            padding: 20px 24px;
            border-bottom: 1px solid #2d3748;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .table-title {{ font-size: 15px; font-weight: 600; color: #fff; }}
        .table-count {{
            background: #2d3748; color: #a0aec0;
            font-size: 12px; padding: 3px 10px;
            border-radius: 20px; font-weight: 500;
        }}
        table {{ width: 100%; border-collapse: collapse; }}
        thead tr {{ background: #16213e; }}
        th {{
            text-align: left; padding: 12px 20px;
            font-size: 11px; font-weight: 600;
            color: #718096; text-transform: uppercase;
            letter-spacing: 0.8px; border-bottom: 1px solid #2d3748;
        }}
        td {{ padding: 14px 20px; border-bottom: 1px solid #1e2535; vertical-align: middle; }}
        tr:last-child td {{ border-bottom: none; }}
        tbody tr:hover {{ background: #16213e; transition: background 0.15s; }}
        .row-num {{
            width: 26px; height: 26px; border-radius: 6px;
            background: #2d3748; color: #718096;
            font-size: 12px; font-weight: 600;
            display: inline-flex; align-items: center; justify-content: center;
        }}
        .id-badge {{
            background: linear-gradient(135deg, #2d3748, #1a202c);
            color: #f6e05e; border: 1px solid #744210;
            padding: 4px 10px; border-radius: 6px;
            font-size: 12px; font-weight: 700; letter-spacing: 1px;
            font-family: monospace;
        }}
        .patient-name {{ font-weight: 600; color: #e2e8f0; font-size: 14px; }}
        .phone-badge {{
            background: #1e2535; color: #90cdf4;
            padding: 4px 10px; border-radius: 6px;
            font-size: 13px; white-space: nowrap;
        }}
        .time-badge {{
            background: #1e2535; color: #d6bcfa;
            padding: 4px 10px; border-radius: 6px;
            font-size: 13px; white-space: nowrap;
        }}
        .status-badge {{
            padding: 4px 12px; border-radius: 20px;
            font-size: 11px; font-weight: 600; text-transform: uppercase;
        }}
        .badge-today {{ background: #1c4532; color: #68d391; border: 1px solid #2f855a; }}
        .badge-past {{ background: #2d3748; color: #a0aec0; border: 1px solid #4a5568; }}
        .date-text {{ font-size: 12px; color: #718096; }}
        .empty-state {{
            text-align: center; padding: 60px 20px;
            color: #4a5568; font-size: 15px;
        }}
        .empty-icon {{ font-size: 48px; margin-bottom: 12px; }}
        .empty-sub {{ font-size: 13px; color: #4a5568; margin-top: 6px; }}
        .footer {{
            text-align: center; padding: 24px;
            font-size: 12px; color: #4a5568;
        }}
    </style>
</head>
<body>
    <div class="topbar">
        <div class="logo">
            <div class="logo-icon">🦷</div>
            <div>
                <div class="logo-text">Oneofi Dental Clinic</div>
                <div class="logo-sub">AI Receptionist Dashboard</div>
            </div>
        </div>
        <div class="topbar-right">
            <div class="live-dot"></div>
            <span class="live-text">Live</span>
            <a href="/" class="refresh-btn">↻ Refresh</a>
        </div>
    </div>

    <div class="main">
        <div class="page-header">
            <div class="page-title">Appointments</div>
            <div class="page-sub">All bookings made through the AI voice receptionist</div>
        </div>

        <div class="stats-grid">
            <div class="stat-card stat-total">
                <div class="stat-icon">📋</div>
                <div class="stat-label">Total Appointments</div>
                <div class="stat-value">{total}</div>
            </div>
            <div class="stat-card stat-today">
                <div class="stat-icon">📅</div>
                <div class="stat-label">Booked Today</div>
                <div class="stat-value">{today_count}</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🤖</div>
                <div class="stat-label">API Status</div>
                <div class="stat-value" style="color:#48bb78; font-size:18px; margin-top:10px;">● Online</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🗓</div>
                <div class="stat-label">Today's Date</div>
                <div class="stat-value" style="color:#ed8936; font-size:16px; margin-top:8px;">{datetime.utcnow().strftime("%b %d, %Y")}</div>
            </div>
        </div>

        <div class="table-card">
            <div class="table-header">
                <span class="table-title">Patient Bookings</span>
                <span class="table-count">{total} total</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Appointment ID</th>
                        <th>Patient Name</th>
                        <th>Phone</th>
                        <th>Appointment Time</th>
                        <th>Status</th>
                        <th>Booked At</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                    {empty_state}
                </tbody>
            </table>
        </div>
    </div>
    <div class="footer">Oneofi Dental Clinic · AI Voice Receptionist · {datetime.utcnow().strftime("%Y")}</div>
</body>
</html>"""
    return HTMLResponse(content=html)

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.get("/appointments")
async def list_appointments():
    conn = sqlite3.connect("appointments.db")
    rows = conn.execute("SELECT * FROM appointments ORDER BY booked_at DESC").fetchall()
    conn.close()
    appointments = [
        {"id": r[0], "patient_name": r[1], "phone": r[2], "appointment_time": r[3], "booked_at": r[4]}
        for r in rows
    ]
    return {"total_appointments": len(appointments), "appointments": appointments}

@app.post("/api/webhook/voice")
async def voice_ai_webhook(request: Request):
    try:
        payload = await request.json()
        message = payload.get("message", {})
        msg_type = message.get("type")
        print(f"🔔 Incoming: {msg_type}")

        if msg_type == "tool-calls":
            tool_calls = message.get("toolCalls", [])
            tool_results = []

            for tool in tool_calls:
                function_name = tool.get("function", {}).get("name")
                tool_call_id = tool.get("id")
                args = tool.get("function", {}).get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                if function_name == "check_calendar":
                    requested_day = args.get("requested_day", None)
                    print(f"📅 Checking calendar for: {requested_day or 'all week'}")
                    response_text = get_available_slots(requested_day)
                    print(f"✅ {response_text}")
                    tool_results.append({"toolCallId": tool_call_id, "result": response_text})

                elif function_name == "get_appointment":
                    appt_id = args.get("appointment_id", "").upper()
                    found, name, appt_time = get_patient_appointment(appt_id)
                    if found:
                        result_msg = f"Found: {name} is booked for {appt_time} (ID: {appt_id}). Tell the patient their current appointment is on {appt_time}, then ask what new day and time they would like."
                    else:
                        result_msg = f"No appointment found for ID {appt_id}. Ask the patient to double check their appointment ID."
                    tool_results.append({"toolCallId": tool_call_id, "result": result_msg})

                elif function_name == "reschedule_appointment":
                    appt_id = args.get("appointment_id", "").upper()
                    new_time = args.get("new_appointment_time", "Unknown")
                    print(f"🔄 Rescheduling ID {appt_id} to {new_time}")
                    success, old_time = reschedule_appointment(appt_id, new_time)
                    if success:
                        result_msg = f"Appointment {appt_id} rescheduled from {old_time} to {new_time}. Tell the patient their appointment has been updated."
                    else:
                        result_msg = f"Could not find appointment ID {appt_id}. Ask the patient to double check their ID."
                    tool_results.append({"toolCallId": tool_call_id, "result": result_msg})

                elif function_name == "cancel_appointment":
                    appt_id = args.get("appointment_id", "").upper()
                    print(f"🗑 Cancelling appointment ID {appt_id}")
                    success, appt_time = cancel_appointment_from_calendar(appt_id)
                    if success:
                        result_msg = f"Appointment {appt_id} on {appt_time} has been cancelled. Tell the patient their appointment is cancelled and we hope to see them soon."
                    else:
                        result_msg = f"Could not find appointment ID {appt_id}. Ask the patient to double check their ID."
                    tool_results.append({"toolCallId": tool_call_id, "result": result_msg})

                elif function_name == "book_appointment":
                    appt_time = args.get("appointment_time", "Unknown")
                    name = args.get("patient_name", "Unknown")
                    ph = args.get("phone", "Unknown")
                    print(f"✅ Booking: {name} | {ph} | {appt_time}")
                    success, appt_id = book_appointment_on_calendar(name, ph, appt_time)
                    if success:
                        spelled = spell_id(appt_id)
                        result_msg = f"Appointment booked! Tell the patient: Your appointment is confirmed for {appt_time}. Your appointment ID is {spelled}. Please write it down — you will need this ID to cancel or reschedule. Without this ID we cannot make changes to your booking."
                    else:
                        result_msg = "Appointment saved. Tell the customer they are all set."
                    tool_results.append({"toolCallId": tool_call_id, "result": result_msg})

            return {"results": tool_results}

        return {"status": "success"}

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return JSONResponse(status_code=200, content={"results": []})

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
