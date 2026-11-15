"""Кэш расшифровок и оценок: повторный запуск не платит за уже обработанные звонки."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from .models import Evaluation, ItemResult


class Cache:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _path(self, call_id: str) -> Path:
        safe = re.sub(r"[^\w.-]", "_", call_id)
        return self.folder / f"{safe}.json"

    def _load(self, call_id: str) -> dict:
        path = self._path(call_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _save(self, call_id: str, data: dict) -> None:
        self._path(call_id).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def get_transcript(self, call_id: str) -> str:
        return self._load(call_id).get("transcript", "")

    def set_transcript(self, call_id: str, text: str) -> None:
        data = self._load(call_id)
        data["transcript"] = text
        self._save(call_id, data)

    # key = оценщик + отпечаток чек-листа: поменяли чек-лист — звонки переоценятся
    def get_evaluation(self, call_id: str, key: str) -> Evaluation | None:
        raw = self._load(call_id).get("evaluations", {}).get(key)
        if not raw:
            return None
        raw["items"] = [ItemResult(**i) for i in raw["items"]]
        return Evaluation(**raw)

    def set_evaluation(self, call_id: str, key: str, evaluation: Evaluation) -> None:
        data = self._load(call_id)
        data.setdefault("evaluations", {})[key] = asdict(evaluation)
        self._save(call_id, data)
