import sys, io
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import os
import json
import sqlite3
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz
from groq import Groq

EAT = pytz.timezone("Africa/Addis_Ababa")

def now_eat():
    return datetime.now(EAT).replace(tzinfo=None)

from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

app = FastAPI(title="AI Voice Assistant API")

# --- Config ---
CREDENTIALS_FILE = "ai-customer-support-for-dental-97534c20c8ce.json"
CALENDAR_ID = "gemechuhunduma20@gmail.com"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
BUSINESS_START = 0   # fully open 24/7; admin closes specific times via Google Calendar
BUSINESS_END = 24

GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_calendar",
            "description": "Check calendar availability. Use with just a day to show open/closed hours for that day. Use with day AND time to verify one specific slot before booking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "requested_day":  {"type": "string", "description": "Day to check e.g. Monday, today, tomorrow, Saturday"},
                    "requested_time": {"type": "string", "description": "Specific time to check e.g. 10:00 AM. Omit to get a full open/closed summary for the day."}
                },
                "required": ["requested_day"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a dental appointment for the patient.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name":     {"type": "string"},
                    "phone":            {"type": "string"},
                    "appointment_time": {"type": "string", "description": "e.g. Monday 10:00 AM"}
                },
                "required": ["patient_name", "phone", "appointment_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_appointment",
            "description": "Look up an existing appointment by ID.",
            "parameters": {
                "type": "object",
                "properties": {"appointment_id": {"type": "string"}},
                "required": ["appointment_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment by ID.",
            "parameters": {
                "type": "object",
                "properties": {"appointment_id": {"type": "string"}},
                "required": ["appointment_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Reschedule an appointment to a new time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id":       {"type": "string"},
                    "new_appointment_time": {"type": "string", "description": "e.g. Wednesday 2:00 PM"}
                },
                "required": ["appointment_id", "new_appointment_time"]
            }
        }
    }
]

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

def handle_tool_call(name: str, args: dict) -> str:
    if name == "check_calendar":
        return get_available_slots(args.get("requested_day"), args.get("requested_time"))
    elif name == "book_appointment":
        # Guard: don't double-book same patient + time
        conn = sqlite3.connect("appointments.db")
        existing = conn.execute(
            "SELECT appointment_id FROM appointments WHERE patient_name=? AND appointment_time=?",
            (args["patient_name"], args["appointment_time"])
        ).fetchone()
        conn.close()
        if existing:
            return f"This appointment already exists. ID: {spell_id(existing[0])}. No new booking was made."
        success, appt_id = book_appointment_on_calendar(
            args["patient_name"], args["phone"], args["appointment_time"]
        )
        if success:
            spelled = spell_id(appt_id)
            return f"Booked. Appointment ID: {spelled}. Patient must save this ID to cancel or reschedule."
        return "Booking failed. Please try again."
    elif name == "get_appointment":
        found, pname, appt_time = get_patient_appointment(args["appointment_id"])
        if found:
            return f"Found: {pname} is booked for {appt_time}."
        return "No appointment found for that ID."
    elif name == "cancel_appointment":
        success, appt_time = cancel_appointment_from_calendar(args["appointment_id"])
        if success:
            return f"Appointment on {appt_time} has been cancelled."
        return "Could not find an appointment with that ID."
    elif name == "reschedule_appointment":
        success, old_time = reschedule_appointment(
            args["appointment_id"], args["new_appointment_time"]
        )
        if success:
            return f"Rescheduled from {old_time} to {args['new_appointment_time']}."
        return "Could not find an appointment with that ID."
    return "Unknown tool."

init_db()

# --- Get busy times from Google Calendar ---
def fetch_busy_times():
    service = get_calendar_service()
    now_utc = datetime.utcnow()
    week_later_utc = now_utc + timedelta(days=7)
    body = {
        "timeMin": now_utc.isoformat() + "Z",
        "timeMax": week_later_utc.isoformat() + "Z",
        "items": [{"id": CALENDAR_ID}]
    }
    result = service.freebusy().query(body=body).execute()
    raw_busy = result["calendars"][CALENDAR_ID]["busy"]

    # Convert busy times from UTC to EAT for correct local slot comparison
    eat_busy = [
        {
            "start": (datetime.fromisoformat(b["start"].replace("Z", "")) + timedelta(hours=3)).isoformat(),
            "end":   (datetime.fromisoformat(b["end"].replace("Z", ""))   + timedelta(hours=3)).isoformat()
        }
        for b in raw_busy
    ]
    now = now_eat()
    week_later = now + timedelta(days=7)
    return eat_busy, now, week_later

# --- Check availability for a specific day ---
def check_day_availability(day_name: str, busy_times: list, now: datetime, week_later: datetime):
    day_slots = []
    current = now.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    if current < now:
        current += timedelta(days=1)

    while current <= week_later:
        if current.strftime("%A").lower() == day_name.lower():
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
def get_available_slots(requested_day: str = None, requested_time: str = None):
    try:
        busy_times, now, _ = fetch_busy_times()

        def slot_is_busy(slot_start):
            # 2-hour appointment window
            slot_end = slot_start + timedelta(hours=2)
            return any(
                datetime.fromisoformat(b["start"].replace("Z", "")) < slot_end and
                datetime.fromisoformat(b["end"].replace("Z", "")) > slot_start
                for b in busy_times
            )

        def resolve_day(day_name):
            days_map = {d.lower(): i for i, d in enumerate(
                ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"])}
            idx = days_map.get(day_name.lower())
            if idx is None:
                return None
            target = now + timedelta(days=(idx - now.weekday()) % 7)
            if target.date() < now.date():
                target += timedelta(weeks=1)
            return target

        # Both day + time — check that exact slot
        if requested_day and requested_time:
            dn = requested_day.lower()
            if dn in ("today",):
                target = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elif dn in ("tomorrow",):
                target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                target = resolve_day(requested_day)
            if target is None:
                return f"I don't recognise '{requested_day}'. Please use a day name like Monday."
            try:
                slot_start = datetime.strptime(
                    f"{target.strftime('%Y-%m-%d')} {requested_time}", "%Y-%m-%d %I:%M %p"
                )
            except ValueError:
                return f"I couldn't understand the time '{requested_time}'. Please use a format like 10:00 AM."
            if slot_start <= now:
                return "That time has already passed. Please choose a future time."
            if slot_is_busy(slot_start):
                return f"Sorry, {requested_day} at {requested_time} is already reserved. Please choose a different time."
            return f"{requested_day} at {requested_time} is available."

        # Day only — return open and blocked hours summary
        if requested_day:
            dn = requested_day.lower()
            if dn == "today":
                target = now.replace(hour=0, minute=0, second=0, microsecond=0)
                label = "Today"
            elif dn == "tomorrow":
                target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                label = "Tomorrow"
            else:
                target = resolve_day(requested_day)
                label = requested_day.capitalize()
            if target is None:
                return f"I don't recognise '{requested_day}'."

            closed_slots = []
            cur = target.replace(hour=0, minute=0, second=0, microsecond=0)
            while cur < target.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1):
                if cur > now and slot_is_busy(cur):
                    closed_slots.append(cur.strftime("%I:%M %p").lstrip("0"))
                cur += timedelta(hours=1)

            if not closed_slots:
                return f"{label} is fully open — no reserved or blocked times."
            return f"{label} has reserved/blocked times: {', '.join(closed_slots)}. All other times are open."

        return "Which day would you like to check?"

    except Exception as e:
        print(f"Calendar error: {e}")
        return "I had trouble checking the calendar. Please try again."

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
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=datetime.utcnow().isoformat() + "Z",
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
            (new_time, now_eat().isoformat(), db_id)
        )
        conn.commit()
        conn.close()

        # Delete old Google Calendar event
        service = get_calendar_service()
        now = now_eat()
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=datetime.utcnow().isoformat() + "Z",
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

        event_end = event_start + timedelta(hours=2)
        event = {
            "summary": f"Dental Appointment - {name}",
            "description": f"Patient: {name}\nPhone: {phone}\nAppointment ID: {appointment_id}\nRescheduled from: {old_time}",
            "start": {"dateTime": event_start.isoformat(), "timeZone": "Africa/Addis_Ababa"},
            "end": {"dateTime": event_end.isoformat(), "timeZone": "Africa/Addis_Ababa"},
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
        now = now_eat()
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

        event_end = event_start + timedelta(hours=2)
        appt_id = generate_appointment_id()
        event = {
            "summary": f"Dental Appointment - {patient_name}",
            "description": f"Patient: {patient_name}\nPhone: {phone}\nAppointment ID: {appt_id}",
            "start": {"dateTime": event_start.isoformat(), "timeZone": "Africa/Addis_Ababa"},
            "end": {"dateTime": event_end.isoformat(), "timeZone": "Africa/Addis_Ababa"},
        }
        service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        conn = sqlite3.connect("appointments.db")
        conn.execute(
            "INSERT INTO appointments (appointment_id, patient_name, phone, appointment_time, booked_at) VALUES (?, ?, ?, ?, ?)",
            (appt_id, patient_name, phone, appointment_time, now_eat().isoformat())
        )
        conn.commit()
        conn.close()
        print(f"📅 Booked: {patient_name} at {appointment_time} | ID: {appt_id}")
        return True, appt_id
    except Exception as e:
        print(f"Booking error: {e}")
        return False, None

# --- Mount static files ---
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Routes ---
@app.get("/")
async def root():
    return FileResponse("static/landing.html")

@app.get("/admin")
async def admin():
    return FileResponse("static/index.html")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.get("/api/vapi-config")
async def vapi_config():
    return {
        "publicKey": os.getenv("VAPI_PUBLIC_KEY", ""),
        "assistantId": os.getenv("VAPI_ASSISTANT_ID", "")
    }

@app.get("/appointments")
async def list_appointments():
    conn = sqlite3.connect("appointments.db")
    rows = conn.execute(
        "SELECT id, patient_name, phone, appointment_time, booked_at, appointment_id FROM appointments ORDER BY booked_at DESC"
    ).fetchall()
    conn.close()
    appointments = [
        {"id": r[0], "patient_name": r[1], "phone": r[2], "appointment_time": r[3], "booked_at": r[4], "appointment_id": r[5]}
        for r in rows
    ]
    return {"total_appointments": len(appointments), "appointments": appointments}

# ── ADMIN SCHEDULE / CALENDAR BLOCKS ──────────────────────────────────────────

@app.get("/api/admin/blocks")
async def get_blocks():
    try:
        service = get_calendar_service()
        now_utc = datetime.utcnow().isoformat() + "Z"
        far_future = (datetime.utcnow() + timedelta(days=60)).isoformat() + "Z"
        result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now_utc,
            timeMax=far_future,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        blocks = []
        for e in result.get("items", []):
            start = e.get("start", {})
            end   = e.get("end", {})
            blocks.append({
                "id":       e["id"],
                "title":    e.get("summary", "Blocked"),
                "start":    start.get("dateTime", start.get("date", "")),
                "end":      end.get("dateTime",   end.get("date", "")),
                "all_day":  "date" in start
            })
        return {"blocks": blocks}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/block")
async def create_block(request: Request):
    try:
        body = await request.json()
        label      = body.get("label", "Clinic Closed")
        date_str   = body.get("date")        # "YYYY-MM-DD"
        start_time = body.get("start_time")  # "HH:MM" 24h, or None for all-day
        end_time   = body.get("end_time")    # "HH:MM" 24h, or None for all-day
        all_day    = body.get("all_day", False)

        service = get_calendar_service()

        if all_day or not start_time or not end_time:
            event = {
                "summary": f"🔒 {label}",
                "start": {"date": date_str},
                "end":   {"date": date_str},
            }
        else:
            event = {
                "summary": f"🔒 {label}",
                "start": {"dateTime": f"{date_str}T{start_time}:00", "timeZone": "Africa/Addis_Ababa"},
                "end":   {"dateTime": f"{date_str}T{end_time}:00",   "timeZone": "Africa/Addis_Ababa"},
            }

        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return {"ok": True, "event_id": created["id"]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/admin/block/{event_id}")
async def delete_block(event_id: str):
    try:
        service = get_calendar_service()
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    try:
        body = await request.json()
        messages = body.get("messages", [])
        system_prompt = open("prompts/system_prompt.txt", encoding="utf-8").read()

        # Build message list for Groq
        groq_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            role = "assistant" if msg["role"] == "assistant" else "user"
            groq_messages.append({"role": role, "content": msg["content"]})

        # Agentic loop — handle tool calls until final text reply
        for _ in range(6):
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=groq_messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                max_tokens=1024
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                # Add assistant message with tool calls to history
                groq_messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                        for tc in msg.tool_calls
                    ]
                })
                # Execute each tool and add results
                for tc in msg.tool_calls:
                    fn_args = json.loads(tc.function.arguments)
                    fn_result = handle_tool_call(tc.function.name, fn_args)
                    groq_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": fn_result
                    })
            else:
                reply = msg.content or ""
                messages.append({"role": "assistant", "content": reply})
                return {"reply": reply, "messages": messages}

        return {"reply": "I'm having trouble completing that. Please try again.", "messages": messages}

    except Exception as e:
        import traceback
        print(f"Chat error: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=200, content={"reply": f"Error: {str(e)}", "messages": []})


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
                    requested_time = args.get("requested_time", None)
                    print(f"Checking calendar: {requested_day} {requested_time}")
                    response_text = get_available_slots(requested_day, requested_time)
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
