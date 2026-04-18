import os
import json
import base64
import threading
import urllib.request
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from google.oauth2 import service_account
from googleapiclient.discovery import build
import psycopg2
from psycopg2.extras import RealDictCursor
from twilio.rest import Client as TwilioClient

mcp = FastMCP(
    "Dental MCP",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")

DANISH_DAYS = {
    "Monday": "mandag", "Tuesday": "tirsdag", "Wednesday": "onsdag",
    "Thursday": "torsdag", "Friday": "fredag", "Saturday": "lørdag", "Sunday": "søndag"
}

DANISH_MONTHS = {
    1: "januar", 2: "februar", 3: "marts", 4: "april", 5: "maj", 6: "juni",
    7: "juli", 8: "august", 9: "september", 10: "oktober", 11: "november", 12: "december"
}

ENGLISH_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"
}

CLINIC_HOURS = {
    0: (8, 17),
    1: (8, 17),
    2: (8, 17),
    3: (8, 17),
    4: (8, 17),
}

def is_within_clinic_hours(dt: datetime) -> bool:
    weekday = dt.weekday()
    if weekday not in CLINIC_HOURS:
        return False
    open_hour, close_hour = CLINIC_HOURS[weekday]
    return open_hour <= dt.hour < close_hour

def format_danish_date(dt: datetime) -> str:
    """Formaterer dato på naturligt dansk: 'tirsdag den 21. april kl. 14:00'"""
    day_name = DANISH_DAYS.get(dt.strftime("%A"), dt.strftime("%A"))
    month_name = DANISH_MONTHS.get(dt.month, str(dt.month))
    return f"{day_name} den {dt.day}. {month_name} kl. {dt.strftime('%H:%M')}"

def format_english_date(dt: datetime) -> str:
    """Formats date in natural English: 'Tuesday April 21 at 14:00'"""
    day_name = dt.strftime("%A")
    month_name = ENGLISH_MONTHS.get(dt.month, str(dt.month))
    return f"{day_name} {month_name} {dt.day} at {dt.strftime('%H:%M')}"

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clinics (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS dentists (
            id SERIAL PRIMARY KEY,
            clinic_id INTEGER REFERENCES clinics(id),
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS patients (
            id SERIAL PRIMARY KEY,
            clinic_id INTEGER REFERENCES clinics(id),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            created_by TEXT DEFAULT 'sofia',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id),
            clinic_id INTEGER REFERENCES clinics(id),
            calendar_event_id TEXT,
            appointment_time TIMESTAMP,
            purpose TEXT,
            dentist_name TEXT,
            status TEXT DEFAULT 'booked',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS purpose TEXT;")
    cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dentist_name TEXT;")
    cur.execute("""
        ALTER TABLE patients
        DROP CONSTRAINT IF EXISTS patients_clinic_phone_unique;
    """)
    cur.execute("""
        DELETE FROM patients
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM patients
            GROUP BY clinic_id, phone
        );
    """)
    cur.execute("""
        ALTER TABLE patients
        ADD CONSTRAINT patients_clinic_phone_unique
        UNIQUE (clinic_id, phone);
    """)
    cur.execute("""
        INSERT INTO clinics (id, name, phone, email)
        VALUES (1, 'Tandlæge Test Klinik', '12345678', 'test@tandlaege.dk')
        ON CONFLICT (id) DO NOTHING;
    """)
    cur.execute("""
        INSERT INTO patients (clinic_id, name, phone, created_by)
        VALUES 
            (1, 'Anders Jensen', '12345678', 'import'),
            (1, 'Mette Hansen', '87654321', 'import'),
            (1, 'Lars Nielsen', '11223344', 'import')
        ON CONFLICT (clinic_id, phone) DO NOTHING;
    """)
    cur.execute("""
        INSERT INTO dentists (clinic_id, name)
        VALUES 
            (1, 'Dr. Hansen'),
            (1, 'Dr. Nielsen')
        ON CONFLICT DO NOTHING;
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("[DB] Database initialiseret og duplikater fjernet")

def get_calendar_service():
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
    creds_json = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
    creds = service_account.Credentials.from_service_account_info(
        creds_json, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)

def send_sms(to_phone: str, message: str) -> bool:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        print("[SMS] Twilio ikke konfigureret — skipper SMS")
        return False
    try:
        phone_clean = "".join(c for c in to_phone.strip() if c.isdigit() or c == "+")
        has_plus = phone_clean.startswith("+")
        digits_only = phone_clean.lstrip("+")
        
        if digits_only.startswith("45") and len(digits_only) == 10:
            final_number = "+" + digits_only
        elif len(digits_only) == 8:
            final_number = "+45" + digits_only
        else:
            final_number = phone_clean if has_plus else "+" + digits_only
        
        print(f"[SMS] Normaliseret nummer: {to_phone} → {final_number}")
        
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=final_number
        )
        print(f"[SMS] Sendt til {final_number} — SID: {msg.sid}")
        return True
    except Exception as e:
        print(f"[SMS ERROR] {e}")
        return False

def get_patient_phone(patient_id: int) -> str:
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT phone FROM patients WHERE id = %s", (patient_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row["phone"] if row else ""
    except Exception as e:
        print(f"[ERROR] get_patient_phone: {e}")
        return ""

def keep_alive():
    while True:
        import time
        time.sleep(840)
        try:
            urllib.request.urlopen("https://dental-mcp.onrender.com/mcp")
        except:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

@mcp.tool()
def get_dentists() -> dict:
    """Hent liste over tandlæger på klinikken"""
    print("[TOOL] get_dentists")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM dentists WHERE clinic_id = 1")
        dentists = cur.fetchall()
        cur.close()
        conn.close()
        return {"dentists": [dict(d) for d in dentists]}
    except Exception as e:
        print(f"[ERROR] get_dentists: {e}")
        return {"error": str(e)}

@mcp.tool()
def find_patient(name: str, phone: str) -> dict:
    """Find en eksisterende patient eller opret en ny i databasen"""
    print(f"[TOOL] find_patient: name={name}, phone={phone}")
    try:
        conn = get_db()
        cur = conn.cursor()
        phone_clean = phone.strip().replace(" ", "")
        cur.execute("""
            SELECT id, name, phone, clinic_id, created_by
            FROM patients
            WHERE phone = %s AND clinic_id = 1
        """, (phone_clean,))
        patient = cur.fetchone()
        if patient:
            cur.close()
            conn.close()
            return {
                "found": True,
                "is_new_patient": False,
                "patient": dict(patient)
            }
        cur.execute("""
            INSERT INTO patients (clinic_id, name, phone, created_by)
            VALUES (1, %s, %s, 'sofia')
            ON CONFLICT (clinic_id, phone) DO UPDATE SET name = EXCLUDED.name
            RETURNING id, name, phone, clinic_id, created_by
        """, (name.strip(), phone_clean))
        new_patient = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        print(f"[DB] Ny patient oprettet: {name}")
        return {
            "found": False,
            "is_new_patient": True,
            "patient": dict(new_patient)
        }
    except Exception as e:
        print(f"[ERROR] find_patient: {e}")
        return {"error": str(e)}

@mcp.tool()
def get_patient_bookings(patient_id: int) -> dict:
    """Hent patientens kommende bookinger fra databasen"""
    print(f"[TOOL] get_patient_bookings: patient_id={patient_id}")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                id,
                appointment_time,
                purpose,
                dentist_name,
                status,
                calendar_event_id
            FROM bookings
            WHERE patient_id = %s
            AND status = 'booked'
            AND appointment_time > NOW()
            ORDER BY appointment_time ASC
        """, (patient_id,))
        bookings = cur.fetchall()
        cur.close()
        conn.close()
        if not bookings:
            return {
                "bookings": [],
                "message": "Ingen kommende bookinger fundet"
            }
        result = []
        for b in bookings:
            b = dict(b)
            if b["appointment_time"]:
                b["display"] = format_danish_date(b["appointment_time"])
                b["display_en"] = format_english_date(b["appointment_time"])
            result.append(b)
        return {"bookings": result}
    except Exception as e:
        print(f"[ERROR] get_patient_bookings: {e}")
        return {"error": str(e)}

@mcp.tool()
def get_available_times(preferred_day: str = "", dentist_name: str = "", exclude_slot_id: str = "") -> dict:
    """Hent ledige tider fra Google Calendar — kun inden for klinikkens åbningstider (man-fre 8-17)."""
    print(f"[TOOL] get_available_times: preferred_day={preferred_day}, dentist={dentist_name}, exclude={exclude_slot_id}")
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(days=30)).isoformat() + "Z"
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        available = []
        for event in events:
            title = event.get("summary", "").lower()
            if not title.startswith("ledig tid"):
                continue
            slot_id = event["id"]
            if exclude_slot_id and slot_id == exclude_slot_id:
                continue
            if dentist_name:
                if dentist_name.lower() not in title:
                    continue
            start = event["start"].get("dateTime", "")
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            
            if not is_within_clinic_hours(dt):
                print(f"[FILTER] Sprang over tid uden for åbningstid: {format_danish_date(dt)}")
                continue
            
            if preferred_day:
                day_name_da = DANISH_DAYS.get(dt.strftime("%A"), "").lower()
                day_name_en = dt.strftime("%A").lower()
                if preferred_day.lower() not in day_name_da and preferred_day.lower() not in day_name_en:
                    continue
            display_title = event.get("summary", "Ledig tid")
            available.append({
                "slot_id": slot_id,
                "start": start,
                "dentist": display_title.replace("Ledig tid", "").replace("-", "").strip() or "Første ledige",
                "display": format_danish_date(dt),
                "display_en": format_english_date(dt)
            })
        if not available:
            return {"available_times": [], "message": "Ingen ledige tider fundet"}
        return {"available_times": available}
    except Exception as e:
        print(f"[ERROR] get_available_times: {e}")
        return {"error": str(e), "message": "Teknisk fejl ved hentning af tider"}

@mcp.tool()
def book_appointment(patient_id: int, patient_name: str, slot_id: str, purpose: str = "", is_new_patient: bool = False, dentist_name: str = "", moved_from: str = "", language: str = "da") -> dict:
    """Book en ledig tid og send SMS-bekræftelse. language='da' eller 'en' styrer SMS-sproget."""
    print(f"[TOOL] book_appointment: {patient_name}, slot={slot_id}, purpose={purpose}, dentist={dentist_name}, moved_from={moved_from}, language={language}")
    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=slot_id
        ).execute()
        event["summary"] = patient_name
        if is_new_patient:
            status_line = "NY PATIENT — første besøg"
        else:
            status_line = "Kendt patient"
        description_parts = [
            status_line,
            f"Formål: {purpose if purpose else 'Ikke angivet'}",
            f"Tandlæge: {dentist_name if dentist_name else 'Første ledige'}"
        ]
        if moved_from:
            description_parts.append(f"Flyttet fra: {moved_from}")
        if language == "en":
            description_parts.append("Sprog: Engelsk")
        event["description"] = "\n".join(description_parts)
        updated_event = service.events().update(
            calendarId=CALENDAR_ID,
            eventId=slot_id,
            body=event
        ).execute()
        start = updated_event["start"].get("dateTime", "")
        appointment_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        display_da = format_danish_date(appointment_time)
        display_en = format_english_date(appointment_time)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE bookings 
            SET status = 'cancelled' 
            WHERE calendar_event_id = %s AND status = 'booked'
        """, (slot_id,))
        final_purpose = purpose
        if moved_from:
            final_purpose = f"{purpose} (flyttet fra {moved_from})"
        cur.execute("""
            INSERT INTO bookings (patient_id, clinic_id, calendar_event_id, appointment_time, purpose, dentist_name, status)
            VALUES (%s, 1, %s, %s, %s, %s, 'booked')
        """, (patient_id, slot_id, appointment_time, final_purpose, dentist_name))
        conn.commit()
        cur.close()
        conn.close()
        
        patient_phone = get_patient_phone(patient_id)
        if patient_phone:
            first_name = patient_name.split()[0]
            if language == "en":
                dentist_text = f" with {dentist_name}" if dentist_name else ""
                purpose_text = f" — {purpose}" if purpose else ""
                sms_message = (
                    f"Dear {first_name}! "
                    f"Your appointment at the Dental Clinic is confirmed: {display_en}{dentist_text}{purpose_text}. "
                    f"Address: 3rd floor, Nørregade. "
                    f"Need to cancel or reschedule? Call us at 12345678. "
                    f"We look forward to seeing you."
                )
            else:
                dentist_text = f" hos {dentist_name}" if dentist_name else ""
                purpose_text = f" — {purpose}" if purpose else ""
                sms_message = (
                    f"Kære {first_name}! "
                    f"Din tid hos Tandlægeklinikken er bekræftet: {display_da}{dentist_text}{purpose_text}. "
                    f"Adresse: 3. sal, Nørregade. "
                    f"Skal du aflyse eller flytte? Ring til os på 12345678. "
                    f"Vi glæder os til at se dig."
                )
            send_sms(patient_phone, sms_message)
        
        return {
            "success": True,
            "message": f"Booket til {patient_name} — {display_da}"
        }
    except Exception as e:
        print(f"[ERROR] book_appointment: {e}")
        return {"success": False, "error": str(e)}

@mcp.tool()
def cancel_appointment(slot_id: str, language: str = "da") -> dict:
    """Aflys en booking. language='da' eller 'en' styrer SMS-sproget."""
    print(f"[TOOL] cancel_appointment: slot={slot_id}, language={language}")
    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=slot_id
        ).execute()
        original_time_da = ""
        original_time_en = ""
        if event.get("start", {}).get("dateTime"):
            dt = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
            original_time_da = format_danish_date(dt)
            original_time_en = format_english_date(dt)
        
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT p.phone, p.name, b.id as booking_id
            FROM bookings b
            JOIN patients p ON b.patient_id = p.id
            WHERE b.calendar_event_id = %s AND b.status = 'booked'
            ORDER BY b.created_at DESC
            LIMIT 1
        """, (slot_id,))
        patient_row = cur.fetchone()
        
        if not patient_row:
            print(f"[WARN] Ingen aktiv booking fundet for slot {slot_id}")
        
        event["summary"] = "Ledig tid"
        event["description"] = ""
        service.events().update(
            calendarId=CALENDAR_ID,
            eventId=slot_id,
            body=event
        ).execute()
        
        if patient_row:
            cur.execute("""
                UPDATE bookings SET status = 'cancelled'
                WHERE id = %s
            """, (patient_row["booking_id"],))
        else:
            cur.execute("""
                UPDATE bookings SET status = 'cancelled'
                WHERE calendar_event_id = %s AND status = 'booked'
            """, (slot_id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        if patient_row and patient_row["phone"]:
            first_name = patient_row["name"].split()[0] if patient_row["name"] else ""
            if language == "en":
                sms_message = (
                    f"Dear {first_name}! "
                    f"Your appointment {original_time_en} has been cancelled. "
                    f"Would you like to book a new one? Call us at 12345678. "
                    f"— The Dental Clinic"
                )
            else:
                sms_message = (
                    f"Kære {first_name}! "
                    f"Din tid {original_time_da} er aflyst. "
                    f"Vil du booke en ny tid? Ring til os på 12345678. "
                    f"— Tandlægeklinikken"
                )
            send_sms(patient_row["phone"], sms_message)
        else:
            print(f"[SMS] Kunne ikke finde aktiv patient for slot {slot_id} — ingen SMS sendt")
        
        return {
            "success": True,
            "cancelled_slot_id": slot_id,
            "original_time": original_time_da,
            "message": "Tid aflyst — sat tilbage til ledig"
        }
    except Exception as e:
        print(f"[ERROR] cancel_appointment: {e}")
        return {"success": False, "error": str(e)}

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
