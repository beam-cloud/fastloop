import asyncio
import logging
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any

import dotenv
import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from scipy.io.wavfile import write

from ..integrations import Integration
from ..logging import setup_logger
from ..loop import LoopEvent
from ..types import IntegrationType
from .plugins.Deepgram import DeepgramSpeechToTextManager

dotenv.load_dotenv()

if TYPE_CHECKING:
    from ..fastloop import FastLoop

logger = setup_logger(__name__)
logger.setLevel(logging.DEBUG)


class StartConversationEvent(LoopEvent):
    type: str = "start_conversation"


class OnUserAudioDataEvent(LoopEvent):
    type: str = "on_user_audio_data"
    audio: bytes


class GenerateResponseEvent(LoopEvent):
    type: str = "generate_response"


class GenerationationInterruptedEvent(LoopEvent):
    type: str = "generationation_interrupted"


class AudioStreamResponseEvent(LoopEvent):
    type: str = "audio_stream_response"
    audio: bytes


class ConversationIntegration(Integration):
    def __init__(self):
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.executor = ProcessPoolExecutor()
        self.is_running: bool = False

    def type(self) -> IntegrationType:
        return IntegrationType.CONVERSATION

    def register(self, fastloop: "FastLoop", loop_name: str) -> None:
        fastloop.register_events(
            [
                StartConversationEvent,
                OnUserAudioDataEvent,
                GenerateResponseEvent,
                GenerationationInterruptedEvent,
                AudioStreamResponseEvent,
            ]
        )

        self._fastloop: FastLoop = fastloop
        self._fastloop.app.add_api_websocket_route(
            path=f"/{loop_name}/conversation/start",
            endpoint=self._handle_start_conversation,
        )
        self.loop_name: str = loop_name

    def save_audio_to_file(self, audio_chunks: list[bytes], filename: str = "recorded_audio.wav"):
        """Save recorded audio chunks to a WAV file."""
        if audio_chunks:
            try:
                combined_audio = np.frombuffer(b"".join(audio_chunks), dtype=np.int16)
                write(filename, 16000, combined_audio)
                print(f"Saved {len(audio_chunks)} audio chunks to {filename}")
                return True
            except Exception as e:
                print(f"Error saving audio file: {e}")
                return False
        return False

    async def _handle_websocket_event(self, websocket: WebSocket, request_id: str):
        audio_buffer: list[bytes] = []
        while True:
            try:
                data = await websocket.receive()
                if "bytes" in data:
                    audio_buffer.append(data["bytes"])
                    self.queue.put_nowait(
                        OnUserAudioDataEvent(
                            loop_id=request_id or None,
                            audio=data["bytes"],
                        )
                    )
                    continue

                match data.get("type"):
                    case "on_user_stop_speaking":
                        self.queue.put_nowait(
                            GenerateResponseEvent(
                                loop_id=request_id or None,
                            )
                        )
                    case _:
                        # logger.error(f"Unknown event: {data}")
                        continue
            except WebSocketDisconnect:
                logger.info("Client disconnected")
                self.is_running = False
                break
            except BaseException as e:
                logger.error(f"Error receiving data: {e}")
                self.is_running = False
                break

    async def _handle_start_conversation(self, websocket: WebSocket):
        await websocket.accept()
        self.is_running = True
        request_id = str(uuid.uuid4())

        # stt_manager = SpeechmaticsSpeechToTextManager(request_id, websocket)
        stt_manager = DeepgramSpeechToTextManager(request_id, websocket)
        await stt_manager.start()
        print("starting conversation")
        asyncio.create_task(self._handle_websocket_event(websocket, request_id))
        # llm_manager = LLMManager(self._fastloop, request_id)
        # stt_task = self.executor.submit(stt_manager.on_voice_stream, websocket)
        # tts_manager = TextToSpeechManager(self._fastloop, request_id)

        while self.is_running:
            try:
                loop_event: Any = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
                continue
            except BaseException as e:
                logger.error(f"Error processing event: {e}")
                self.is_running = False
                break

            if isinstance(loop_event, OnUserAudioDataEvent):
                await stt_manager.send_audio(loop_event.audio)
            elif isinstance(loop_event, GenerateResponseEvent):
                pass
                # llm_manager.generate_response(stt_manager.get_text())
            elif isinstance(loop_event, GenerationationInterruptedEvent):
                pass
            elif isinstance(loop_event, AudioStreamResponseEvent):
                pass
                # case "on_agent_audio_data":
                #   pass
                # case "on_agent_stop_speaking":
                #   pass
                # case "on_user_generation_interrupted":
                #   pass
                # case "on_generation_completed":
                #   pass
                # case "on_error":
                #   pass

            # # loop_state: LoopState = await loop_event_handler(mapped_request)
            # mapped_request: dict[str, Any] = loop_event.to_dict() if loop_event else {}

            # loop_event_handler = self._fastloop.loop_event_handlers.get(self.loop_name)
            # if not loop_event_handler:
            #   continue

            # loop: LoopState = await loop_event_handler(mapped_request)
            # if loop.loop_id:
            #     await self._fastloop.state_manager.set_loop_mapping(
            #         f"conversation:{loop.loop_id}", loop.loop_id
            #     )

            # if loop_state.loop_id:
            #   await self._fastloop.state_manager.set_loop_mapping(
            #       f"conversation:{loop_state.loop_id}", loop_state.loop_id
            #   )

        await stt_manager.stop()

    async def emit(self, event: LoopEvent):
        pass

    def events(self) -> list[Any]:
        return [StartConversationEvent]


class TextToSpeechManager:
    def __init__(self, fastloop: "FastLoop", request_id: str):
        self.fastloop = fastloop

    def synthesize(self, text: str):
        pass


class LLMManager:
    def __init__(self, fastloop: "FastLoop", request_id: str):
        self.fastloop = fastloop

    def generate_response(self, text: str):
        pass


# class ConversationManager:
#   def __init__(self, fastloop: "FastLoop"):
#     self.fastloop = fastloop

#   def start_conversation(self, loop_id: str):
#     pass

#   def generate_response(self, loop_id: str):
#     pass
