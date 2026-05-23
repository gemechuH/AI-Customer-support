from fastapi import FastAPI, Request
import uvicorn
import os
from dotenv import load_dotenv

# Load sensitive keys from the .env file
load_dotenv()

app = FastAPI(title="AI Voice Assistant API")

@app.get("/")
async def root():
    return {"message": "AI Voice Assistant API is running!"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

# --- Mock Calendar Database ---
AVAILABLE_TIMES = ["Monday 10:00 AM", "Tuesday 2:00 PM", "Wednesday 11:00 AM"]

@app.post("/api/webhook/voice")
async def voice_ai_webhook(request: Request):
    """
    The Voice AI platform will call this URL during the phone call 
    to trigger actions (like checking the calendar).
    """
    payload = await request.json()
    print("🔔 Incoming request:", payload)
    
    # Check if the AI wants to use a tool (requesting data)
    message = payload.get("message", {})
    
    if message.get("type") == "tool-calls":
        tool_calls = message.get("toolCalls", [])
        tool_results = []
        
        for tool in tool_calls:
            function_name = tool.get("function", {}).get("name")
            tool_call_id = tool.get("id")

            if function_name == "check_calendar":
                print("📅 AI is checking the calendar!")
                response_text = f"The available times are: {', '.join(AVAILABLE_TIMES)}"
                tool_results.append({"toolCallId": tool_call_id, "result": response_text})
            
            elif function_name == "book_appointment":
                print("✅ AI is booking an appointment!")
                tool_results.append({"toolCallId": tool_call_id, "result": "Appointment booked successfully! Tell the customer they are all set."})
        
        # Send the data back to the Voice AI so it can speak to the customer
        return {"results": tool_results}

    return {"status": "success", "message": "Received AI request"}

if __name__ == "__main__":
    # This block allows you to run the server directly from this file
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
