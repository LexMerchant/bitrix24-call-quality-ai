"""Отчёт для руководителя: Excel (сводка, менеджеры, звонки) и короткий текст для Telegram."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .evaluate import Checklist
from .pipeline import CallResult

GREEN = PatternFill("solid", fgColor="C6EFCE")
YELLOW = PatternFill("solid", fgColor="FFEB9C")
RED = PatternFill("solid", fgColor="FFC7CE")
HEADER = PatternFill("solid", fgColor="DDEBF7")


def score_fill(score: int | None) -> PatternFill | None:
    if score is None:
        return None
    return GREEN if score >= 80 else YELLOW if score >= 50 else RED


def mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class ManagerStats:
    name: str
    total: int = 0
    answered: int = 0
    missed: int = 0
    talk_seconds: int = 0
    scores: list[int] = field(default_factory=list)
    item_hits: dict[str, list[bool]] = field(default_factory=lambda: defaultdict(list))

    @property
    def avg_score(self) -> int | None:
        return round(sum(self.scores) / len(self.scores)) if self.scores else None

    def weakest_item(self, checklist: Checklist) -> str:
        rates = {i.title: sum(self.item_hits[i.id]) / len(self.item_hits[i.id])
                 for i in checklist.items if self.item_hits.get(i.id)}
        if not rates:
            return ""
        title, rate = min(rates.items(), key=lambda x: x[1])
        return f"{title} ({round(rate * 100)}%)" if rate < 1 else "—"


def manager_stats(results: list[CallResult]) -> list[ManagerStats]:
    stats: dict[str, ManagerStats] = {}
    for r in results:
        s = stats.setdefault(r.call.manager, ManagerStats(r.call.manager))
        s.total += 1
        if r.call.answered:
            s.answered += 1
            s.talk_seconds += r.call.duration
        else:
            s.missed += 1
        if r.evaluation:
            s.scores.append(r.evaluation.score)
            for item in r.evaluation.items:
                s.item_hits[item.id].append(item.passed)
    return sorted(stats.values(), key=lambda s: (s.avg_score is None, -(s.avg_score or 0)))


def item_rates(results: list[CallResult], checklist: Checklist) -> list[tuple[str, int]]:
    """Доля звонков, где пункт выполнен, по всему отделу."""
    out = []
    evaluated = [r.evaluation for r in results if r.evaluation]
    for item in checklist.items:
        hits = [i.passed for e in evaluated for i in e.items if i.id == item.id]
        if hits:
            out.append((item.title, round(100 * sum(hits) / len(hits))))
    return out


def crm_link(portal: str, r: CallResult) -> str:
    if not (portal and r.call.crm_entity_type and r.call.crm_entity_id):
        return ""
    return f"{portal.rstrip('/')}/crm/{r.call.crm_entity_type.lower()}/details/{r.call.crm_entity_id}/"


def build_excel(results: list[CallResult], checklist: Checklist, path: str | Path, period: str, portal: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    managers = manager_stats(results)
    evaluated = [r for r in results if r.evaluation]

    # ---- Сводка ----
    ws = wb.active
    ws.title = "Сводка"
    avg = round(sum(r.evaluation.score for r in evaluated) / len(evaluated)) if evaluated else None
    rows = [
        ("Компания", checklist.company),
        ("Период", period),
        ("Всего звонков", len(results)),
        ("Состоялось", sum(r.call.answered for r in results)),
        ("Пропущено", sum(not r.call.answered for r in results)),
        ("Оценено", len(evaluated)),
        ("Средний балл отдела", avg if avg is not None else "—"),
        ("Лучший менеджер", next((m.name for m in managers if m.avg_score is not None), "—")),
        ("Способ оценки", _evaluator_label(evaluated)),
    ]
    ws.append(["Показатель", "Значение"])
    for row in rows:
        ws.append(row)
    ws.append([])
    ws.append(["Пункт чек-листа", "Выполняется в % звонков"])
    header_row = ws.max_row
    for title, rate in sorted(item_rates(results, checklist), key=lambda x: x[1]):
        ws.append([title, rate])
        ws.cell(ws.max_row, 2).fill = score_fill(rate)
    _style_header(ws, 1)
    _style_header(ws, header_row)
    if avg is not None:
        ws.cell(8, 2).fill = score_fill(avg)
    _autowidth(ws)

    # ---- Менеджеры ----
    ws = wb.create_sheet("Менеджеры")
    ws.append(["Менеджер", "Звонков", "Состоялось", "Пропущено", "Время разговоров, мин",
               "Оценено", "Средний балл", "Слабое место"])
    for m in managers:
        ws.append([m.name, m.total, m.answered, m.missed, round(m.talk_seconds / 60, 1),
                   len(m.scores), m.avg_score if m.avg_score is not None else "—", m.weakest_item(checklist)])
        if m.avg_score is not None:
            ws.cell(ws.max_row, 7).fill = score_fill(m.avg_score)
    _style_header(ws, 1)
    ws.freeze_panes = "A2"
    _autowidth(ws)

    # ---- Звонки ----
    ws = wb.create_sheet("Звонки")
    head = ["Дата", "Менеджер", "Тип", "Клиент", "Длительность", "Балл"]
    head += [i.title for i in checklist.items]
    # «Итог звонка» пишет только нейросеть — в режиме по ключевым фразам колонку не показываем
    with_summary = any(r.evaluation and r.evaluation.summary for r in results)
    head += ["Настроение клиента"] + (["Итог звонка"] if with_summary else []) + ["Рекомендации", "Примечание", "Карточка CRM"]
    ws.append(head)
    for r in results:
        ev = r.evaluation
        marks = {i.id: ("✓" if i.passed else "✗") for i in ev.items} if ev else {}
        ws.append([
            r.call.start[:16].replace("T", " "), r.call.manager, r.call.direction, r.call.phone,
            mmss(r.call.duration), ev.score if ev else "",
            *[marks.get(i.id, "") for i in checklist.items],
            ev.client_mood if ev else "",
            *([ev.summary if ev else ""] if with_summary else []),
            ev.recommendations if ev else "",
            r.note, crm_link(portal, r),
        ])
        if ev:
            ws.cell(ws.max_row, 6).fill = score_fill(ev.score)
            for col, item in enumerate(checklist.items, start=7):
                ws.cell(ws.max_row, col).fill = GREEN if marks.get(item.id) == "✓" else RED
                ws.cell(ws.max_row, col).alignment = Alignment(horizontal="center")
    _style_header(ws, 1)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    _autowidth(ws, wrap_after=40)

    for sheet in wb.worksheets:  # печать: альбомная, по ширине страницы
        sheet.page_setup.orientation = "landscape"
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0

    wb.save(path)
    return path


def text_summary(results: list[CallResult], checklist: Checklist, period: str) -> str:
    evaluated = [r for r in results if r.evaluation]
    lines = [f"📞 Контроль звонков — {checklist.company}", f"Период: {period}", ""]
    lines.append(f"Звонков: {len(results)}, пропущено: {sum(not r.call.answered for r in results)}, "
                 f"оценено: {len(evaluated)}")
    if evaluated:
        avg = round(sum(r.evaluation.score for r in evaluated) / len(evaluated))
        lines.append(f"Средний балл отдела: {avg}/100")
        lines.append("")
        lines.append("Менеджеры:")
        for m in manager_stats(results):
            if m.avg_score is not None:
                weak = m.weakest_item(checklist)
                lines.append(f"• {m.name} — {m.avg_score}/100" + (f", слабое место: {weak}" if weak and weak != "—" else ""))
        worst = sorted(item_rates(results, checklist), key=lambda x: x[1])[:2]
        if worst:
            lines.append("")
            lines.append("Чаще всего проседает: " + "; ".join(f"{t} ({p}%)" for t, p in worst))
    return "\n".join(lines)


def _evaluator_label(evaluated: list[CallResult]) -> str:
    kinds = {r.evaluation.evaluator for r in evaluated}
    if kinds == {"llm"}:
        return "нейросеть по чек-листу"
    if kinds == {"keywords"}:
        return "по ключевым фразам (без нейросети)"
    return "смешанный" if kinds else "—"


def _style_header(ws, row: int) -> None:
    for cell in ws[row]:
        if cell.value is not None:
            cell.font = Font(bold=True)
            cell.fill = HEADER
            cell.alignment = Alignment(vertical="center", wrap_text=True)


def _autowidth(ws, wrap_after: int = 60) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(max(longest + 2, 8), wrap_after)
        if longest > wrap_after:
            for c in col[1:]:
                c.alignment = Alignment(wrap_text=True, vertical="top")
