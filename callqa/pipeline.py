"""Обработка звонков: запись → расшифровка → оценка, с кэшем и понятными причинами пропуска."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Callable

from .cache import Cache
from .evaluate import Checklist
from .models import Call, Evaluation

log = logging.getLogger(__name__)


@dataclass
class CallResult:
    call: Call
    evaluation: Evaluation | None = None
    note: str = ""          # почему звонок не оценён: пропущен, короткий, нет записи, ошибка


def checklist_fingerprint(checklist: Checklist) -> str:
    raw = json.dumps([asdict(i) for i in checklist.items], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def process(
    calls: list[Call],
    checklist: Checklist,
    evaluator,
    download: Callable[[Call], bytes] | None = None,
    transcribe: Callable[[bytes], str] | None = None,
    cache: Cache | None = None,
) -> list[CallResult]:
    key = f"{evaluator.name}:{checklist_fingerprint(checklist)}"
    results = []

    for n, call in enumerate(calls, start=1):
        res = CallResult(call)
        results.append(res)

        if not call.answered:
            res.note = "Пропущен" + (f": {call.status_text}" if call.status_text else "")
            continue
        if call.duration < checklist.min_duration:
            res.note = f"Короткий ({call.duration} с) — не оценивался"
            continue

        try:
            if not call.transcript and cache:
                call.transcript = cache.get_transcript(call.id)
            if not call.transcript:
                if not (call.record_file_id or call.record_url):
                    res.note = "Нет записи разговора"
                    continue
                if not (download and transcribe):
                    res.note = "Расшифровка не настроена"
                    continue
                log.info("[%d/%d] Расшифровка звонка %s", n, len(calls), call.id)
                call.transcript = transcribe(download(call))
                if cache:
                    cache.set_transcript(call.id, call.transcript)

            if cache and (cached := cache.get_evaluation(call.id, key)):
                res.evaluation = cached
                continue
            log.info("[%d/%d] Оценка звонка %s (%s)", n, len(calls), call.id, call.manager)
            res.evaluation = evaluator.evaluate(call.transcript)
            if cache:
                cache.set_evaluation(call.id, key, res.evaluation)
        except Exception as exc:  # один сбойный звонок не останавливает отчёт
            log.exception("Ошибка на звонке %s", call.id)
            res.note = f"Ошибка обработки: {exc}"

    return results


def timeline_comment(res: CallResult) -> str:
    """Текст оценки для таймлайна карточки в CRM."""
    ev = res.evaluation
    lines = [f"Оценка звонка от {res.call.start[:16].replace('T', ' ')}: {ev.score}/100"]
    lines += [f"{'✅' if i.passed else '❌'} {i.title}" + (f" — {i.comment}" if i.comment and not i.passed else "")
              for i in ev.items]
    if ev.summary:
        lines.append(f"Итог: {ev.summary}")
    if ev.recommendations:
        lines.append(f"Рекомендации: {ev.recommendations}")
    return "\n".join(lines)
