"""Запуск:
  python -m callqa --demo                         # демо на вымышленных звонках, без Битрикса и ИИ
  python -m callqa --from 2026-11-01 --to 2026-11-08 --write-back --telegram
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from .cache import Cache
from .evaluate import Checklist, KeywordEvaluator, LLMEvaluator
from .models import Call
from .pipeline import process, timeline_comment
from .report import build_excel, text_summary

log = logging.getLogger("callqa")
DEMO_FILE = Path(__file__).parent.parent / "demo" / "sample_calls.json"


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").astimezone()


def make_llm_client(prefix: str = "LLM"):
    from openai import OpenAI  # нужна только в режиме с ИИ

    key = os.getenv(f"{prefix}_API_KEY") or os.getenv("LLM_API_KEY")
    base = os.getenv(f"{prefix}_BASE_URL") or os.getenv("LLM_BASE_URL") or None
    return OpenAI(api_key=key, base_url=base, timeout=120, max_retries=2)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    p = argparse.ArgumentParser(description="Контроль качества звонков из Битрикс24")
    p.add_argument("--demo", action="store_true", help="Демо на вымышленных звонках из demo/sample_calls.json")
    p.add_argument("--from", dest="date_from", type=parse_date, default=today - timedelta(days=1),
                   help="Начало периода, ГГГГ-ММ-ДД (по умолчанию вчера)")
    p.add_argument("--to", dest="date_to", type=parse_date, default=today,
                   help="Конец периода (не включительно), ГГГГ-ММ-ДД (по умолчанию сегодня)")
    p.add_argument("--checklist", default="checklist.toml", help="Файл чек-листа")
    p.add_argument("--evaluator", choices=["auto", "llm", "keywords"], default="auto",
                   help="llm — нейросеть, keywords — по ключевым фразам, auto — llm, если задан LLM_API_KEY")
    p.add_argument("--manager", help="Только звонки этого менеджера (часть имени)")
    p.add_argument("--out", default=None, help="Файл отчёта .xlsx")
    p.add_argument("--write-back", action="store_true", help="Записать оценки в таймлайн карточек CRM")
    p.add_argument("--telegram", action="store_true", help="Отправить сводку и отчёт в Telegram")
    p.add_argument("--no-cache", action="store_true", help="Не использовать кэш расшифровок и оценок")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    checklist = Checklist.load(args.checklist)

    mode = args.evaluator
    if mode == "auto":
        mode = "llm" if os.getenv("LLM_API_KEY") and not args.demo else "keywords"
    if mode == "llm":
        evaluator = LLMEvaluator(checklist, make_llm_client(), os.environ["LLM_MODEL"])
    else:
        evaluator = KeywordEvaluator(checklist)
    log.info("Оценщик: %s", evaluator.name)

    bitrix, download, transcribe, portal = None, None, None, ""
    if args.demo:
        raw = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
        calls = [Call(**c) for c in raw]
        period = "демо-данные"
    else:
        from .bitrix import Bitrix
        from .transcribe import Transcriber

        webhook = os.environ.get("BITRIX_WEBHOOK")
        if not webhook:
            raise SystemExit("Не задан BITRIX_WEBHOOK — скопируйте .env.example в .env и заполните")
        bitrix = Bitrix(webhook)
        u = urlparse(webhook)
        portal = f"{u.scheme}://{u.netloc}"
        calls = bitrix.get_calls(args.date_from, args.date_to)
        period = f"{args.date_from:%d.%m.%Y} – {(args.date_to - timedelta(days=1)):%d.%m.%Y}"
        log.info("Звонков за период: %d", len(calls))
        if os.getenv("STT_MODEL"):
            transcriber = Transcriber(make_llm_client("STT"), os.environ["STT_MODEL"])
            download, transcribe = bitrix.download_record, transcriber.transcribe

    if args.manager:
        calls = [c for c in calls if args.manager.lower() in c.manager.lower()]

    cache = None if args.no_cache or args.demo else Cache("cache")
    results = process(calls, checklist, evaluator, download=download, transcribe=transcribe, cache=cache)

    out = args.out or f"reports/calls_{datetime.now():%Y-%m-%d_%H%M}.xlsx"
    path = build_excel(results, checklist, out, period, portal)
    summary = text_summary(results, checklist, period)
    print("\n" + summary + "\n")
    log.info("Отчёт сохранён: %s", path)

    if args.write_back and bitrix:
        written = 0
        for r in results:
            if r.evaluation and r.call.crm_entity_type and r.call.crm_entity_id:
                bitrix.add_timeline_comment(r.call.crm_entity_type, r.call.crm_entity_id, timeline_comment(r))
                written += 1
        log.info("Оценки записаны в CRM: %d", written)

    if args.telegram:
        from .notify import send_telegram

        send_telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], summary, path)
        log.info("Сводка отправлена в Telegram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
