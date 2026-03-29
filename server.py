import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Demo MCP")

patients = [
    {"patient_id": "p1", "name": "Anders Jensen", "phone": "12345678"}
]

slots = [
    {"slot_id": "s1", "time": "09:00", "booked": False},
    {"slot_id": "s2", "time": "11:30", "booked": False},
]

@mcp.tool()
def find_patient(name: str, phone: str) -> dict:
    print(f"[TOOL] find_patient called with name={name}, phone={phone}")
    for p in patients:
        if p["name"] == name and p["phone"] == phone:
            return {
                "found": True,
                "patient": p,
                "message": "PATIENT_FOUND"
            }
    return {
        "found": False,
        "message": "PATIENT_NOT_FOUND"
    }

@mcp.tool()
def get_available_times() -> dict:
    print("[TOOL] get_available_times called")
    available = [s for s in slots if not s["booked"]]
    return {
        "available_times": available,
        "message": "AVAILABLE_TIMES_RETURNED"
    }

@mcp.tool()
def book_appointment(patient_id: str, slot_id: str) -> dict:
    print(f"[TOOL] book_appointment called with patient_id={patient_id}, slot_id={slot_id}")
    for s in slots:
        if s["slot_id"] == slot_id and not s["booked"]:
            s["booked"] = True
            return {
                "success": True,
                "message": f"BOOKED_{slot_id}"
            }
    return {
        "success": False,
        "message": "BOOKING_FAILED"
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(
        transport="sse",
        host="0.0.0.0",
        port=port
    )
