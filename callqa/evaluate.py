"""Оценка звонка по чек-листу.

Два оценщика с одинаковым интерфейсом:
- LLMEvaluator — нейросеть читает расшифровку и отмечает пункты с комментариями;
- KeywordEvaluator — без нейросети, по ключевым фразам (демо, тесты, запасной режим).

Итоговый балл всегда считает код по весам пунктов, а не нейросеть:
так оценка воспроизводима и её нельзя «уговорить».
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import Evaluation, ItemResult

NEGATIVE = ("дорого", "не интересно", "неинтересно", "не звоните", "не надо", "жалоб", "ужас", "обман")
POSITIVE = ("спасибо", "отлично", "здорово", "супер", "замечательно", "хорошо, давайте", "давайте попробуем")


@dataclass
class ChecklistItem:
    id: str
    title: str
    description: str
    weight: int
    keywords: list[str] = field(default_factory=list)


@dataclass
class Checklist:
    company: str
    items: list[ChecklistItem]
    min_duration: int = 20

    @classmethod
    def load(cls, path: str | Path) -> "Checklist":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        items = [ChecklistItem(**{k: v for k, v in i.items() if k in ChecklistItem.__dataclass_fields__})
                 for i in data.get("item", [])]
        if not items:
            raise ValueError("В чек-листе нет пунктов [[item]]")
        return cls(company=data.get("company", ""), items=items, min_duration=int(data.get("min_duration", 20)))

    def score(self, passed_ids: set[str]) -> int:
        total = sum(i.weight for i in self.items) or 1
        got = sum(i.weight for i in self.items if i.id in passed_ids)
        return round(100 * got / total)


def split_speakers(transcript: str) -> tuple[str, str]:
    """Делит расшифровку на реплики менеджера и клиента (если они размечены «Менеджер:» / «Клиент:»)."""
    manager, client, current = [], [], None
    for line in transcript.splitlines():
        m = re.match(r"^\s*(менеджер|оператор|клиент)\s*:\s*(.*)", line, re.IGNORECASE)
        if m:
            current = manager if m.group(1).lower() in ("менеджер", "оператор") else client
            current.append(m.group(2))
        elif current is not None:
            current.append(line)
    if not manager and not client:  # разметки нет — считаем всё речью менеджера
        return transcript, ""
    return "\n".join(manager), "\n".join(client)


def guess_mood(client_text: str) -> str:
    text = client_text.lower()
    neg = sum(w in text for w in NEGATIVE)
    pos = sum(w in text for w in POSITIVE)
    if neg > pos:
        return "негативный"
    if pos > neg:
        return "позитивный"
    return "нейтральный"


class KeywordEvaluator:
    name = "keywords"

    def __init__(self, checklist: Checklist):
        self.checklist = checklist

    def evaluate(self, transcript: str) -> Evaluation:
        manager_text, client_text = split_speakers(transcript)
        low = manager_text.lower()
        items = []
        for item in self.checklist.items:
            hit = next((k for k in item.keywords if k.lower() in low), None)
            items.append(ItemResult(item.id, item.title, hit is not None, f"найдено: «{hit}»" if hit else ""))
        failed = [i.title for i in items if not i.passed]
        return Evaluation(
            score=self.checklist.score({i.id for i in items if i.passed}),
            items=items,
            summary="",  # без нейросети пересказать звонок нельзя
            recommendations=("Обратить внимание: " + "; ".join(failed)) if failed else "Скрипт соблюдён.",
            client_mood=guess_mood(client_text),
            evaluator=self.name,
        )


SYSTEM_PROMPT = """Ты — руководитель отдела продаж компании «{company}». Ты проверяешь качество звонков менеджеров.
Оцени расшифровку звонка по чек-листу. Расшифровка сделана автоматически и может содержать ошибки распознавания —
оценивай по смыслу. Если реплики не размечены, сам определи, где менеджер, а где клиент.

Чек-лист:
{items}

Ответь СТРОГО в формате JSON без пояснений вокруг:
{{
  "items": [{{"id": "<id пункта>", "passed": true/false, "comment": "коротко, почему"}}],
  "summary": "о чём был звонок и чем закончился, 1–2 предложения",
  "recommendations": "конкретно, что менеджеру сделать лучше, 1–3 пункта",
  "client_mood": "позитивный | нейтральный | негативный"
}}"""


def extract_json(text: str) -> dict:
    """Достаёт JSON из ответа модели, даже если она обернула его в ```json ... ``` или добавила текст."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("В ответе модели нет JSON") from None
        return json.loads(text[start:end + 1])


class LLMEvaluator:
    name = "llm"

    def __init__(self, checklist: Checklist, client, model: str):
        """client — объект OpenAI/AsyncOpenAI-совместимого SDK (синхронный)."""
        self.checklist = checklist
        self.client = client
        self.model = model
        items = "\n".join(f"- id={i.id}: {i.title}. {i.description}" for i in checklist.items)
        self.system = SYSTEM_PROMPT.format(company=checklist.company, items=items)

    def evaluate(self, transcript: str) -> Evaluation:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": self.system},
                {"role": "user", "content": f"Расшифровка звонка:\n\n{transcript[:20000]}"},
            ],
        )
        return self.from_response(resp.choices[0].message.content or "")

    def from_response(self, text: str) -> Evaluation:
        data = extract_json(text)
        by_id = {str(i.get("id")): i for i in data.get("items", []) if isinstance(i, dict)}
        items = []
        for item in self.checklist.items:
            got = by_id.get(item.id, {})
            items.append(ItemResult(item.id, item.title, bool(got.get("passed")), str(got.get("comment", ""))))
        mood = str(data.get("client_mood", "")).strip().lower()
        return Evaluation(
            score=self.checklist.score({i.id for i in items if i.passed}),
            items=items,
            summary=str(data.get("summary", "")),
            recommendations=_as_text(data.get("recommendations", "")),
            client_mood=mood if mood in ("позитивный", "нейтральный", "негативный") else "нейтральный",
            evaluator=self.name,
        )


def _as_text(value) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)
