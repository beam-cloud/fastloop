import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

import dotenv
import numpy as np
from fastapi import WebSocket, WebSocketException
from scipy.io.wavfile import write

from ..baml_client import b
from ..baml_client.types import ConversationalAgentInput, ConversationHistory
from ..integrations import Integration, IntegrationType
from ..logging import setup_logger
from ..loop import LoopEvent
from .plugins.stt.Deepgram import DeepgramSpeechToTextManager
from .plugins.tts.DeepgramTTS import TextToSpeechManager
from .plugins.types import (
    AudioStreamResponseEvent,
    GenerateResponseEvent,
    GenerationationInterruptedEvent,
    OnUserAudioDataEvent,
    StartConversationEvent,
    StreamLLMResponseEvent,
)

dotenv.load_dotenv()

if TYPE_CHECKING:
    from ..fastloop import FastLoop

logger = setup_logger(__name__)
logger.setLevel(logging.DEBUG)


class LLMManager:
    def __init__(self, conversation_integration: "ConversationIntegration", request_id: str):
        self.conversation_integration = conversation_integration
        self.request_id = request_id
        self.prompt = "Only answer in one short coherent sentence."
        self.conversation_history: list[ConversationHistory] = []

    async def add_to_conversation_history(self, text: str, role: str):
        self.conversation_history.append(ConversationHistory(role=role, content=text))

    async def generate_response(self, text: str):
        stream = b.stream.GenerateResponse(
            input=ConversationalAgentInput(
                conversation_history=self.conversation_history,
                system_prompt=self.prompt,
                user_message=text,
            )
        )
        current_index = 0
        for chunk in stream:
            end = len(chunk)
            if current_index == end:
                continue
            self.conversation_integration.queue.put_nowait(
                StreamLLMResponseEvent(
                    loop_id=self.request_id or None,
                    text=chunk[current_index:].strip(),
                )
            )
            current_index = end

        final = stream.get_final_response()
        self.conversation_history.append(ConversationHistory(role="assistant", content=final))
        # self.conversation_integration.queue.put_nowait(
        #     StreamLLMResponseEvent(
        #         loop_id=self.request_id or None,
        #         text=final,
        #     )
        # )


class WebsocketManager:
    def __init__(
        self,
        conversation_integration: "ConversationIntegration",
        request_id: str,
        websocket: WebSocket,
    ):
        self.conversation_integration = conversation_integration
        self.request_id = request_id
        self.websocket = websocket

    async def start(self):
        audio_buffer: list[bytes] = []
        while True:
            try:
                data = await self.websocket.receive()
                if "bytes" in data:
                    audio_buffer.append(data["bytes"])
                    await self.conversation_integration.emit(
                        OnUserAudioDataEvent(
                            loop_id=self.request_id or None,
                            audio=data["bytes"],
                        )
                    )
                    continue

                # match data.get("type"):
                #     case "on_user_stop_speaking":
                #         await self.emit(
                #             GenerateResponseEvent(
                #                 loop_id=request_id or None,
                #             )
                #         )
                #     case _:
                #         # logger.error(f"Unknown event: {data}")
                #         continue
            except WebSocketException:
                logger.info("Client disconnected")
                self.is_running = False
                break
            except BaseException as e:
                logger.error(f"Error receiving data: {e}")
                self.is_running = False
                break

    async def stop(self):
        await self.websocket.close()

    async def send_audio(self, audio: bytes):
        await self.websocket.send_bytes(audio)


class ConversationIntegration(Integration):
    def __init__(self):
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
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

    # async def _handle_websocket_event(self, websocket: WebSocket, request_id: str):
    #     audio_buffer: list[bytes] = []
    #     while True:
    #         try:
    #             data = await websocket.receive()
    #             if "bytes" in data:
    #                 audio_buffer.append(data["bytes"])
    #                 await self.emit(
    #                     OnUserAudioDataEvent(
    #                         loop_id=request_id or None,
    #                         audio=data["bytes"],
    #                     )
    #                 )
    #                 continue

    #             # match data.get("type"):
    #             #     case "on_user_stop_speaking":
    #             #         await self.emit(
    #             #             GenerateResponseEvent(
    #             #                 loop_id=request_id or None,
    #             #             )
    #             #         )
    #             #     case _:
    #             #         # logger.error(f"Unknown event: {data}")
    #             #         continue
    #         except WebSocketDisconnect:
    #             logger.info("Client disconnected")
    #             self.is_running = False
    #             break
    #         except BaseException as e:
    #             logger.error(f"Error receiving data: {e}")
    #             self.is_running = False
    #             break

    async def _handle_start_conversation(self, websocket: WebSocket):
        await websocket.accept()
        self.is_running = True
        request_id = str(uuid.uuid4())
        current_generation_task: asyncio.Task[Any] | None = None

        # stt_manager = SpeechmaticsSpeechToTextManager(request_id, websocket)
        stt_manager = DeepgramSpeechToTextManager(self.queue, request_id)
        await stt_manager.start()
        websocket_manager = WebsocketManager(self, request_id, websocket)
        websocket_task = asyncio.create_task(websocket_manager.start())
        llm_manager = LLMManager(self, request_id)
        tts_manager = TextToSpeechManager(self.queue, request_id)
        await tts_manager.start()

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
                print("Generating response", loop_event.text)
                current_generation_task = asyncio.create_task(
                    llm_manager.generate_response(loop_event.text)
                )
            elif isinstance(loop_event, StreamLLMResponseEvent):
                print(loop_event.text)
                # Send text to TTS for synthesis
                await tts_manager.synthesize(loop_event.text)
            elif isinstance(loop_event, GenerationationInterruptedEvent):
                if current_generation_task:
                    current_generation_task.cancel()
                # await websocket_manager.send_json(loop_event.to_dict())
            elif isinstance(loop_event, AudioStreamResponseEvent):
                await websocket_manager.send_audio(loop_event.audio)
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
        await tts_manager.stop()
        if websocket_task:
            websocket_task.cancel()

    async def emit(self, event: LoopEvent):
        await self.queue.put(event)

    def events(self) -> list[Any]:
        return [StartConversationEvent]
