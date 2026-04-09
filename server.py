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

mcp = FastMCP(
    "Dental MCP",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

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
        CREATE TABLE IF NOT EXISTS patients (
            id SERIAL PRIMARY KEY,
            clinic_id INTEGER REFERENCES clinics(id),
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            created_by TEXT DEFAULT 'sofia',
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(clinic_id, phone)
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            patient_id INTEGER REFERENCES patients(id),
            clinic_id INTEGER REFERENCES clinics(id),
            calendar_event_id TEXT,
            appointment_time TIMESTAMP,
            purpose TEXT,
            status TEXT DEFAULT 'booked',
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    cur.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS purpose TEXT;")
    cur.execute("""
        ALTER TABLE patients
        ADD CONSTRAINT IF NOT EXISTS patients_clinic_phone_unique
        UNIQUE (clinic_id, phone);
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
def get_available_times(preferred_day: str = "") -> dict:
    """Hent ledige tider fra Google Calendar"""
    print(f"[TOOL] get_available_times: preferred_day={preferred_day}")
    try:
        service = get_calendar_service()
        now = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(days=7)).isoformat() + "Z"
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
            if event.get("summary", "").lower() == "ledig tid":
                start = event["start"].get("dateTime", "")
                slot_id = event["id"]
                if preferred_day:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    danish_days = {
                        "monday": "mandag", "tuesday": "tirsdag",
                        "wednesday": "onsdag", "thursday": "torsdag",
                        "friday": "fredag"
                    }
                    day_name = danish_days.get(dt.strftime("%A").lower(), "")
                    if preferred_day.lower() not in day_name:
                        continue
                available.append({
                    "slot_id": slot_id,
                    "start": start,
                    "display": datetime.fromisoformat(
                        start.replace("Z", "+00:00")
                    ).strftime("%A den %d/%m kl. %H:%M")
                })
        if not available:
            return {"available_times": [], "message": "Ingen ledige tider fundet"}
        return {"available_times": available}
    except Exception as e:
        print(f"[ERROR] get_available_times: {e}")
        return {"error": str(e), "message": "Teknisk fejl ved hentning af tider"}

@mcp.tool()
def book_appointment(patient_id: int, patient_name: str, slot_id: str, purpose: str = "", is_new_patient: bool = False) -> dict:
    """Book en ledig tid og gem i database med formål og patientstatus"""
    print(f"[TOOL] book_appointment: {patient_name}, slot={slot_id}, purpose={purpose}, new={is_new_patient}")
    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=slot_id
        ).execute()
        event["summary"] = patient_name
        if is_new_patient:
            description = f"NY PATIENT — første besøg\nFormål: {purpose if purpose else 'Ikke angivet'}"
        else:
            description = f"Kendt patient\nFormål: {purpose if purpose else 'Ikke angivet'}"
        event["description"] = description
        updated_event = service.events().update(
            calendarId=CALENDAR_ID,
            eventId=slot_id,
            body=event
        ).execute()
        start = updated_event["start"].get("dateTime", "")
        appointment_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        display = appointment_time.strftime("%A den %d/%m kl. %H:%M")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bookings (patient_id, clinic_id, calendar_event_id, appointment_time, purpose, status)
            VALUES (%s, 1, %s, %s, %s, 'booked')
        """, (patient_id, slot_id, appointment_time, purpose))
        conn.commit()
        cur.close()
        conn.close()
        return {
            "success": True,
            "message": f"Booket til {patient_name} — {display}"
        }
    except Exception as e:
        print(f"[ERROR] book_appointment: {e}")
        return {"success": False, "error": str(e)}

@mcp.tool()
def cancel_appointment(slot_id: str) -> dict:
    """Aflys en booking i kalender og opdater database"""
    print(f"[TOOL] cancel_appointment: slot={slot_id}")
    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=slot_id
        ).execute()
        event["summary"] = "Ledig tid"
        event["description"] = ""
        service.events().update(
            calendarId=CALENDAR_ID,
            eventId=slot_id,
            body=event
        ).execute()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            UPDATE bookings SET status = 'cancelled'
            WHERE calendar_event_id = %s
        """, (slot_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "message": "Tid aflyst — sat tilbage til ledig"}
    except Exception as e:
        print(f"[ERROR] cancel_appointment: {e}")
        return {"success": False, "error": str(e)}

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
