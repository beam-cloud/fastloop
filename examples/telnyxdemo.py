import os
from fastloop import FastLoop, LoopContext
from fastloop.integrations.telnyx import (
    TelnyxIntegration,
    TelnyxRxMessageEvent,
    TelnyxTxMessageEvent,
)

app = FastLoop(name="telnyx-demo")

# This loop will handle incoming Telnyx messages
# The webhook URL will be: http://<HOST>:<PORT>/sms_handler/telnyx/events
@app.loop(
    "sms_handler",
    integrations=[
        TelnyxIntegration(
            api_key=os.getenv("TELNYX_API_KEY") or "YOUR_API_KEY",
            default_from=os.getenv("TELNYX_FROM_NUMBER") or "+15550000000",
        )
    ],
)
async def handle_sms(context: LoopContext):
    # Wait for an incoming message
    message: TelnyxRxMessageEvent = await context.wait_for(TelnyxRxMessageEvent)

    # Extract relevant info from the payload
    data = message.payload.get("data", {})
    inner_payload = data.get("payload", {})
    sender = inner_payload.get("from", {}).get("phone_number")
    text = inner_payload.get("text", "")
    
    # Extract the number receiving the message (our number) to reply from the same line
    to_list = inner_payload.get("to", [])
    our_number = to_list[0].get("phone_number") if to_list else None

    print(f"Received message from {sender} to {our_number}: {text}")

    # Reply to the sender
    if sender and our_number:
        await context.emit(
            TelnyxTxMessageEvent(
                to=sender,
                from_number=our_number,  # Reply from the number that received the message
                text=f"Thanks for your message: '{text}'. We received it!",
            )
        )

if __name__ == "__main__":
    # Run the server
    # If running locally on port 8000, your webhook URL for Telnyx configuration would be:
    # https://your-ngrok-tunnel.ngrok.io/sms_handler/telnyx/events
    app.run(port=8000)
