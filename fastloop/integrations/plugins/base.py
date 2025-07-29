from abc import ABC, abstractmethod

from fastapi import WebSocket


class BaseSpeechToTextManager(ABC):
    def __init__(self, request_id: str, websocket: WebSocket):
        raise NotImplementedError

    @abstractmethod
    async def start(self):
        raise NotImplementedError

    @abstractmethod
    async def stop(self):
        raise NotImplementedError

    @abstractmethod
    async def send_audio(self, audio: bytes):
        raise NotImplementedError
