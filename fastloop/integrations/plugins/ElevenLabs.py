import os
import dotenv
import asyncio
import time


from elevenlabs.client import AsyncElevenLabs
from fastapi import WebSocket
from .utils import bytes_to_wav

dotenv.load_dotenv()
TIME_BETWEEN_TRANSCRIPTIONS = 5

# Elevenlabs does not support realtime transcription so we 
# we hack together a solution by sending audio in chunks and 
# waiting for the transcription to be generated before sending the next chunk.


class ElevenLabsSpeechToTextManager:
  def __init__(self, request_id: str, websocket: WebSocket):
    self.request_id = request_id
    self.websocket = websocket
    self.client  = AsyncElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    self.transcripts: list[str | None] = []
    self.lock = asyncio.Lock()
    self.audio_buffer: list[bytes] = []
    self.last_transcription_time = time.time()


  async def start(self):
    pass
  
  async def _generate_transcript_async(self, index: int, audio: bytes):
    print(f"Generating transcript for {index}")
    transcript = await self.client.speech_to_text.convert(
      model_id="scribe_v1",
      file=bytes_to_wav(audio),
      language_code="eng",
      tag_audio_events=False,
    )
    self.transcripts[index] = transcript.text
    print(f"Transcript: {self.transcripts[index]}")
  
  async def send_audio(self, audio: bytes):
    self.audio_buffer.append(audio)
    if time.time() - self.last_transcription_time > TIME_BETWEEN_TRANSCRIPTIONS:
      index = len(self.transcripts)
      self.transcripts.append(None)

      asyncio.create_task(self._generate_transcript_async(index, b"".join(self.audio_buffer)))
      self.audio_buffer = []
      self.last_transcription_time = time.time()