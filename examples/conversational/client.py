import asyncio
import json
import queue
import time
import wave
from typing import Optional

import numpy as np
import pyaudio
import sounddevice as sd
import webrtcvad
import websockets
from scipy.io.wavfile import write


class SpeechMicrophoneStreamer:
    def __init__(
        self,
        websocket_manager: "WebsocketManager",
        chunk_size: int = 16 * 1024,
        sample_rate: int = 16000,
        channels: int = 1,
        # Simple optimization parameters
        silence_threshold: float = 0.01,  # RMS threshold for silence
        silence_timeout: float = 2.0,  # Stop sending after 2s of silence
        send_interval: float = 0.5,  # Send audio every 500ms instead of immediately
    ):
        """
        Initialize the microphone streamer.

        Args:
            websocket_url: WebSocket server URL
            chunk_size: Audio chunk size in frames
            sample_rate: Sample rate in Hz
            channels: Number of audio channels (1 for mono, 2 for stereo)
            silence_threshold: RMS threshold below which audio is considered silence
            silence_timeout: Stop transmission after this much continuous silence
            send_interval: Batch audio data for this duration before sending
        """
        self.chunk_size = chunk_size
        self.sample_rate = sample_rate
        self.channels = channels
        self.format = pyaudio.paInt16  # 16-bit audio

        # Simple optimization parameters
        self.silence_threshold = silence_threshold
        self.silence_timeout = silence_timeout
        self.send_interval = send_interval

        # Audio components
        self.audio = pyaudio.PyAudio()
        self.stream: Optional[pyaudio.Stream] = None

        # Threading components
        self.audio_queue: queue.Queue[bytes] = queue.Queue()
        self.is_recording = False
        self.websocket = None
        self.stored_audio: list[bytes] = []

        self.vad = webrtcvad.Vad()
        self.vad.set_mode(1)

        # Simple state tracking
        self.last_speech_time = None
        self.audio_buffer: list[bytes] = []
        self.last_send_time = time.time()

        self.processing_task = None
        # Websocket manager
        self.websocket_manager = websocket_manager

    def calculate_rms(self, audio_data: bytes) -> float:
        """Calculate RMS (Root Mean Square) of audio data."""
        try:
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            audio_float = audio_array.astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(audio_float**2))
            return rms
        except Exception:
            return 0.0

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        """Check if frame contains speech using both RMS and WebRTC VAD"""
        # First check RMS - if too quiet, definitely not speech
        rms = self.calculate_rms(frame)
        if rms < self.silence_threshold:
            return False

        # If loud enough, use WebRTC VAD for better detection
        try:
            # WebRTC VAD requires specific frame lengths
            frame_size = 480 * 2  # 30ms at 16kHz, 16-bit
            if len(frame) >= frame_size:
                chunk = frame[:frame_size]
                return self.vad.is_speech(chunk, sample_rate)
            else:
                # If frame too small, pad it
                padded_frame = frame + b"\x00" * (frame_size - len(frame))
                return self.vad.is_speech(padded_frame[:frame_size], sample_rate)
        except Exception:
            # Fallback to RMS-based detection
            return rms > self.silence_threshold * 2

    def should_send_audio(self) -> bool:
        """Simple logic: send if we've heard speech recently"""
        if self.last_speech_time is None:
            return False

        time_since_speech = time.time() - self.last_speech_time
        return time_since_speech < self.silence_timeout

    def list_audio_devices(self):
        """List available audio input devices."""
        print("Available audio devices:")
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:  # type: ignore
                print(f"  {i}: {info['name']} - {info['maxInputChannels']} channels")

    async def send_stop_speaking(self):
        await self.websocket_manager.send_stop_speaking()

    def audio_callback(self, in_data: bytes, *_, **__):
        if not self.is_recording:
            return (None, pyaudio.paContinue)

        current_time = time.time()

        # Check for speech
        if self.is_speech(in_data, self.sample_rate):
            self.last_speech_time = current_time

        # Always buffer audio if we've heard speech recently
        if self.should_send_audio():
            self.audio_buffer.append(in_data)

        # Send batched audio periodically
        if (
            current_time - self.last_send_time >= self.send_interval
            and self.audio_buffer
            and self.should_send_audio()
        ):
            # Combine buffered audio and send
            combined_audio = b"".join(self.audio_buffer)
            self.audio_queue.put(combined_audio)

            # Reset buffer and timer
            self.audio_buffer = []
            self.last_send_time = current_time

        return (None, pyaudio.paContinue)

    async def process_audio_buffer(self):
        while self.is_recording:
            try:
                # Get audio data with timeout
                audio_data: bytes = self.audio_queue.get(timeout=0.1)
                self.stored_audio.append(audio_data)

                # Send to WebSocket
                await self.websocket_manager.send_audio_data(audio_data)
                print(f"Sent {len(audio_data)} bytes of audio")
                await asyncio.sleep(1)

            except queue.Empty:
                # No audio data available, continue
                continue
            except websockets.exceptions.ConnectionClosed:
                print("WebSocket connection closed")
                break
            except Exception as e:
                print(f"Error sending audio data: {e}")

    def start_recording(self, device_index: Optional[int] = None):
        """Start recording audio from microphone."""
        try:
            self.stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk_size,
                stream_callback=self.audio_callback,
            )

            self.is_recording = True
            self.stream.start_stream()
            print(f"Started recording from microphone (device: {device_index or 'default'})")
            print(f"Silence threshold: {self.silence_threshold}, Timeout: {self.silence_timeout}s")

        except Exception as e:
            print(f"Error starting recording: {e}")
            raise

    def stop_recording(self):
        """Stop recording audio."""
        self.is_recording = False

        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None

        print("Stopped recording")

    def save_audio_to_file(self, filename: str = "recorded_audio.wav"):
        """Save recorded audio chunks to a WAV file."""
        if self.stored_audio:
            try:
                combined_audio = np.frombuffer(b"".join(self.stored_audio), dtype=np.int16)
                write(filename, self.sample_rate, combined_audio)
                print(f"Saved {len(self.stored_audio)} audio chunks to {filename}")
                return True
            except Exception as e:
                print(f"Error saving audio file: {e}")
                return False
        return False

    def start_streaming(self, device_index: Optional[int] = None, start_recording: bool = True):
        """Start streaming audio to WebSocket."""
        if start_recording:
            self.start_recording(device_index)
        self.processing_task = asyncio.create_task(self.process_audio_buffer())

    def cleanup(self):
        """Clean up resources."""
        self.stop_recording()
        self.audio.terminate()


class AudioPlayer:
    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate

    def play_audio(self, audio_data: bytes):
        try:
            array = np.frombuffer(audio_data, dtype=np.int16)
            print(f"Playing audio: {len(array)} samples")
            sd.play(array, self.sample_rate, blocking=True)
        except Exception as e:
            print(f"Error playing audio: {e}")


class WebsocketManager:
    def __init__(self, websocket_url: str, audio_player: Optional["AudioPlayer"] = None):
        self.websocket_url = websocket_url
        self.websocket = None
        self.websocket_receive_task = None
        self.running = False
        self.audio_player = audio_player

    async def connect(self):
        if self.websocket:
            return

        self.websocket = await websockets.connect(self.websocket_url)
        self.running = True

    async def handle_websocket_receive(self):
        if not self.websocket:
            return

        buffer_audio = b""

        while self.running:
            try:
                message = await self.websocket.recv()
                if isinstance(message, bytes) and self.audio_player:
                    buffer_audio += message
                    if len(buffer_audio) > 1024 * 16:
                        self.audio_player.play_audio(buffer_audio)
                        buffer_audio = b""
                    print(f"Playing {len(message)} bytes of audio")
                else:
                    print(message)
                await asyncio.sleep(0.1)
            except websockets.exceptions.ConnectionClosed:
                break

    async def send_audio_data(self, audio_data: bytes):
        if self.websocket:
            await self.websocket.send(audio_data)

    async def send_stop_speaking(self):
        if self.websocket:
            await self.websocket.send(json.dumps({"type": "on_user_stop_speaking"}))


class ConversationManager:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self.audio_player = AudioPlayer(48000)
        self.websocket_manager = WebsocketManager(websocket_url, self.audio_player)
        self.streamer = SpeechMicrophoneStreamer(
            websocket_manager=self.websocket_manager,
            chunk_size=1024,
        )

    async def start_conversation(self):
        await self.websocket_manager.connect()
        self.streamer.start_streaming()
        await self.websocket_manager.handle_websocket_receive()

    # This is used to test the server with an audio file
    async def pipe_in_audio_file(self, audio_file_path: str):
        await self.websocket_manager.connect()
        self.streamer.start_streaming(start_recording=False)
        self.streamer.is_recording = True

        with wave.open(audio_file_path, "rb") as wf:
            while True:
                data = wf.readframes(self.streamer.chunk_size * 16)
                if not data:
                    break
                self.streamer.audio_queue.put(data)
        await asyncio.sleep(30)


# Usage example
async def main():
    # WebSocket server URL (replace with your server)
    websocket_url = "ws://localhost:8000/chat/conversation/start"

    # Create streamer instance with simple optimizations
    streamer = ConversationManager(
        websocket_url=websocket_url,
    )

    print("Starting optimized audio streaming... Press Ctrl+C to stop")
    try:
        # await streamer.pipe_in_audio_file("./_sandbox/input.wav")
        await streamer.start_conversation()
    except BaseException as e:
        print(f"Stopping recording: {e}")
        streamer.streamer.stop_recording()
        streamer.streamer.save_audio_to_file()


if __name__ == "__main__":
    asyncio.run(main())
