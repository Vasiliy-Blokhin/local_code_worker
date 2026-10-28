"""Документы: текст PDF/DOCX для read/grep, запись .docx из Markdown, проект без кода."""
import pytest

from lcw import documents
from lcw.agent import ASK_USER, SAVE_ITEM
from lcw.tools import ToolContext, ToolError, t_edit, t_grep, t_read, t_write


def _ctx(tmp_path):
    return ToolContext(root=tmp_path)


def test_fix_mojibake_cp1251_tex():
    broken = "Çàäà÷à 1. Ìíîæåñòâî ýëåìåíòîâ, ïðèíàäëåæàùèõ âñåì òð¼ì ìíîæåñòâàì ¾âõîäèò¿"
    assert documents.fix_mojibake(broken) == ("Задача 1. Множество элементов, принадлежащих всем трём "
                                              "множествам «входит»")


def test_fix_mojibake_keeps_normal_text():
    text = "Обычный текст, café, naïve — без изменений " * 3
    assert documents.fix_mojibake(text) == text


def test_docx_round_trip_through_tools(tmp_path):
    md = "# Задача 1\n**a)** A ∩ B ∩ C\nвторая строка\n- пункт *курсив*\n$$\nΣ C(n,k) = 2^n\n$$\n## Задача 2\n1. раз\n"
    out = t_write(_ctx(tmp_path), "res/result.docx", md)
    assert out.ok and "Word" in out.output and "A ∩ B ∩ C" in out.output
    assert (tmp_path / "res" / "result.docx").read_bytes()[:2] == b"PK"
    text = documents.docx_text(tmp_path / "res" / "result.docx")
    for part in ("Задача 1", "a) A ∩ B ∩ C", "вторая строка", "пункт курсив", "Σ C(n,k) = 2ⁿ", "раз"):
        assert part in text
    assert "**" not in text and "$$" not in text
    assert "res/result.docx:2: **a)** A ∩ B ∩ C" in t_grep(_ctx(tmp_path), "∩", glob="*.docx").output


def test_docx_built_in_parts_with_edit(tmp_path):
    ctx = _ctx(tmp_path)
    t_write(ctx, "result.docx", "# Решения\n## Задача 1\nответ 1")
    out = t_edit(ctx, "result.docx", "", "## Задача 2\nответ $2^{2n}$")
    assert out.ok and "2²ⁿ" in out.output
    t_edit(ctx, "result.docx", "ответ 1", "ответ $A \\cap B$")
    text = documents.docx_text(tmp_path / "result.docx")
    assert text.split("\n") == ["Решения", "Задача 1", "ответ A ∩ B", "Задача 2", "ответ 2²ⁿ"]
    assert "## Задача 2" in t_read(ctx, "result.docx").output  # read показывает Markdown — его правит edit
    with pytest.raises(ToolError):
        t_edit(ctx, "result.docx", "нет такого", "x")


def test_latex_to_unicode():
    f = documents.latex_to_unicode
    assert f(r"$$A\bigcap B\setminus C$$") == "A∩ B\\ C"
    assert f(r"\sum_{k=0}^{n} \binom{2n}{k} = 2^{2n}") == "Σₖ₌₀ⁿ C(2n, k) = 2²ⁿ"
    assert f(r"$x \in \mathbb{R}$, \{\emptyset, \Omega\}") == "x ∈ ℝ, {∅, Ω}"
    assert f("file_name и x_{n+1}") == "file_name и xₙ₊₁"
    assert f(r"(A \ (B ∪ C)), F₁\subsetF₂, {∅,\;A₁}") == "(A \\ (B ∪ C)), F₁⊂F₂, {∅, A₁}"
    assert f(r"\(\sum_{k=0}^{n} \bigl(C(n,k)\bigr)^{2}=C(2n,n)\)") == "Σₖ₌₀ⁿ (C(n,k) )²=C(2n,n)"


def test_problem_items_split_task(tmp_path):
    (tmp_path / "task.txt").write_text("Домашнее задание\nЗадача 1.\nНайдите A ∩ B.\n2\nЗадача 2.\nДокажите X.\n",
                                       encoding="utf-8")
    items = documents.problem_items(tmp_path, "решить задачи из task.txt")
    assert [i.split("\n")[1] for i in items] == ["Найдите A ∩ B.", "Докажите X."]
    assert items[0].startswith("Задача 1 из task.txt")
    assert documents.problem_items(tmp_path, "изучить task.txt") == []


def test_docx_table(tmp_path):
    documents.write_docx(tmp_path / "t.docx", "Итог:\n| № | Ответ |\n|---|---|\n| a | **A ∩ B** |\nпосле")
    assert documents.docx_text(tmp_path / "t.docx").split("\n") == ["Итог:", "№ | Ответ", "a | A ∩ B", "после"]


def test_docx_shrinking_write_rejected_once(tmp_path):
    ctx = _ctx(tmp_path)
    t_write(ctx, "r.docx", "## Задача 1\n" + "подробное решение " * 200)
    with pytest.raises(ToolError, match="сократил"):
        t_write(ctx, "r.docx", "## Задача 1\nкратко")
    assert t_write(ctx, "r.docx", "## Задача 1\nкратко").ok  # повтор того же write — осознанно


def test_doc_issues_duplicate_section(tmp_path):
    documents.write_docx(tmp_path / "r.docx", "## Задача 4\nа\n## Задача 5\nб\n## Задача 4\nв\\n\n")
    assert len(documents.doc_issues(tmp_path / "r.docx")) == 1
    assert "\\n" not in documents.docx_text(tmp_path / "r.docx")


def test_docx_section_replaced_not_duplicated(tmp_path):
    ctx = _ctx(tmp_path)
    t_write(ctx, "r.docx", "# Решения\n## Задача 1\n### a)\nстарое\n## Задача 2\n### a)\nдва")
    t_edit(ctx, "r.docx", "", "## Задача 1\n### a)\nновое")
    md = documents.docx_source(tmp_path / "r.docx")
    assert md.count("## Задача 1") == 1 and "новое" in md and "старое" not in md
    assert md.index("Задача 1") < md.index("Задача 2") and md.count("### a)") == 2
    t_edit(ctx, "r.docx", "", "## Задача 3\nтри")
    assert documents.docx_source(tmp_path / "r.docx").rstrip().endswith("три")
    t_edit(ctx, "r.docx", "  два", "ДВА")  # отступ в old не важен
    assert "ДВА" in documents.docx_source(tmp_path / "r.docx")
    assert "## Задача 1" in t_grep(ctx, "## Задача 1", glob="*.docx").output


def test_section_heading_with_nbsp_is_same_section():
    md = documents.upsert_section("## Задача 1\nа\n## Задача 3\nстарое\n", "## Задача 3\nновое")
    assert md.count("Задача") == 2 and "новое" in md and "старое" not in md


def test_output_file():
    assert documents.output_file(["Получить task.pdf", "ответ сохранить в result.docx"]) == "result.docx"
    assert documents.output_file(["решить задачи из task.pdf"]) == ""


def test_write_docx_paragraph_stops_before_block(tmp_path):
    documents.write_docx(tmp_path / "a.docx", "текст\n# Заголовок\n- пункт")
    assert documents.docx_text(tmp_path / "a.docx").split("\n") == ["текст", "Заголовок", "пункт"]


def test_read_pdf_uses_extracted_pages(tmp_path, monkeypatch):
    (tmp_path / "task.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(documents, "pdf_pages", lambda p: ["Çàäà÷à 1 " * 5, "Задача 2"])
    out = t_read(_ctx(tmp_path), "task.pdf").output
    assert "=== стр. 1 ===" in out and "Задача 1" in out and "=== стр. 2 ===" in out


def test_empty_docx_reads_as_empty(tmp_path):
    (tmp_path / "result.docx").write_bytes(b"")
    assert "пустой" in t_read(_ctx(tmp_path), "result.docx").output


def test_docs_project(tmp_path):
    (tmp_path / "task.pdf").write_bytes(b"%PDF")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    assert documents.docs_project(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert not documents.docs_project(tmp_path)


def test_docs_item_patterns():
    assert ASK_USER.search("Пожалуйста, скопируйте сюда текст вопросов")
    assert ASK_USER.search("я не могу распарсить содержимое PDF")
    assert SAVE_ITEM.search("ответ сохранить в result.docx")
    assert not SAVE_ITEM.search("Получить task.pdf и изучить его")
    assert not SAVE_ITEM.search("Используя данные из materials решить задачи из task.pdf")
