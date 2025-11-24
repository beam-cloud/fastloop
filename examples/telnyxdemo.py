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
    "sms-test",
    integrations=[
        TelnyxIntegration(
            default_from="+17186912415",
        )
    ],
)
async def handle_sms(context: LoopContext):
    # Wait for an incoming message
    message: TelnyxRxMessageEvent = await context.wait_for(TelnyxRxMessageEvent)

    sender = message.from_number
    text = message.text

    # The number that received the message (our Telnyx number)
    receiving_number = message.to_numbers[0] if message.to_numbers else None

    print(f"Received message from {sender} to {receiving_number}: {text}")

    # Reply to the sender
    if sender:
        await context.emit(
            TelnyxTxMessageEvent(
                to=sender,
                # Explicitly reply from the number that received the message
                from_number=receiving_number,
                text=f"Thanks for your message: '{text}'. We received it!",
            )
        )


if __name__ == "__main__":
    # Run the server
    # If running locally on port 8000, your webhook URL for Telnyx configuration would be:
    # https://your-ngrok-tunnel.ngrok.io/sms_handler/telnyx/events
    app.run(port=8000)
