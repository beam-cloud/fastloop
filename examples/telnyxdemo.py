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

    sender = message.from_number
    text = message.text
    
    print(f"Received message from {sender}: {text}")

    # Reply to the sender
    if sender:
        await context.emit(
            TelnyxTxMessageEvent(
                to=sender,
                # Use the messaging profile ID from the incoming message to ensure we reply using the same config
                messaging_profile_id=message.messaging_profile_id,
                text=f"Thanks for your message: '{text}'. We received it!",
            )
        )

if __name__ == "__main__":
    # Run the server
    # If running locally on port 8000, your webhook URL for Telnyx configuration would be:
    # https://your-ngrok-tunnel.ngrok.io/sms_handler/telnyx/events
    app.run(port=8000)
