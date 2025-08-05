import asyncio

from fastloop.baml_client import b
from fastloop.baml_client.types import ConversationalAgentInput, ConversationHistory
from fastloop.loop import LoopEvent
from fastloop.integrations.plugins.types import StreamLLMResponseEvent


class LLMManager:
    STOP_WORDS = [".", "?", "!"]
    
    def __init__(self, queue: asyncio.Queue[LoopEvent], request_id: str):
        self.queue = queue
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
        current_sentence = ""
        for chunk in stream:
            end = len(chunk)
            if current_index == end:
                continue
            
            current_sentence += chunk[current_index:].strip()
            if current_sentence.endswith(tuple(self.STOP_WORDS)):
                self.queue.put_nowait(
                    StreamLLMResponseEvent(
                        loop_id=self.request_id or None,
                        text=current_sentence,
                    )
                )
                current_sentence = ""
            current_index = end
            

        final = stream.get_final_response()
        self.conversation_history.append(ConversationHistory(role="assistant", content=final))
