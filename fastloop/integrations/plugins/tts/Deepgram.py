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
        self.audio_chunks = []
        self.loop = None
        self.audio_output_queue = asyncio.Queue(maxsize=100)
        self.audio_output_task = None

    async def start(self):
        self.loop = asyncio.get_running_loop()
        
        deepgram: DeepgramClient = DeepgramClient(
            os.getenv("DEEPGRAM_API_KEY", ""),
        )
        self.dg_connection = deepgram.speak.websocket.v("1")

        def on_binary_data(cls, data: Any, **kwargs):            
            self.audio_output_queue.put_nowait(
                 AudioStreamResponseEvent(
                    loop_id=self.request_id or None,
                    audio=data,
                )
            )

        self.dg_connection.on(SpeakWebSocketEvents.AudioData, on_binary_data)

        if not self.dg_connection.start(
            {
                "model": "aura-2-thalia-en",
                "encoding": "linear16",
                "sample_rate": 48000,
            }
        ):
            raise Exception("Failed to start Deepgram TTS connection")

        print("Deepgram TTS connection started")

        while not self.dg_connection.is_connected():
            await asyncio.sleep(0.1)

        self.is_running = True
        self.audio_output_task = asyncio.create_task(self._process_audio_output())
    
    async def _process_audio_output(self):
        """Process audio output queue and send to main queue without blocking"""
        while self.is_running:
            try:
                event = await asyncio.wait_for(self.audio_output_queue.get(), timeout=0.1)
                try:
                    await self.queue.put(event)
                except Exception as e:
                    print(f"Error putting audio data into main queue: {e}")
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error in audio output processing: {e}")
                await asyncio.sleep(0.1)

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
                self.dg_connection.send_text(text)
                self.dg_connection.flush()
        except Exception as e:
            print(f"Error in synthesize: {e}")

    async def stop(self):
        """Stop the TTS connection"""
        self.is_running = False
        if self.audio_output_task:
            self.audio_output_task.cancel()
            try:
                await self.audio_output_task
            except asyncio.CancelledError:
                pass
        if self.dg_connection:
            self.dg_connection.finish()  # type: ignore
