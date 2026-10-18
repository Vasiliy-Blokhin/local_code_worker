import asyncio
from pathlib import Path

from lcw import entities
from lcw.tools import ToolContext, execute

SRC = '''"""Стратегии торговли."""
import os


@decorator
def train(history, as_of, *, l2=0.1):
    """Обучение модели на истории."""
    return 1


class Model(Base):
    """L2-логистическая регрессия."""

    def fit(self, x, y=None):
        """Подбор весов."""
        return self

    async def predict(self, x):
        return x
'''

JS = '''// Отрисовка ленты
export function render(items, opts) {
  return 1;
}

/** Клиент API */
class Api extends Base {
}

const load = async (id) => {
  return id;
};
'''


def run(ctx, name, **args):
    return asyncio.run(execute(ctx, name, args))


def make(tmp_path):
    (tmp_path / "strat.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.js").write_text(JS, encoding="utf-8")
    (tmp_path / "broken.py").write_text("def x(:\n", encoding="utf-8")
    return tmp_path


def test_python_entities(tmp_path):
    root = make(tmp_path)
    doc, ents = entities.file_entities(root, "strat.py")
    assert doc == "Стратегии торговли."
    by = {e.name: e for e in ents}
    assert by["train"].sig == "(history, as_of, *, l2=…)"
    assert entities._py_entities("def reg(cls):\n    pass\n")[1][0].sig == "(cls)"  # не метод — cls виден
    assert by["train"].line == 5 and by["train"].end == 8  # с декоратором
    assert by["train"].doc == "Обучение модели на истории."
    assert by["Model"].sig == "(Base)" and by["Model.fit"].kind == "method"
    assert by["Model.fit"].sig == "(x, y=…)"
    assert by["Model.predict"].kind == "async method"


def test_js_entities(tmp_path):
    root = make(tmp_path)
    _, ents = entities.file_entities(root, "web/app.js")
    by = {e.name: e for e in ents}
    assert by["render"].doc == "Отрисовка ленты" and by["render"].sig == "(items, opts)"
    assert by["Api"].sig == "(Base)" and by["Api"].doc == "Клиент API"
    assert "load" in by and by["render"].end == 4


def test_module_map_and_broken_file(tmp_path):
    m = entities.module_map(make(tmp_path))
    assert "strat.py — Стратегии торговли. [train, Model]" in m
    assert "broken.py — (синтаксическая ошибка" in m


def test_symbols_tool(tmp_path):
    ctx = ToolContext(root=Path(make(tmp_path)))
    out = run(ctx, "symbols", path="strat.py").output
    assert "14-16 method Model.fit(x, y=…) — Подбор весов." in out
    hits = run(ctx, "symbols", query="регрессия|render").output
    assert "strat.py:11-19 class Model(Base)" in hits and "web/app.js:2-4 def render" in hits
    assert "Нет совпадений" in run(ctx, "symbols", query="zzz").output
    assert not run(ctx, "symbols", path="../..").ok


def test_read_symbol(tmp_path):
    ctx = ToolContext(root=Path(make(tmp_path)))
    r = run(ctx, "read", path="strat.py", symbol="fit")
    assert r.output.startswith("14│    def fit") and "16│" in r.output and "17│" not in r.output
    assert run(ctx, "read", path="strat.py", symbol="Model.predict").output.startswith("18│")
    bad = run(ctx, "read", path="strat.py", symbol="nope")
    assert not bad.ok and "Model.fit" in bad.output


def test_cache_invalidated_on_change(tmp_path):
    root = make(tmp_path)
    entities.file_entities(root, "strat.py")
    p = root / "strat.py"
    p.write_text(SRC + "\n\ndef extra():\n    pass\n", encoding="utf-8")
    import os
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert any(e.name == "extra" for e in entities.file_entities(root, "strat.py")[1])


def test_big_read_returns_outline_first(tmp_path):
    body = "\n".join(f"def f{i}():\n    \"\"\"Функция {i}.\"\"\"\n    return {i}\n" for i in range(80))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    (tmp_path / "big.txt").write_text("x\n" * 400, encoding="utf-8")
    ctx = ToolContext(root=Path(tmp_path))
    first = run(ctx, "read", path="big.py")
    assert "оглавление" in first.output and "def f79() — Функция 79." in first.output
    assert "│" not in first.output
    again = run(ctx, "read", path="big.py")  # повтор — читаем как просили
    assert again.output.startswith("  1│def f0")
    assert run(ctx, "read", path="big.py", offset=5, limit=20).output.startswith(" 5│")
    assert run(ctx, "read", path="big.txt").output.startswith("  1│x")  # не код — без оглавления


def test_related_ranks_by_matched_stems(tmp_path):
    (tmp_path / "bt.py").write_text(
        '"""Бэктест стратегий."""\n\n\ndef metrics(trades):\n    """Метрики бэктеста: доходность и просадка."""\n\n\n'
        'def plot():\n    """Рисует график."""\n\n\ndef choose():\n    """Выбор лучшей стратегии по метрикам."""\n',
        encoding="utf-8")
    rel = entities.related(tmp_path, "оставить в работе только стратегии с лучшими метриками бэктеста")
    assert rel[0].startswith("bt.py") and "metrics" in rel[1] + rel[0]
    assert not any("plot" in r for r in rel)


def test_duplicates_across_modules(tmp_path):
    body = "def now():\n    import time\n    return time.time()\n"
    (tmp_path / "a.py").write_text(body, encoding="utf-8")
    (tmp_path / "b.py").write_text(body + "\n\ndef only_b():\n    return 1\n", encoding="utf-8")
    d = entities.duplicates(tmp_path)
    assert any(x.startswith("одноимённые now(): a.py:1, b.py:1") for x in d)
    assert any(x.startswith("одинаковое тело:") for x in d)
    assert not any("only_b" in x for x in d)
