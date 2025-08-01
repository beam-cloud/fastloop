import asyncio
import os
import time
from typing import Any

import dotenv
from fastapi import WebSocket
from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    ServerMessageType,
    TranscriptionConfig,
)

from fastloop.integrations.plugins.base import BaseSpeechToTextManager

dotenv.load_dotenv()

TIME_BETWEEN_TRANSCRIPTIONS = 5


class SpeechmaticsSpeechToTextManager(BaseSpeechToTextManager):
    def __init__(self, request_id: str, websocket: WebSocket):
        self.request_id = request_id
        self.websocket = websocket
        self.client = AsyncClient(api_key=os.getenv("SPEECHMATICS_API_KEY"))
        self.session = None
        self.lock = asyncio.Lock()
        self.audio_buffer: list[bytes] = []
        self.last_transcription_time = time.time()

    async def start(self):
        await self.client.start_session(
            transcription_config=TranscriptionConfig(
                language="en",
                enable_partials=True,
                max_delay=1,
            ),
            audio_format=AudioFormat(
                encoding=AudioEncoding.PCM_S16LE,
                sample_rate=16000,
                chunk_size=1024,
            ),
        )

        def on_message(message: dict[str, Any]):
            results = message["results"]
            for result in results:
                print(result)

        def on_recognition_started(*_, **__):
            print("Recognition started")

        self.client.on(ServerMessageType.RECOGNITION_STARTED, on_recognition_started)
        self.client.on(ServerMessageType.ADD_TRANSCRIPT, on_message)

    async def send_audio(self, audio: bytes):
        # self.audio_buffer.append(audio)
        # if time.time() - self.last_transcription_time > TIME_BETWEEN_TRANSCRIPTIONS:
        await self.client.send_audio(audio)
        # self.audio_buffer = []
        # self.last_transcription_time = time.time()

    async def stop(self):
        await self.client.close()
