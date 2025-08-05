import json
import queue
import threading
import time
from typing import Optional, Callable

import numpy as np
import pyaudio
import webrtcvad
import websocket



class AudioInterface:
    def __init__(
        self,
        input_callback: Callable[[bytes], None],
        input_chunk_size: int = 1024,
        input_sample_rate: int = 16000,
        input_channels: int = 1,
        output_sample_rate: int = 22000,
        output_channels: int = 2,
        silence_threshold: float = 0.01,
        silence_timeout: float = 2.0,
        send_interval: float = 0.5,
    ):
        """
        Initialize the unified audio interface.

        Args:
            input_callback: Function to call when audio input is detected
            turn_manager: Optional turn manager for conversation flow control
            input_chunk_size: Audio chunk size in frames for input
            input_sample_rate: Sample rate in Hz for input
            input_channels: Number of audio channels for input (1 for mono, 2 for stereo)
            output_sample_rate: Sample rate in Hz for output
            output_channels: Number of audio channels for output
            silence_threshold: RMS threshold below which audio is considered silence
            silence_timeout: Stop transmission after this much continuous silence
            send_interval: Batch audio data for this duration before sending
        """
        # Input parameters
        self.input_callback = input_callback
        self.input_chunk_size = input_chunk_size
        self.input_sample_rate = input_sample_rate
        self.input_channels = input_channels
        
        # Output parameters
        self.output_sample_rate = output_sample_rate
        self.output_channels = output_channels
        
        self.format = pyaudio.paInt16  # 16-bit audio

        # Voice activity detection parameters
        self.silence_threshold = silence_threshold
        self.silence_timeout = silence_timeout
        self.send_interval = send_interval

        # Audio components
        self.audio = pyaudio.PyAudio()
        self.input_stream: Optional[pyaudio.Stream] = None
        self.output_stream: Optional[pyaudio.Stream] = None

        # Threading components
        self.input_queue: queue.Queue[bytes] = queue.Queue()
        self.output_queue: queue.Queue[bytes] = queue.Queue()
        self.is_running = False
        self.input_thread: Optional[threading.Thread] = None
        self.output_thread: Optional[threading.Thread] = None

        # Voice activity detection
        self.vad = webrtcvad.Vad()
        self.vad.set_mode(1)

        # State tracking
        self.last_speech_time = None
        self.audio_buffer: list[bytes] = []
        self.last_send_time = time.time()
        self.was_sending_audio = False  # Track if we were previously sending audio

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
        if self.last_speech_time is None:
            return False

        time_since_speech = time.time() - self.last_speech_time
        return time_since_speech < self.silence_timeout

    def _input_callback(self, in_data: bytes, *_, **__):
        if not self.is_running:
            return (None, pyaudio.paContinue)

        current_time = time.time()

        # Check for speech
        if self.is_speech(in_data, self.input_sample_rate):
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
            self.input_queue.put_nowait(combined_audio)

            # Reset buffer and timer
            self.audio_buffer = []
            self.last_send_time = current_time

        # Track audio state for potential future use, but don't switch turns aggressively
        current_should_send = self.should_send_audio()
        self.was_sending_audio = current_should_send

        return (None, pyaudio.paContinue)

    def _process_input_queue(self):
        while self.is_running:
            try:
                audio_data = self.input_queue.get(timeout=0.1)
                
                # Always send audio - let server decide when to process it
                print(f"Sending {len(audio_data)} bytes of audio")
                self.input_callback(audio_data)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error processing input audio: {e}")
                continue

    def _process_output_queue(self):
        while self.is_running:
            try:
                audio_data = self.output_queue.get(timeout=0.1)
                if self.output_stream:
                    self.output_stream.write(audio_data)
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error playing audio: {e}")
                continue

    def start(self, input_device_index: Optional[int] = None):
        try:
            # Start input stream
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.input_channels,
                rate=self.input_sample_rate,
                input=True,
                input_device_index=input_device_index,
                frames_per_buffer=self.input_chunk_size,
                stream_callback=self._input_callback,  # type: ignore
                start=True
            )

            # Start output stream
            self.output_stream = self.audio.open(
                format=self.format,
                channels=1,
                rate=48000,
                output=True,
                start=True
            )

            self.is_running = True

            # Start processing threads
            self.input_thread = threading.Thread(target=self._process_input_queue, daemon=True)
            self.output_thread = threading.Thread(target=self._process_output_queue, daemon=True)
            
            self.input_thread.start()
            self.output_thread.start()

        except Exception:
            raise

    def play(self, audio_data: bytes):
        if self.is_running:
            self.output_queue.put(audio_data)

    def stop(self):
        self.is_running = False

        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None

        if self.output_stream:
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None

        # Wait for threads to finish
        if self.input_thread:
            self.input_thread.join(timeout=1.0)
        if self.output_thread:
            self.output_thread.join(timeout=1.0)

    def cleanup(self):
        self.stop()
        self.audio.terminate()



class WebsocketManager:
    def __init__(
        self,
        websocket_url: str,
        audio_interface: Optional["AudioInterface"] = None,
    ):
        self.websocket_url = websocket_url
        self.websocket = None
        self.running = False
        self.audio_interface = audio_interface
        self.receive_thread: Optional[threading.Thread] = None

    def connect(self):
        if self.websocket:
            return

        try:
            self.websocket = websocket.create_connection(
                self.websocket_url,
                timeout=10,
                enable_multithread=True
            )
            self.running = True
            
            # Start receive thread
            self.receive_thread = threading.Thread(target=self._handle_websocket_receive, daemon=True)
            self.receive_thread.start()
        except Exception:
            raise

    def _handle_websocket_receive(self):
        if not self.websocket:
            return
            
        while self.running:
            try:
                # Set timeout for recv to allow periodic checking of running status
                message = self.websocket.recv()
                if not message:
                    continue
                
                if self.audio_interface and isinstance(message, bytes):
                    self.audio_interface.play(message)
                    
                elif isinstance(message, str):
                    data = json.loads(message)
                    print(f"Received message: {data}")
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                self.running = False
                break
            except ConnectionResetError:
                self.running = False
                break
            except Exception:
                if not self.running:
                    break
                # Continue on other errors unless shutting down
                continue

    def send_audio_data(self, audio_data: bytes):
        """Send audio data to websocket"""
        if self.websocket and self.running:
            try:
                self.websocket.send(audio_data, opcode=websocket.ABNF.OPCODE_BINARY)
            except websocket.WebSocketConnectionClosedException:
                print("Cannot send audio: WebSocket connection closed")
                self.running = False
            except Exception as e:
                print(f"Error sending audio data: {e}")

    def send_stop_speaking(self):
        """Send stop speaking message"""
        if self.websocket and self.running:
            try:
                message = json.dumps({"type": "on_user_stop_speaking"})
                self.websocket.send(message)
            except websocket.WebSocketConnectionClosedException:
                print("Cannot send message: WebSocket connection closed")
                self.running = False
            except Exception as e:
                print(f"Error sending stop speaking message: {e}")

    def disconnect(self):
        """Disconnect from websocket"""
        self.running = False
        
        if self.receive_thread:
            self.receive_thread.join(timeout=1.0)
            
        if self.websocket:
            try:
                self.websocket.close()
            except Exception:
                pass
            self.websocket = None
            
        print("Disconnected from websocket")


class ConversationManager:
    def __init__(self, websocket_url: str, input_sample_rate: int = 16000, output_sample_rate: int = 48000):
        self.websocket_url = websocket_url
        self.running = False
        
        # Create audio interface with callback to send audio data
        self.audio_interface = AudioInterface(
            input_callback=self._on_audio_input,
            input_sample_rate=input_sample_rate,
            output_sample_rate=output_sample_rate,
        )
        
        # Create websocket manager
        self.websocket_manager = WebsocketManager(
            websocket_url, self.audio_interface
        )

    def _on_audio_input(self, audio_data: bytes):
        """Callback function called when audio input is detected"""
        self.websocket_manager.send_audio_data(audio_data)

    def start_conversation(self):
        """Start the conversation (synchronous)"""
        try:
            # Connect to websocket
            self.websocket_manager.connect()
            
            # Start audio interface
            self.audio_interface.start()
            
            self.running = True
            print("Conversation started. Press Ctrl+C to stop")
            
            # Keep the main thread alive
            while self.running:
                time.sleep(0.1)
                
        except KeyboardInterrupt:
            print("Stopping conversation...")
        finally:
            self.stop_conversation()

    def stop_conversation(self):
        """Stop the conversation"""
        self.running = False
        self.audio_interface.stop()
        self.websocket_manager.disconnect()
        print("Conversation stopped")

    def cleanup(self):
        """Clean up resources"""
        self.stop_conversation()
        self.audio_interface.cleanup()


# Usage example
def main():
    # WebSocket server URL (replace with your server)
    websocket_url = "ws://localhost:8000/chat/conversation/start"

    # Create conversation manager
    conversation_manager = ConversationManager(websocket_url=websocket_url)

    print("Starting conversation... Press Ctrl+C to stop")
    try:
        conversation_manager.start_conversation()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conversation_manager.cleanup()


if __name__ == "__main__":
    main()
