"""Отправка сводки и файла отчёта руководителю в Telegram."""

from __future__ import annotations

from pathlib import Path

import requests


def send_telegram(token: str, chat_id: str, text: str, file: str | Path | None = None) -> None:
    api = f"https://api.telegram.org/bot{token}"
    r = requests.post(f"{api}/sendMessage", json={"chat_id": chat_id, "text": text[:4000]}, timeout=30)
    r.raise_for_status()
    if file:
        with open(file, "rb") as f:
            r = requests.post(f"{api}/sendDocument", data={"chat_id": chat_id},
                              files={"document": (Path(file).name, f)}, timeout=60)
        r.raise_for_status()
