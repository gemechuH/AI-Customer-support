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
import google.generativeai as genai

EAT = pytz.timezone("Africa/Addis_Ababa")

def now_eat():
    return datetime.now(EAT).replace(tzinfo=None)

from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = FastAPI(title="AI Voice Assistant API")

# --- Config ---
CREDENTIALS_FILE = "ai-customer-support-for-dental-97534c20c8ce.json"
CALENDAR_ID = "gemechuhunduma20@gmail.com"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
BUSINESS_START = 8   # 8 AM default open; admin closes hours via Google Calendar
BUSINESS_END = 20    # 8 PM default close

GEMINI_TOOLS = genai.protos.Tool(function_declarations=[
    genai.protos.FunctionDeclaration(
        name="check_calendar",
        description="Check if a specific day and time is available for booking. Always call this before booking to confirm the slot is free.",
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={
                "requested_day": genai.protos.Schema(
                    type=genai.protos.Type.STRING,
                    description="Day the patient wants e.g. Monday, Saturday"
                ),
                "requested_time": genai.protos.Schema(
                    type=genai.protos.Type.STRING,
                    description="Time the patient wants e.g. 10:00 AM, 2:30 PM"
                )
            },
            required=["requested_day", "requested_time"]
        )
    ),
    genai.protos.FunctionDeclaration(
        name="book_appointment",
        description="Book a dental appointment for the patient.",
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={
                "patient_name": genai.protos.Schema(type=genai.protos.Type.STRING),
                "phone": genai.protos.Schema(type=genai.protos.Type.STRING),
                "appointment_time": genai.protos.Schema(type=genai.protos.Type.STRING, description="e.g. Monday 10:00 AM")
            },
            required=["patient_name", "phone", "appointment_time"]
        )
    ),
    genai.protos.FunctionDeclaration(
        name="get_appointment",
        description="Look up an existing appointment by ID.",
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={"appointment_id": genai.protos.Schema(type=genai.protos.Type.STRING)},
            required=["appointment_id"]
        )
    ),
    genai.protos.FunctionDeclaration(
        name="cancel_appointment",
        description="Cancel an existing appointment by ID.",
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={"appointment_id": genai.protos.Schema(type=genai.protos.Type.STRING)},
            required=["appointment_id"]
        )
    ),
    genai.protos.FunctionDeclaration(
        name="reschedule_appointment",
        description="Reschedule an appointment to a new time.",
        parameters=genai.protos.Schema(
            type=genai.protos.Type.OBJECT,
            properties={
                "appointment_id": genai.protos.Schema(type=genai.protos.Type.STRING),
                "new_appointment_time": genai.protos.Schema(type=genai.protos.Type.STRING, description="e.g. Wednesday 2:00 PM")
            },
            required=["appointment_id", "new_appointment_time"]
        )
    )
])

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

        # Both day + time given — check that exact slot
        if requested_day and requested_time:
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
            if not (BUSINESS_START <= slot_start.hour < BUSINESS_END):
                return f"Our hours are {BUSINESS_START}:00 AM to {BUSINESS_END % 12 or BUSINESS_END}:00 PM. Please pick a time within those hours."
            if slot_is_busy(slot_start):
                return f"Sorry, {requested_day} at {requested_time} is already reserved. Please choose a different time."
            return f"{requested_day} at {requested_time} is available."

        return "Please tell me which day and time you'd prefer."

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

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    try:
        body = await request.json()
        messages = body.get("messages", [])
        system_prompt = open("prompts/system_prompt.txt", encoding="utf-8").read()

        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            system_instruction=system_prompt,
            tools=[GEMINI_TOOLS]
        )

        # Build history from all but last message
        history = []
        for msg in messages[:-1]:
            role = "model" if msg["role"] == "assistant" else "user"
            history.append({"role": role, "parts": [{"text": msg["content"]}]})

        chat = model.start_chat(history=history)
        user_msg = messages[-1]["content"] if messages else ""

        # Agentic loop — handle tool calls until final text reply
        fn_response = None
        for _ in range(6):
            if fn_response is None:
                response = chat.send_message(user_msg)
            else:
                response = chat.send_message([
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fn_response["name"],
                            response={"result": fn_response["result"]}
                        )
                    )
                ])

            fn_part = next(
                (p.function_call for p in response.parts if hasattr(p, "function_call") and p.function_call.name),
                None
            )

            if fn_part:
                fn_result = handle_tool_call(fn_part.name, dict(fn_part.args))
                fn_response = {"name": fn_part.name, "result": fn_result}
            else:
                reply = response.text
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
