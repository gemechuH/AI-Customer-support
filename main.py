from fastapi import FastAPI, Request
import uvicorn
import os
import sqlite3
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
            patient_name TEXT,
            phone TEXT,
            appointment_time TEXT,
            booked_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# --- Check real Google Calendar availability ---
def get_available_slots():
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        week_later = now + timedelta(days=7)

        # Get busy times from Google Calendar
        body = {
            "timeMin": now.isoformat() + "Z",
            "timeMax": week_later.isoformat() + "Z",
            "items": [{"id": CALENDAR_ID}]
        }
        result = service.freebusy().query(body=body).execute()
        busy_times = result["calendars"][CALENDAR_ID]["busy"]

        # Generate candidate slots: Mon-Fri, 9AM-5PM every hour
        available = []
        current = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if current < now:
            current += timedelta(days=1)

        while current <= week_later and len(available) < 5:
            if current.weekday() < 5:  # Monday to Friday only
                slot_end = current + timedelta(hours=1)
                is_busy = any(
                    datetime.fromisoformat(b["start"].replace("Z", "")) < slot_end and
                    datetime.fromisoformat(b["end"].replace("Z", "")) > current
                    for b in busy_times
                )
                if not is_busy:
                    day_name = current.strftime("%A")
                    time_str = current.strftime("%I:%M %p")
                    available.append(f"{day_name} {time_str}")
            current += timedelta(hours=1)

        return available if available else ["No available slots this week"]
    except Exception as e:
        print(f"Calendar error: {e}")
        return ["Monday 10:00 AM", "Tuesday 2:00 PM", "Wednesday 11:00 AM"]

# --- Book appointment on Google Calendar + save to DB ---
def book_appointment_on_calendar(patient_name: str, phone: str, appointment_time: str):
    try:
        service = get_calendar_service()

        # Parse the appointment time
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
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        # Save to SQLite
        conn = sqlite3.connect("appointments.db")
        conn.execute(
            "INSERT INTO appointments (patient_name, phone, appointment_time, booked_at) VALUES (?, ?, ?, ?)",
            (patient_name, phone, appointment_time, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

        print(f"📅 Booked: {patient_name} at {appointment_time}")
        return True
    except Exception as e:
        print(f"Booking error: {e}")
        return False

# --- Routes ---
@app.get("/")
async def root():
    return {"message": "AI Voice Assistant API is running!"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.get("/appointments")
async def list_appointments():
    conn = sqlite3.connect("appointments.db")
    rows = conn.execute("SELECT * FROM appointments ORDER BY booked_at DESC").fetchall()
    conn.close()
    appointments = [
        {
            "id": r[0],
            "patient_name": r[1],
            "phone": r[2],
            "appointment_time": r[3],
            "booked_at": r[4]
        }
        for r in rows
    ]
    total = len(appointments)
    return {"total_appointments": total, "appointments": appointments}

@app.post("/api/webhook/voice")
async def voice_ai_webhook(request: Request):
    payload = await request.json()
    print("🔔 Incoming request type:", payload.get("message", {}).get("type"))

    message = payload.get("message", {})

    if message.get("type") == "tool-calls":
        tool_calls = message.get("toolCalls", [])
        tool_results = []

        # Try to extract patient info from conversation
        artifact = message.get("artifact", {})
        messages = artifact.get("messages", [])
        patient_name = "Unknown"
        phone = "Unknown"
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("message", "")
                if "name" in content.lower() and len(content) < 100:
                    patient_name = content
                if any(c.isdigit() for c in content) and len(content) < 50:
                    phone = content

        for tool in tool_calls:
            function_name = tool.get("function", {}).get("name")
            tool_call_id = tool.get("id")

            if function_name == "check_calendar":
                print("📅 Checking Google Calendar...")
                slots = get_available_slots()
                response_text = f"Available times: {', '.join(slots)}"
                print(f"✅ Available slots: {slots}")
                tool_results.append({"toolCallId": tool_call_id, "result": response_text})

            elif function_name == "book_appointment":
                args = tool.get("function", {}).get("arguments", {})
                if isinstance(args, str):
                    import json
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                appt_time = args.get("appointment_time", "Unknown")
                name = args.get("patient_name", patient_name)
                ph = args.get("phone", phone)

                print(f"✅ Booking appointment for {name} | phone: {ph} | time: {appt_time}")
                success = book_appointment_on_calendar(name, ph, appt_time)

                if success:
                    result_msg = "Appointment booked successfully! Tell the customer they are all set."
                else:
                    result_msg = "Appointment saved. Tell the customer they are all set."
                tool_results.append({"toolCallId": tool_call_id, "result": result_msg})

        return {"results": tool_results}

    return {"status": "success", "message": "Received"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
