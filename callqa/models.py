"""Данные, с которыми работает система: звонок и его оценка."""

from __future__ import annotations

from dataclasses import dataclass, field

CALL_TYPES = {1: "Исходящий", 2: "Входящий", 3: "Входящий (переадресация)", 4: "Обратный звонок"}


@dataclass
class Call:
    id: str
    start: str                    # ISO-дата начала звонка
    manager_id: str
    manager: str                  # имя менеджера
    phone: str                    # номер клиента
    direction: str                # Входящий / Исходящий / ...
    duration: int                 # секунды
    status_code: str = "200"      # "200" — состоялся, иначе пропущен/сорван
    status_text: str = ""
    crm_entity_type: str = ""     # LEAD / DEAL / CONTACT / COMPANY
    crm_entity_id: str = ""
    record_file_id: str = ""      # ID файла записи на Диске Битрикс24
    record_url: str = ""          # прямая ссылка на запись, если есть
    transcript: str = ""

    @property
    def answered(self) -> bool:
        return self.status_code == "200" and self.duration > 0

    @property
    def has_record(self) -> bool:
        return bool(self.record_file_id or self.record_url or self.transcript)


@dataclass
class ItemResult:
    id: str
    title: str
    passed: bool
    comment: str = ""


@dataclass
class Evaluation:
    score: int                               # 0–100
    items: list[ItemResult] = field(default_factory=list)
    summary: str = ""                        # о чём звонок
    recommendations: str = ""                # что улучшить
    client_mood: str = ""                    # позитивный / нейтральный / негативный
    evaluator: str = ""                      # кто оценивал: llm / keywords
