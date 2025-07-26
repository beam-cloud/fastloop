import os
from typing import Any
from fastapi import WebSocket
from deepgram import DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions
import asyncio
import dotenv

dotenv.load_dotenv()

class DeepgramSpeechToTextManager:
  def __init__(self, request_id: str , websocket: WebSocket):
    self.request_id = request_id
    self.websocket = websocket

  async def start(self):
    # self.dg = DeepgramClient(api_key=os.getenv("DEEPGRAM_API_KEY", ""))
    config = DeepgramClientOptions(options={"keepalive": "true"})
    deepgram: DeepgramClient = DeepgramClient(os.getenv("DEEPGRAM_API_KEY", ""), config)
    self.dg_connection = deepgram.listen.asyncwebsocket.v("1")
    
    async def on_message(*_, result: Any, **__):
      sentence = result.channel.alternatives[0].transcript
      print(f"Transcript: {sentence}")
      if len(sentence) > 0:
          # Send transcript back to client``
          response = {
              "type": "transcript",
              "text": sentence,
              "is_final": result.speech_final
          }
          await self.websocket.send_json(response)
    
    async def on_error(error: Any, *_, **__):
      print(f"Deepgram error: {error}")
      error_response = {
        "type": "error",
            "message": str(error)
      }
      # await self.websocket.send_json(error_response)

    async def on_close(error: Any, *d, **k):
      print(f"Deepgram connection closed: {error}")
      # await self.websocket.send_json(error_response)
      
    async def on_speech_started(result: Any, *_, **__):
      print(f"Speech started: {result}")
      
    async def on_utterance_end(result: Any, *_, **__):
      print(f"Utterance end: {result}")
      
    self.dg_connection.on(LiveTranscriptionEvents.Transcript, on_message) # type: ignore
    self.dg_connection.on(LiveTranscriptionEvents.Error, on_error) # type: ignore
    self.dg_connection.on(LiveTranscriptionEvents.Close, on_close) # type: ignore
    self.dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started) # type: ignore
    self.dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end) # type: ignore
    
    print("starting deepgram connection")
    
    if not await self.dg_connection.start(
      LiveOptions(
        model="nova-3", 
        punctuate=True, 
        encoding="linear16", 
        channels=1, 
        sample_rate=16000,
        vad_events=True,
        endpointing=False,
        interim_results=False,
      )
      ): # type: ignore
      print("Failed to start Deepgram connection")

    print("deepgram connection started")
    
    while not await self.dg_connection.is_connected():
      await asyncio.sleep(0.1)
      
    print("deepgram connection connected")
    
  async def send_audio(self, audio: bytes):
    try:
      if await self.dg_connection.is_connected():
        await self.dg_connection.send(audio)
      else:
        return
    except Exception as e:
      print(f"Error sending audio: {e}")
