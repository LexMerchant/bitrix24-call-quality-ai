"""Расшифровка записи звонка в текст через OpenAI-совместимый API распознавания речи."""

from __future__ import annotations


class Transcriber:
    def __init__(self, client, model: str, language: str = "ru"):
        """client — объект OpenAI-совместимого SDK; model — модель распознавания у вашего провайдера."""
        self.client = client
        self.model = model
        self.language = language

    def transcribe(self, audio: bytes, filename: str = "call.mp3") -> str:
        result = self.client.audio.transcriptions.create(
            model=self.model,
            file=(filename, audio),
            language=self.language,
        )
        return getattr(result, "text", "") or ""
