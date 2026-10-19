"""Дифференциальная проверка объединённых функций на реальных аргументах из тестов."""
from pathlib import Path

from lcw import equivalence
from lcw.tools import find_merges


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def project(tmp_path):
    write(tmp_path / "pkg" / "__init__.py", "")
    write(tmp_path / "pkg" / "common.py",
          "def fmt(stats, prefix=''):\n    return f\"CAGR {stats['CAGR']:+.1%}\"\n\n\n"
          "def now():\n    import datetime\n    return datetime.datetime.now()\n")
    # обёртка: раньше был префикс «поиск», теперь он теряется
    write(tmp_path / "pkg" / "evolve.py",
          "from pkg.common import fmt\n\n\ndef describe(stats):\n    return fmt(stats, prefix='поиск')\n")
    write(tmp_path / "pkg" / "clock.py", "from pkg.common import now\n")
    write(tmp_path / "pkg" / "same.py", "from pkg.common import fmt as fmt2\n\n\ndef show(stats):\n    return fmt2(stats)\n")
    write(tmp_path / "tests" / "test_x.py",
          "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
          "from pkg.evolve import describe\nfrom pkg.same import show\n\n\n"
          "def test_describe():\n    assert 'CAGR' in describe({'CAGR': 0.12})\n\n\n"
          "def test_show():\n    assert show({'CAGR': 0.1})\n")
    return {
        "pkg/evolve.py": "def describe(stats):\n    return f\"поиск CAGR {stats['CAGR']:+.1%}\"\n",
        "pkg/clock.py": "import datetime\nfrom zoneinfo import ZoneInfo\n\n\n"
                        "def now():\n    return datetime.datetime.now(datetime.timezone.utc)\n",
        "pkg/same.py": "def show(stats):\n    x = stats['CAGR']\n    return f\"CAGR {x:+.1%}\"\n",
    }


def test_differences_found_on_real_arguments(tmp_path):
    originals = project(tmp_path)
    merges = find_merges(tmp_path, originals)
    assert [m["name"] for m in merges] == ["now", "describe", "show"]
    results = equivalence.check_merges(tmp_path, merges)
    by = {m["name"]: r for m, r in zip(merges, results)}
    assert "поиск CAGR +12.0%" in by["describe"]["old"] and by["describe"]["new"] == "'CAGR +12.0%'"
    assert "часовой пояс" in by["now"]["new"]          # функция без аргументов — вызвана напрямую
    assert by["show"] == {"i": 2, "ok": 1}             # тело другое, но результат тот же
    bad, good = equivalence.describe(merges, results)
    assert len(bad) == 2 and "РАСХОЖДЕНИЕ" in bad[0] and good[0].startswith("pkg/same.py")


def test_untested_function_asks_for_characterization_test(tmp_path):
    originals = project(tmp_path)
    (tmp_path / "tests" / "test_x.py").write_text("def test_nothing():\n    pass\n", encoding="utf-8")
    merges = [m for m in find_merges(tmp_path, originals) if m["name"] == "describe"]
    results = equivalence.check_merges(tmp_path, merges)
    assert results[0].get("untested")
    bad, _ = equivalence.describe(merges, results)
    assert "фиксирует СТАРОЕ поведение" in bad[0]
