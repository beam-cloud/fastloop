from fastloop.loop import LoopEvent


class GenerateResponseEvent(LoopEvent):
    type: str = "generate_response"
    text: str


class StartConversationEvent(LoopEvent):
    type: str = "start_conversation"


class OnUserAudioDataEvent(LoopEvent):
    type: str = "on_user_audio_data"
    audio: bytes


class StreamLLMResponseEvent(LoopEvent):
    type: str = "stream_llm_response"
    text: str


class GenerationationInterruptedEvent(LoopEvent):
    type: str = "generationation_interrupted"


class AudioStreamResponseEvent(LoopEvent):
    type: str = "audio_stream_response"
    audio: bytes
