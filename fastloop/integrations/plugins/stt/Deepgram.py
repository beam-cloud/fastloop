import asyncio
import os
from typing import Any

import dotenv
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)

from fastloop.integrations.plugins.base import BaseSpeechToTextManager
from fastloop.integrations.plugins.types import GenerateResponseEvent

dotenv.load_dotenv()


class DeepgramSpeechToTextManager(BaseSpeechToTextManager):
    def __init__(self, queue: asyncio.Queue[Any], request_id: str):
        self.queue = queue
        self.request_id = request_id
        self.buffer = b""
        self.buffer_lock = asyncio.Lock()
        self.is_running = False
        self.current_sentence = ""

    async def start(self):
        # self.dg = DeepgramClient(api_key=os.getenv("DEEPGRAM_API_KEY", ""))
        config = DeepgramClientOptions(options={"keepalive": "true"})
        deepgram: DeepgramClient = DeepgramClient(os.getenv("DEEPGRAM_API_KEY", ""), config)
        self.dg_connection = deepgram.listen.asyncwebsocket.v("1")

        async def on_message(*_, result: Any, **__):
            sentence = result.channel.alternatives[0].transcript
            print(f"Transcript: {sentence}")
            if result.speech_final:
                self.current_sentence += sentence
            #     # Send transcript back to client``
            #     response = {"type": "transcript", "text": sentence, "is_final": result.speech_final}
            #     await self.websocket.send_json(
            #         {"type": "transcript", "text": sentence, "is_final": result.speech_final}
            #     )

        async def on_error(error: Any, *_, **__):
            print(f"Deepgram error: {error}")
            # await self.websocket.send_json(error_response)

        async def on_close(error: Any, *d, **k):
            print(f"Deepgram connection closed: {error}")
            # await self.websocket.send_json(error_response)

        async def on_speech_started(result: Any, *_, **__):
            print(f"Speech started: {result}")

        async def on_utterance_end(result: Any, *_, **__):
            print(f"Utterance end: {result}")
            self.queue.put_nowait(
                GenerateResponseEvent(
                    loop_id=self.request_id or None,
                    text=self.current_sentence,
                )
            )
            self.current_sentence = ""

        self.dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)  # type: ignore
        self.dg_connection.on(LiveTranscriptionEvents.Error, on_error)  # type: ignore
        self.dg_connection.on(LiveTranscriptionEvents.Close, on_close)  # type: ignore
        self.dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)  # type: ignore
        self.dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)  # type: ignore

        print("starting deepgram connection")

        if not await self.dg_connection.start(
            LiveOptions(
                model="nova-3",
                smart_format=True,
                encoding="linear16",
                channels=1,
                sample_rate=16000,
                # endpointing=True,
                interim_results=True,
                utterance_end_ms="1000",
            )
        ):  # type: ignore
            print("Failed to start Deepgram connection")

        print("deepgram connection started")

        while not await self.dg_connection.is_connected():
            await asyncio.sleep(0.1)

        print("deepgram connection connected")
        self.is_running = True
        self.process_audio_task = asyncio.create_task(self.process_audio())

    async def process_audio(self):
        while self.is_running:
            async with self.buffer_lock:
                audio = self.buffer

            if len(audio) > 0:
                print("sending audio", len(audio) / 1024, "kb")
                await self.dg_connection.send(audio)
                self.buffer = b""
            await asyncio.sleep(0.5)

    async def send_audio(self, audio: bytes):
        try:
            if await self.dg_connection.is_connected():
                # await self.dg_connection.send(audio)
                async with self.buffer_lock:
                    self.buffer += audio
            else:
                return
        except Exception as e:
            print(f"Error sending audio: {e}")

    async def stop(self):
        print("stopping deepgram connection")
        self.is_running = False
        self.process_audio_task.cancel()
        await self.dg_connection.finish()  # type: ignore
