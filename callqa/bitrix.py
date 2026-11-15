"""Клиент Битрикс24 REST API через входящий вебхук.

Вебхук создаётся в Битрикс24: Разработчикам → Другое → Входящий вебхук.
Нужны права: telephony (звонки), user (имена сотрудников), disk (записи),
crm (если оценки пишутся в карточку).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

from .models import CALL_TYPES, Call

log = logging.getLogger(__name__)


class BitrixError(RuntimeError):
    pass


class Bitrix:
    def __init__(self, webhook_url: str, session: requests.Session | None = None, pause: float = 0.5):
        self.base = webhook_url.rstrip("/") + "/"
        self.session = session or requests.Session()
        self.pause = pause  # у Битрикс24 лимит ~2 запроса в секунду

    # ---------- низкий уровень ----------

    def call(self, method: str, params: dict | None = None) -> dict:
        for attempt in range(1, 4):
            resp = self.session.post(self.base + method + ".json", json=params or {}, timeout=30)
            data = resp.json() if resp.content else {}
            if data.get("error") == "QUERY_LIMIT_EXCEEDED":
                log.warning("Битрикс просит притормозить, жду %d с", attempt * 2)
                time.sleep(attempt * 2)
                continue
            if "error" in data:
                raise BitrixError(f"{method}: {data.get('error')} — {data.get('error_description', '')}")
            if self.pause:
                time.sleep(self.pause)
            return data
        raise BitrixError(f"{method}: превышен лимит запросов")

    def list_all(self, method: str, params: dict) -> list[dict]:
        """Постраничная выгрузка: Битрикс отдаёт по 50 записей и номер следующей страницы в `next`."""
        items, start = [], 0
        while True:
            data = self.call(method, {**params, "start": start})
            items.extend(data.get("result", []))
            if "next" not in data:
                return items
            start = data["next"]

    # ---------- звонки ----------

    def get_calls(self, date_from: datetime, date_to: datetime) -> list[Call]:
        raw = self.list_all(
            "voximplant.statistic.get",
            {
                "FILTER": {
                    ">=CALL_START_DATE": date_from.isoformat(),
                    "<CALL_START_DATE": date_to.isoformat(),
                },
                "SORT": "CALL_START_DATE",
                "ORDER": "ASC",
            },
        )
        names = self.user_names({r.get("PORTAL_USER_ID") for r in raw if r.get("PORTAL_USER_ID")})
        return [self._to_call(r, names) for r in raw]

    @staticmethod
    def _to_call(r: dict, names: dict[str, str]) -> Call:
        uid = str(r.get("PORTAL_USER_ID") or "")
        return Call(
            id=str(r.get("CALL_ID") or r.get("ID")),
            start=str(r.get("CALL_START_DATE", "")),
            manager_id=uid,
            manager=names.get(uid, f"Сотрудник {uid}" if uid else "Не назначен"),
            phone=str(r.get("PHONE_NUMBER", "")),
            direction=CALL_TYPES.get(int(r.get("CALL_TYPE") or 0), "Неизвестно"),
            duration=int(r.get("CALL_DURATION") or 0),
            status_code=str(r.get("CALL_FAILED_CODE") or "200"),
            status_text=str(r.get("CALL_FAILED_REASON") or ""),
            crm_entity_type=str(r.get("CRM_ENTITY_TYPE") or ""),
            crm_entity_id=str(r.get("CRM_ENTITY_ID") or ""),
            record_file_id=str(r.get("RECORD_FILE_ID") or ""),
            record_url=str(r.get("CALL_RECORD_URL") or ""),
        )

    def user_names(self, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        users = self.list_all("user.get", {"FILTER": {"ID": sorted(ids)}})
        return {
            str(u["ID"]): " ".join(p for p in (u.get("NAME"), u.get("LAST_NAME")) if p) or f"Сотрудник {u['ID']}"
            for u in users
        }

    # ---------- записи ----------

    def download_record(self, call: Call) -> bytes:
        url = call.record_url
        if call.record_file_id:
            info = self.call("disk.file.get", {"id": call.record_file_id})["result"]
            url = info["DOWNLOAD_URL"]
        if not url:
            raise BitrixError(f"У звонка {call.id} нет записи")
        resp = self.session.get(url, timeout=120)
        resp.raise_for_status()
        return resp.content

    # ---------- запись результата в CRM ----------

    def add_timeline_comment(self, entity_type: str, entity_id: str, text: str) -> None:
        """Оставляет комментарий в таймлайне лида/сделки/контакта/компании."""
        self.call(
            "crm.timeline.comment.add",
            {"fields": {"ENTITY_TYPE": entity_type.lower(), "ENTITY_ID": int(entity_id), "COMMENT": text}},
        )
