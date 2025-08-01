import asyncio
import os
from typing import Any

import dotenv
from deepgram import (
    DeepgramClient,
    SpeakWebSocketEvents,
)

from fastloop.integrations.plugins.types import AudioStreamResponseEvent

dotenv.load_dotenv()


class TextToSpeechManager:
    def __init__(self, queue: asyncio.Queue[Any], request_id: str):
        self.queue = queue
        self.request_id = request_id
        self.is_running = False
        self.dg_connection = None
        self.text_buffer = ""
        self.text_buffer_lock = asyncio.Lock()

    async def start(self):
        deepgram: DeepgramClient = DeepgramClient(
            os.getenv("DEEPGRAM_API_KEY", ""),
        )
        self.dg_connection = deepgram.speak.websocket.v("1")

        def on_binary_data(cls, data: Any, **kwargs):
            print("Received audio chunk")
            # Convert binary data to audio format playback devices understand
            # array = np.frombuffer(data, dtype=np.int16)
            # Play the audio immediately upon receiving each chunk
            # sd.play(array, 48000, blocking=True)
            self.queue.put_nowait(
                AudioStreamResponseEvent(
                    loop_id=self.request_id or None,
                    audio=data,
                )
            )

        def on_error(cls, *args, **kwargs):
            """Handle TTS errors"""
            print(f"Deepgram TTS error: {args} {kwargs}")

        def on_close(cls, error: Any, *d, **k):
            """Handle TTS connection close"""
            print(f"Deepgram TTS connection closed: {error}")

        # Register event handlers - using string events similar to STT
        self.dg_connection.on(SpeakWebSocketEvents.AudioData, on_binary_data)  # type: ignore
        self.dg_connection.on(SpeakWebSocketEvents.Error, on_error)  # type: ignore
        self.dg_connection.on(SpeakWebSocketEvents.Close, on_close)  # type: ignore
        print("Starting Deepgram TTS connection")

        if not self.dg_connection.start(
            {
                "model": "aura-2-thalia-en",
                "encoding": "linear16",
                "sample_rate": 48000,
            }
        ):  # type: ignore
            print("Failed to start Deepgram TTS connection")
            raise Exception("Failed to start Deepgram TTS connection")

        print("Deepgram TTS connection started")

        while not self.dg_connection.is_connected():
            await asyncio.sleep(0.1)

        print("Deepgram TTS connection connected")
        self.is_running = True
        self.process_text_task = asyncio.create_task(self.process_text())

    async def process_text(self):
        """Process text buffer and send to Deepgram TTS"""
        while self.is_running:
            async with self.text_buffer_lock:
                text = self.text_buffer
                self.text_buffer = ""

            if text.strip():
                try:
                    if self.dg_connection:
                        self.dg_connection.send_text(text)
                        self.dg_connection.flush()
                except Exception as e:
                    print(f"Error sending text to TTS: {e}")

            await asyncio.sleep(0.1)

    async def synthesize(self, text: str):
        """Add text to the buffer for TTS synthesis"""
        try:
            if self.dg_connection and self.dg_connection.is_connected():
                async with self.text_buffer_lock:
                    self.text_buffer += text + " "
            else:
                print("TTS connection not connected")
        except Exception as e:
            print(f"Error in synthesize: {e}")

    async def stop(self):
        """Stop the TTS connection"""
        print("Stopping Deepgram TTS connection")
        self.is_running = False
        if hasattr(self, "process_text_task"):
            self.process_text_task.cancel()
        if self.dg_connection:
            self.dg_connection.finish()  # type: ignore
