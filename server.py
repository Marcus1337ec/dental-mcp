import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "Dental MCP",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
)

patients = [
    {"patient_id": "p1", "name": "Anders Jensen", "phone": "12345678"}
]

slots = [
    {"slot_id": "s1", "date": "mandag", "time": "09:00", "booked": False},
    {"slot_id": "s2", "date": "mandag", "time": "11:30", "booked": False},
    {"slot_id": "s3", "date": "tirsdag", "time": "10:00", "booked": False},
    {"slot_id": "s4", "date": "tirsdag", "time": "14:00", "booked": False},
    {"slot_id": "s5", "date": "onsdag", "time": "09:30", "booked": False},
]

@mcp.tool()
def find_patient(name: str, phone: str) -> dict:
    """Find en patient baseret på navn og telefonnummer"""
    print(f"[TOOL] find_patient: name={name}, phone={phone}")
    name_clean = name.strip().lower()
    phone_clean = phone.strip().replace(" ", "")
    for p in patients:
        if p["name"].lower() == name_clean and p["phone"].replace(" ", "") == phone_clean:
            return {"found": True, "patient": p}
    # Opret ny patient automatisk
    new_id = f"p{len(patients)+1}"
    new_patient = {"patient_id": new_id, "name": name.strip(), "phone": phone_clean}
    patients.append(new_patient)
    return {"found": False, "new_patient_created": True, "patient": new_patient}

@mcp.tool()
def get_available_times(preferred_day: str = "") -> dict:
    """Returner ledige tider, evt. filtreret på dag"""
    print(f"[TOOL] get_available_times: preferred_day={preferred_day}")
    available = [s for s in slots if not s["booked"]]
    if preferred_day:
        day_clean = preferred_day.strip().lower()
        filtered = [s for s in available if day_clean in s["date"].lower()]
        if filtered:
            available = filtered
    if not available:
        return {"available_times": [], "message": "Ingen ledige tider"}
    return {"available_times": available}

@mcp.tool()
def book_appointment(patient_id: str, slot_id: str) -> dict:
    """Book en tid til en patient"""
    print(f"[TOOL] book_appointment: patient_id={patient_id}, slot_id={slot_id}")
    for s in slots:
        if s["slot_id"] == slot_id and not s["booked"]:
            s["booked"] = True
            return {
                "success": True,
                "message": f"Tid booket: {s['date']} kl. {s['time']}"
            }
    return {"success": False, "message": "Tid ikke tilgængelig"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
