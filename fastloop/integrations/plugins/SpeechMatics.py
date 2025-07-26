import asyncio
import time
from typing import Any
from speechmatics.rt import AsyncClient, TranscriptionConfig, ServerMessageType, AudioFormat, AudioEncoding
from fastapi import WebSocket
import os
import dotenv

dotenv.load_dotenv()

TIME_BETWEEN_TRANSCRIPTIONS = 5

class SpeechmaticsSpeechToTextManager:
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
    self.client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT, on_message)
  
  async def send_audio(self, audio: bytes):
    self.audio_buffer.append(audio)
    if time.time() - self.last_transcription_time > TIME_BETWEEN_TRANSCRIPTIONS:
      await self.client.send_audio(b"".join(self.audio_buffer))
      self.audio_buffer = []
      self.last_transcription_time = time.time()
  