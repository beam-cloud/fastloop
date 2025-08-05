import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

import dotenv
from fastapi import WebSocket, WebSocketException

from .plugins.llm.baml import LLMManager
from ..integrations import Integration, IntegrationType
from ..logging import setup_logger
from ..loop import LoopEvent
from .plugins.stt.Deepgram import DeepgramSpeechToTextManager
from .plugins.tts.Deepgram import TextToSpeechManager
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
        print("Starting websocket manager")
        while True:
            try:
                data = await self.websocket.receive()
                print(f"Received {len(data)} bytes of data")
                if "bytes" in data:
                    audio_buffer.append(data["bytes"])
                    print(f"Received {len(data['bytes'])} bytes of audio")
                    await self.conversation_integration.emit(
                        OnUserAudioDataEvent(
                            loop_id=self.request_id or None,
                            audio=data["bytes"],
                        )
                    )
                    continue

                # Handle text messages (JSON)
                if "text" in data:
                    try:
                        message = json.loads(data["text"]) if isinstance(data["text"], str) else data["text"]
                        print(f"Received message: {message}")
                        
                        message_type = message.get("type")
                        if message_type == "turn_change":
                            turn = message.get("turn")
                            print(f"Turn change received: {turn}")
                            # Handle turn change logic here if needed
                        elif message_type == "on_user_stop_speaking":
                            print("User stopped speaking - STT will handle response generation when utterance ends")
                        else:
                            print(f"Unknown message type: {message_type}")
                    except Exception as e:
                        print(f"Error processing text message: {e}")
                    continue
            except WebSocketException:
                logger.info("Client disconnected")
                self.conversation_integration.is_running = False
                break
            except BaseException as e:
                logger.error(f"Error receiving data: {e}")
                self.conversation_integration.is_running = False
                break

    async def stop(self):
        await self.websocket.close()
        

    async def send_audio(self, audio: bytes):
        await self.websocket.send_bytes(audio)


class ConversationIntegration(Integration):
    def __init__(self):
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        self.audio_input_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
        self.audio_output_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
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
        llm_manager = LLMManager(self.queue, request_id)
        tts_manager = TextToSpeechManager(self.queue, request_id)
        await tts_manager.start()

        # Create separate tasks for different event types to prevent blocking
        audio_input_task = asyncio.create_task(self._process_audio_input(stt_manager))
        audio_output_task = asyncio.create_task(self._process_audio_output(websocket_manager))
        main_event_task = asyncio.create_task(self._process_main_events(llm_manager, tts_manager, current_generation_task))

        try:
            await asyncio.gather(audio_input_task, audio_output_task, main_event_task, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error in event processing tasks: {e}")

        # Cancel remaining tasks
        for task in [audio_input_task, audio_output_task, main_event_task]:
            if not task.done():
                task.cancel()
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

        # await stt_manager.stop()
        await tts_manager.stop()
        websocket_task.cancel()
        await websocket_manager.stop()
        await stt_manager.stop()

    async def _process_audio_input(self, stt_manager):
        """Process audio input events without blocking other operations"""
        while self.is_running:
            try:
                event = await asyncio.wait_for(self.audio_input_queue.get(), timeout=0.1)
                if isinstance(event, OnUserAudioDataEvent):
                    await stt_manager.send_audio(event.audio)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error processing audio input: {e}")
                await asyncio.sleep(0.1)

    async def _process_audio_output(self, websocket_manager):
        """Process audio output events without blocking other operations"""
        while self.is_running:
            try:
                event = await asyncio.wait_for(self.audio_output_queue.get(), timeout=0.1)
                if isinstance(event, AudioStreamResponseEvent):
                    await websocket_manager.send_audio(event.audio)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error processing audio output: {e}")
                await asyncio.sleep(0.1)

    async def _process_main_events(self, llm_manager, tts_manager, current_generation_task):
        """Process main events (LLM generation, TTS synthesis)"""
        while self.is_running:
            try:
                loop_event = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                
                if isinstance(loop_event, OnUserAudioDataEvent):
                    try:
                        self.audio_input_queue.put_nowait(loop_event)
                    except asyncio.QueueFull:
                        logger.warning("Audio input queue full, dropping audio data")
                elif isinstance(loop_event, GenerateResponseEvent):
                    print("Generating response", loop_event.text)
                    current_generation_task = asyncio.create_task(
                        llm_manager.generate_response(loop_event.text)
                    )
                elif isinstance(loop_event, StreamLLMResponseEvent):
                    print(loop_event.text)
                    # Send text to TTS for synthesis (non-blocking)
                    asyncio.create_task(tts_manager.synthesize(loop_event.text))
                elif isinstance(loop_event, GenerationationInterruptedEvent):
                    if current_generation_task:
                        current_generation_task.cancel()
                elif isinstance(loop_event, AudioStreamResponseEvent):
                    try:
                        self.audio_output_queue.put_nowait(loop_event)
                    except asyncio.QueueFull:
                        logger.warning("Audio output queue full, dropping audio data")
                        
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error processing main event: {e}")
                await asyncio.sleep(0.1)

    async def emit(self, event: LoopEvent):
        try:
            await self.queue.put(event)
        except asyncio.QueueFull:
            logger.warning(f"Main queue full, dropping event: {type(event).__name__}")

    def events(self) -> list[Any]:
        return [StartConversationEvent]
