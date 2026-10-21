"""
Дифференциальная проверка объединённых функций.

Маленькая модель склеивает «похожие» функции и не замечает различий (потерянный префикс в
тексте, другой порядок полей, пропавший часовой пояс), а тесты эти места часто не покрывают.
Поэтому harness сам сравнивает старую и новую реализацию на реальных аргументах:

1. тесты проекта прогоняются в одном процессе, вызовы функции перехватываются (до 5 наборов
   аргументов на функцию);
2. старая реализация восстанавливается из исходника до правки и исполняется в пространстве имён
   модуля, где функция теперь «живёт»;
3. те же аргументы подаются старой и новой версии, результаты сравниваются (для datetime —
   ещё и наличие часового пояса). Функции без обязательных аргументов вызываются напрямую.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .workspace import find_venv, venv_bin

_PROBE = r'''
import copy, datetime as _dt, importlib, inspect, json, sys, textwrap
sys.path.insert(0, sys.argv[2])
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
OUT = []


def short(v):
    r = repr(v)
    return r if len(r) <= 300 else r[:300] + "…"


def same(a, b):
    if isinstance(a, _dt.datetime) and isinstance(b, _dt.datetime):
        return ((a.tzinfo is None) == (b.tzinfo is None)
                and abs((a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds()) < 5)
    try:
        return type(a) is type(b) and bool(a == b)
    except Exception:
        return repr(a) == repr(b)


olds, news, calls = {}, {}, {}
for i, t in enumerate(cfg):
    short_name = t["name"].split(".")[-1]
    if not t["plain"]:
        OUT.append({"i": i, "skip": "метод класса — автоматически не сравнить"})
        continue
    try:
        ns = dict(vars(importlib.import_module(t["ns_module"])))
        for stmt in t.get("old_imports", []):
            try:
                exec(stmt, ns)
            except Exception:
                pass
        exec(textwrap.dedent(t["old_src"]), ns)
        olds[i] = ns[short_name]
        owner = importlib.import_module(t["new_module"])
        for part in t["new_name"].split(".")[:-1]:
            owner = getattr(owner, part)
        news[i] = getattr(owner, t["new_name"].split(".")[-1])
    except Exception as e:
        OUT.append({"i": i, "skip": f"не удалось подготовить сравнение: {type(e).__name__}: {e}"})
        continue
    if t["patch_module"]:
        try:
            owner = importlib.import_module(t["patch_module"])
            for part in t["name"].split(".")[:-1]:
                owner = getattr(owner, part)
            raw = inspect.getattr_static(owner, short_name)
            func = getattr(owner, short_name)
            calls[i] = []

            def wrap(*a, _f=func, _i=i, **k):
                if len(calls[_i]) < 5:
                    try:
                        calls[_i].append(copy.deepcopy((a, k)))
                    except Exception:
                        calls[_i].append((a, k))
                return _f(*a, **k)

            setattr(owner, short_name, staticmethod(wrap) if isinstance(raw, staticmethod) else wrap)
        except Exception:
            pass

if calls:
    import pytest
    pytest.main(["-q", "-p", "no:cacheprovider", "-o", "addopts=", sys.argv[2]])

for i, t in enumerate(cfg):
    if i not in olds:
        continue
    samples = calls.get(i) or []
    if not samples:
        sig = inspect.signature(olds[i])
        if all(p.default is not p.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               for p in sig.parameters.values()):
            samples = [((), {})]
    if not samples:
        OUT.append({"i": i, "untested": True})
        continue
    for a, k in samples:
        try:
            r_old = olds[i](*copy.deepcopy(a), **copy.deepcopy(k))
        except Exception as e:
            r_old = f"исключение {type(e).__name__}: {e}"
        try:
            r_new = news[i](*copy.deepcopy(a), **copy.deepcopy(k))
        except Exception as e:
            r_new = f"исключение {type(e).__name__}: {e}"
        if not same(r_old, r_new):
            extra = ""
            if isinstance(r_old, _dt.datetime) and isinstance(r_new, _dt.datetime):
                extra = f" (часовой пояс: было {r_old.tzinfo}, стало {r_new.tzinfo})"
            OUT.append({"i": i, "args": short(a)[:200] + (" " + short(k) if k else ""),
                        "old": short(r_old), "new": short(r_new) + extra})
            break
    else:
        OUT.append({"i": i, "ok": len(samples)})
print("@@LCW@@" + json.dumps(OUT, ensure_ascii=False))
'''


def check_merges(root: Path, merges: list[dict], timeout: int = 600) -> list[dict]:
    """
    По каждому объединению (из tools.find_merges): {"ok": n} — совпало на n наборах аргументов,
    {"args", "old", "new"} — первое расхождение, {"untested": True} — тесты функцию не вызывают,
    {"skip": причина} — сравнить не удалось.
    """
    if not merges:
        return []
    venv = find_venv(root)
    py = sys.executable
    if venv:
        b = venv_bin(venv)
        py = str(b / "python.exe") if (b / "python.exe").is_file() else str(b / "python")
    with tempfile.TemporaryDirectory() as d:
        cfg, probe = Path(d) / "cfg.json", Path(d) / "probe.py"
        cfg.write_text(json.dumps(merges, ensure_ascii=False), encoding="utf-8")
        probe.write_text(_PROBE, encoding="utf-8")
        try:
            p = subprocess.run([py, str(probe), str(cfg), str(root)], cwd=str(root), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8", "CI": "1"})
        except (OSError, subprocess.TimeoutExpired):
            return []
    line = next((l for l in p.stdout.splitlines()[::-1] if l.startswith("@@LCW@@")), "")
    try:
        res = json.loads(line[7:]) if line else []
    except ValueError:
        return []
    by = {r["i"]: r for r in res}
    return [by.get(i, {"skip": "проверка не выполнилась"}) for i in range(len(merges))]


def describe(merges: list[dict], results: list[dict]) -> tuple[list[str], list[str]]:
    """(расхождения и непроверенное — для модели, совпавшее — для информации)."""
    bad, good = [], []
    for m, r in zip(merges, results):
        head = m["text"].split("\n", 1)[0]
        if "ok" in r:
            good.append(f"{head} — поведение совпало на {r['ok']} наборах аргументов")
        elif "old" in r:
            bad.append(f"{head}\nРАСХОЖДЕНИЕ на аргументах {r['args']}:\n  было:  {r['old']}\n  стало: {r['new']}")
        elif r.get("untested"):
            bad.append(m["text"] + "\nТесты эту функцию не вызывают — сравнить автоматически нельзя. Напиши "
                                   "тест, который фиксирует СТАРОЕ поведение (точный ожидаемый результат по "
                                   "старому коду), и добейся, чтобы он проходил.")
        else:
            bad.append(m["text"] + f"\n(автосравнение не выполнено: {r.get('skip', '?')}) — сверь вручную.")
    return bad, good
