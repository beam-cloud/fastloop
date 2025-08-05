import asyncio
import os
import queue
import threading
import wave

import pyaudio
import sounddevice as sd
from deepgram import (
    DeepgramClient,
    SpeakWebSocketEvents,
)
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# Configuration
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
TTS_TEXT = "Hello, this is a text to speech example using ElevenLabs. We're extending the text to make it longer. So that we can see if it is streaming or waiting for the whole text to be sent."
# WEBSOCKET_URL = "wss://api.elevenlabs.io/v1/text-to-speech/Xb7hH8MSUJpSbSDYk0k2/stream-input?model_id=eleven_flash_v2_5"
WEBSOCKET_URL = "wss://api.deepgram.com/v1/speak"
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")


class AudioPlayer:
    def __init__(self):
        self.audio_queue = queue.Queue()
        self.is_playing = False
        self.p = pyaudio.PyAudio()
        # Match the sample rate from ElevenLabs (16kHz)
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=48000,
            output=True,
        )

    def add_audio_chunk(self, chunk):
        self.audio_queue.put(chunk)

    def save_audio_to_file(self, filename: str):
        with wave.open(filename, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(48000)
            while not self.audio_queue.empty():
                chunk = self.audio_queue.get()
                wav_file.writeframes(chunk)

    async def play(self):
        self.is_playing = True
        while self.is_playing:
            try:
                chunk = self.audio_queue.get(block=True, timeout=0.1)
                self.stream.write(chunk)
            except queue.Empty:
                pass
            except Exception as e:
                print(f"Error playing audio: {e}")


async def main():
    audio_player = AudioPlayer()
    asyncio.create_task(audio_player.play())

    try:
        print("Connecting to ElevenLabs TTS WebSocket...")

        deepgram = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        dg_connection = deepgram.speak.websocket.v("1")

        def on_open(self, open, **kwargs):
            print(f"Connection opened: {open}")
        def on_binary_data(self, data, **kwargs):
            print("Received audio chunk")
            # Convert binary data to audio format playback devices understand
            audio_player.add_audio_chunk(data)
            # Play the audio immediately upon receiving each chunk
            # sd.play(array, 48000)
            # sd.wait()
        def on_close(self, close, **kwargs):
            print(f"Connection closed: {close}")

        dg_connection.on(SpeakWebSocketEvents.Open, on_open)
        dg_connection.on(SpeakWebSocketEvents.AudioData, on_binary_data)
        dg_connection.on(SpeakWebSocketEvents.Close, on_close)
        
        # Start the connection
        if dg_connection.start({
            "model": "aura-2-thalia-en",
            "encoding": "linear16",
            "sample_rate": 48000,
        }) is False:
            print("Failed to start connection")
            return

        dg_connection.send_text(TTS_TEXT)
        dg_connection.flush()
        # import time
        # time.sleep(5)
        await asyncio.sleep(5)
        
        dg_connection.finish()

    finally:
        audio_player.save_audio_to_file("debug_16000.wav")


def run_sync():
    """Synchronous wrapper for the async main function"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    run_sync()
