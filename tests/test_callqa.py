"""Тесты без сети: Битрикс, нейросеть и Telegram подменены заглушками."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from callqa import __main__ as cli
from callqa import bitrix as bitrix_mod
from callqa.bitrix import Bitrix, BitrixError
from callqa.cache import Cache
from callqa.evaluate import Checklist, KeywordEvaluator, LLMEvaluator, extract_json, split_speakers
from callqa.models import Call
from callqa.pipeline import process, timeline_comment
from callqa.report import build_excel, manager_stats, text_summary

ROOT = Path(__file__).parent.parent
CHECKLIST = ROOT / "checklist.toml"


@pytest.fixture
def checklist():
    return Checklist.load(CHECKLIST)


@pytest.fixture
def demo_calls():
    return [Call(**c) for c in json.loads((ROOT / "demo" / "sample_calls.json").read_text(encoding="utf-8"))]


# ---------------- Битрикс ----------------

class FakeSession:
    """Отвечает заранее заданными ответами и запоминает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json=None, timeout=None):
        self.requests.append((url, json))
        body = self.responses.pop(0)
        return SimpleNamespace(content=b"x", json=lambda: body)

    def get(self, url, timeout=None):
        self.requests.append((url, None))
        return SimpleNamespace(content=b"AUDIO", raise_for_status=lambda: None)


def test_get_calls_paginates_and_maps(monkeypatch):
    monkeypatch.setattr(bitrix_mod.time, "sleep", lambda s: None)
    session = FakeSession([
        {"result": [{"CALL_ID": "a1", "PORTAL_USER_ID": "7", "PHONE_NUMBER": "+79990000001", "CALL_TYPE": "2",
                     "CALL_DURATION": "120", "CALL_START_DATE": "2026-11-10T10:00:00+03:00",
                     "CALL_FAILED_CODE": "200", "CRM_ENTITY_TYPE": "LEAD", "CRM_ENTITY_ID": "15",
                     "RECORD_FILE_ID": "900"}], "next": 50, "total": 2},
        {"result": [{"CALL_ID": "a2", "PORTAL_USER_ID": "7", "CALL_TYPE": "1", "CALL_DURATION": "0",
                     "CALL_FAILED_CODE": "304", "CALL_FAILED_REASON": "Пропущенный"}], "total": 2},
        {"result": [{"ID": "7", "NAME": "Анна", "LAST_NAME": "Смирнова"}]},
    ])
    bx = Bitrix("https://example.bitrix24.ru/rest/1/token/", session=session, pause=0)
    from datetime import datetime
    calls = bx.get_calls(datetime(2026, 11, 10), datetime(2026, 11, 11))

    assert [c.id for c in calls] == ["a1", "a2"]
    assert calls[0].manager == "Анна Смирнова" and calls[0].direction == "Входящий"
    assert calls[0].answered and calls[0].record_file_id == "900"
    assert not calls[1].answered and calls[1].direction == "Исходящий"
    # вторая страница запрошена со start=50
    assert session.requests[1][1]["start"] == 50
    assert session.requests[0][0].endswith("/voximplant.statistic.get.json")


def test_bitrix_error_and_rate_limit(monkeypatch):
    monkeypatch.setattr(bitrix_mod.time, "sleep", lambda s: None)
    session = FakeSession([{"error": "QUERY_LIMIT_EXCEEDED"}, {"result": {"ok": True}},
                           {"error": "ACCESS_DENIED", "error_description": "нет прав"}])
    bx = Bitrix("https://x.bitrix24.ru/rest/1/t", session=session, pause=0)
    assert bx.call("profile")["result"] == {"ok": True}  # повтор после лимита
    with pytest.raises(BitrixError, match="нет прав"):
        bx.call("crm.lead.get")


def test_download_record_via_disk(monkeypatch):
    session = FakeSession([{"result": {"DOWNLOAD_URL": "https://x/file.mp3"}}])
    bx = Bitrix("https://x.bitrix24.ru/rest/1/t", session=session, pause=0)
    call = Call("c", "", "1", "A", "", "Входящий", 60, record_file_id="900")
    assert bx.download_record(call) == b"AUDIO"
    assert session.requests[-1][0] == "https://x/file.mp3"


def test_timeline_comment_payload():
    session = FakeSession([{"result": 1}])
    Bitrix("https://x.bitrix24.ru/rest/1/t", session=session, pause=0).add_timeline_comment("LEAD", "15", "текст")
    url, body = session.requests[0]
    assert url.endswith("crm.timeline.comment.add.json")
    assert body["fields"] == {"ENTITY_TYPE": "lead", "ENTITY_ID": 15, "COMMENT": "текст"}


# ---------------- Оценка ----------------

def test_split_speakers():
    m, c = split_speakers("Менеджер: привет\nКлиент: дорого\nещё строка клиента\nОператор: понимаю вас")
    assert "привет" in m and "понимаю вас" in m
    assert "дорого" in c and "ещё строка клиента" in c


def test_keyword_evaluator_only_counts_manager_speech(checklist):
    # клиент сказал «здравствуйте», менеджер — нет: приветствие не засчитано
    ev = KeywordEvaluator(checklist).evaluate("Клиент: Здравствуйте!\nМенеджер: Алло. Что хотели?")
    assert ev.score == 0
    assert not next(i for i in ev.items if i.id == "greeting").passed


def test_keyword_evaluator_full_script(checklist, demo_calls):
    ev = KeywordEvaluator(checklist).evaluate(demo_calls[0].transcript)
    assert ev.score == 100 and ev.client_mood == "позитивный"


def test_checklist_score_weights(checklist):
    assert checklist.score(set()) == 0
    assert checklist.score({i.id for i in checklist.items}) == 100
    assert checklist.score({"needs"}) == 25  # вес 25 из 100


@pytest.mark.parametrize("raw", [
    '{"items": []}',
    '```json\n{"items": []}\n```',
    'Вот оценка:\n{"items": []}\nГотово.',
])
def test_extract_json(raw):
    assert extract_json(raw) == {"items": []}


def fake_llm(answer: str):
    create = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_llm_evaluator_scores_by_weights_not_by_model(checklist):
    answer = json.dumps({
        "items": [{"id": "greeting", "passed": True, "comment": "ок"},
                  {"id": "needs", "passed": True},
                  {"id": "next_step", "passed": False, "comment": "не договорился"}],
        "summary": "Клиент спросил цену", "recommendations": ["Спросить имя", "Записать на пробный"],
        "client_mood": "Негативный", "score": 999,
    }, ensure_ascii=False)
    ev = LLMEvaluator(checklist, fake_llm(answer), "model").evaluate("текст")
    assert ev.score == 35                      # 10 + 25, «score: 999» от модели игнорируется
    assert len(ev.items) == len(checklist.items)  # пропущенные моделью пункты = не выполнены
    assert ev.recommendations == "Спросить имя; Записать на пробный"
    assert ev.client_mood == "негативный" and ev.evaluator == "llm"


# ---------------- Конвейер и кэш ----------------

def test_pipeline_notes_and_transcription(tmp_path, checklist):
    calls = [
        Call("missed", "2026-11-10T10:00", "1", "A", "", "Входящий", 0, status_code="304", status_text="Пропущенный"),
        Call("short", "2026-11-10T10:05", "1", "A", "", "Входящий", 5, record_file_id="1"),
        Call("norec", "2026-11-10T10:10", "1", "A", "", "Входящий", 60),
        Call("rec", "2026-11-10T10:20", "1", "A", "", "Входящий", 60, record_file_id="2"),
    ]
    transcribed = []

    def transcribe(audio):
        transcribed.append(audio)
        return "Менеджер: Добрый день, меня зовут Анна. Записываю вас на пробный урок."

    cache = Cache(tmp_path)
    res = process(calls, checklist, KeywordEvaluator(checklist),
                  download=lambda c: b"AUDIO-" + c.id.encode(), transcribe=transcribe, cache=cache)
    assert res[0].note.startswith("Пропущен")
    assert res[1].note.startswith("Короткий")
    assert res[2].note == "Нет записи разговора"
    assert res[3].evaluation.score == 30 and transcribed == [b"AUDIO-rec"]

    # повторный запуск берёт расшифровку и оценку из кэша — без скачивания
    calls[3].transcript = ""
    res2 = process(calls, checklist, KeywordEvaluator(checklist),
                   download=lambda c: pytest.fail("не должен скачивать"), transcribe=transcribe, cache=cache)
    assert res2[3].evaluation.score == 30 and len(transcribed) == 1


def test_pipeline_survives_errors(checklist):
    calls = [Call("bad", "", "1", "A", "", "Входящий", 60, record_file_id="1"),
             Call("good", "", "1", "A", "", "Входящий", 60, transcript="Менеджер: Здравствуйте")]

    def broken(_):
        raise RuntimeError("Битрикс недоступен")

    res = process(calls, checklist, KeywordEvaluator(checklist), download=broken, transcribe=lambda a: "")
    assert "Битрикс недоступен" in res[0].note
    assert res[1].evaluation is not None


def test_timeline_comment(checklist, demo_calls):
    res = process(demo_calls[:2], checklist, KeywordEvaluator(checklist))
    text = timeline_comment(res[1])
    assert text.startswith("Оценка звонка от 2026-11-10 11:20: 0/100") and "❌" in text


# ---------------- Отчёт ----------------

def test_report(tmp_path, checklist, demo_calls):
    results = process(demo_calls, checklist, KeywordEvaluator(checklist))
    stats = {m.name: m for m in manager_stats(results)}
    assert stats["Мария Ким"].missed == 1 and stats["Мария Ким"].total == 4
    assert stats["Игорь Волков"].avg_score < stats["Анна Смирнова"].avg_score

    path = build_excel(results, checklist, tmp_path / "r.xlsx", "демо", portal="https://x.bitrix24.ru")
    wb = load_workbook(path)
    assert wb.sheetnames == ["Сводка", "Менеджеры", "Звонки"]
    calls_ws = wb["Звонки"]
    assert calls_ws.max_row == len(demo_calls) + 1
    assert calls_ws.cell(2, calls_ws.max_column).value == "https://x.bitrix24.ru/crm/lead/details/101/"

    summary = text_summary(results, checklist, "демо")
    assert "Средний балл отдела" in summary and "Игорь Волков" in summary


def test_cli_demo(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    out = tmp_path / "demo.xlsx"
    assert cli.main(["--demo", "--out", str(out)]) == 0
    assert out.exists()


def test_report_shows_summary_column_only_with_llm(tmp_path, checklist, demo_calls):
    answer = json.dumps({"items": [{"id": "greeting", "passed": True}], "summary": "Клиент записан на пробный",
                         "recommendations": "—", "client_mood": "позитивный"}, ensure_ascii=False)
    llm_results = process(demo_calls[:1], checklist, LLMEvaluator(checklist, fake_llm(answer), "m"))
    kw_results = process(demo_calls[:1], checklist, KeywordEvaluator(checklist))
    head = lambda res: [c.value for c in load_workbook(build_excel(res, checklist, tmp_path / "x.xlsx", "p"))["Звонки"][1]]
    assert "Итог звонка" in head(llm_results)
    assert "Итог звонка" not in head(kw_results)
