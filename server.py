import os
import json
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from google.oauth2 import service_account
from googleapiclient.discovery import build

mcp = FastMCP(
    "Dental MCP",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
SERVICE_ACCOUNT_FILE = "/etc/secrets/service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("googleapiclient", "calendar", "v3", credentials=creds)

@mcp.tool()
def find_patient(name: str, phone: str) -> dict:
    """Find eller opret en patient"""
    print(f"[TOOL] find_patient: name={name}, phone={phone}")
    return {
        "found": True,
        "patient": {
            "patient_id": f"p_{phone}",
            "name": name,
            "phone": phone
        }
    }

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
                    if preferred_day.lower() not in start.lower():
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
        return {"error": str(e)}

@mcp.tool()
def book_appointment(patient_id: str, patient_name: str, slot_id: str) -> dict:
    """Book en ledig tid ved at opdatere begivenheden i Google Calendar"""
    print(f"[TOOL] book_appointment: {patient_name}, slot={slot_id}")
    try:
        service = get_calendar_service()
        event = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=slot_id
        ).execute()
        event["summary"] = patient_name
        event["description"] = f"Patient ID: {patient_id}"
        updated_event = service.events().update(
            calendarId=CALENDAR_ID,
            eventId=slot_id,
            body=event
        ).execute()
        start = updated_event["start"].get("dateTime", "")
        display = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ).strftime("%A den %d/%m kl. %H:%M")
        return {
            "success": True,
            "message": f"Booket til {patient_name} — {display}"
        }
    except Exception as e:
        print(f"[ERROR] book_appointment: {e}")
        return {"success": False, "error": str(e)}

@mcp.tool()
def cancel_appointment(slot_id: str) -> dict:
    """Aflys en booking ved at sætte titlen tilbage til Ledig tid"""
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
        return {"success": True, "message": "Tid aflyst — sat tilbage til ledig"}
    except Exception as e:
        print(f"[ERROR] cancel_appointment: {e}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
