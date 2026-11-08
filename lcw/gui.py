"""
Окна GUI-приложений на Windows: найти окно процесса, снять скриншот, послать клавиши и клики
(через очередь сообщений окна, без перевода фокуса). Только ctypes, без pywin32.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    H = ctypes.c_void_p
    user32.GetDC.argtypes = [H]
    user32.GetDC.restype = H
    user32.ReleaseDC.argtypes = [H, H]
    user32.GetClientRect.argtypes = [H, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindowVisible.argtypes = [H]
    user32.GetWindowThreadProcessId.argtypes = [H, ctypes.POINTER(wintypes.DWORD)]
    user32.PrintWindow.argtypes = [H, H, wintypes.UINT]
    user32.PostMessageW.argtypes = [H, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.ClientToScreen.argtypes = [H, ctypes.POINTER(wintypes.POINT)]
    gdi32.CreateCompatibleDC.argtypes = [H]
    gdi32.CreateCompatibleDC.restype = H
    gdi32.CreateCompatibleBitmap.argtypes = [H, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = H
    gdi32.SelectObject.argtypes = [H, H]
    gdi32.SelectObject.restype = H
    gdi32.DeleteObject.argtypes = [H]
    gdi32.DeleteDC.argtypes = [H]
    gdi32.BitBlt.argtypes = [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int,
                             ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.UINT]
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = H
    kernel32.CloseHandle.argtypes = [H]
    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, H, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [_ENUM, ctypes.c_void_p]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def client_size(hwnd: int) -> tuple[int, int]:
    """Размер клиентской области окна."""
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def find_windows(pids: set[int]) -> list[int]:
    """Видимые окна верхнего уровня этих процессов (больше 100×100), крупнейшие первыми."""
    if sys.platform != "win32":
        return []
    found = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                w, h = client_size(hwnd)
                if w * h >= 100 * 100:
                    found.append((w * h, hwnd))
        return True

    user32.EnumWindows(_ENUM(cb), None)
    return [h for _, h in sorted(found, key=lambda x: -x[0])]


def child_pids(pid: int) -> set[int]:
    """pid и все его потомки (шелл → python → …)."""
    if sys.platform != "win32":
        return {pid}
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    parents: list[tuple[int, int]] = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            parents.append((e.th32ProcessID, e.th32ParentProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    out = {pid}
    grew = True
    while grew:
        grew = False
        for p, pp in parents:
            if pp in out and p not in out:
                out.add(p)
                grew = True
    return out


def screenshot(hwnd: int, path: str) -> tuple[int, int]:
    """Снимок клиентской области окна в PNG (PrintWindow, при неудаче — BitBlt с экрана)."""
    from PIL import Image
    w, h = client_size(hwnd)
    if w <= 0 or h <= 0:
        raise RuntimeError("окно свёрнуто или нулевого размера")
    hdc = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 1 | 2):  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
            gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.SelectObject(mem, old)
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    return w, h


def is_blank(path: str) -> bool:
    """Кадр одного цвета — ничего не нарисовано (или снимок не удался)."""
    from PIL import Image
    with Image.open(path) as img:
        colors = img.convert("RGB").getcolors(4)
    return colors is not None and len(colors) <= 1


VK = {**{chr(c): ord(chr(c).upper()) for c in range(ord("a"), ord("z") + 1)},
      **{str(d): 0x30 + d for d in range(10)},
      "space": 0x20, "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B, "tab": 0x09,
      "backspace": 0x08, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "shift": 0x10, "ctrl": 0x11,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
_EXTENDED = {0x25, 0x26, 0x27, 0x28}


def _lp(v: int) -> int:
    return v - (1 << 32) if v >= 1 << 31 else v


def send_key(hwnd: int, name: str, hold: float = 0.08) -> None:
    """Нажать и отпустить клавишу в окне (WM_KEYDOWN/WM_CHAR/WM_KEYUP)."""
    key = name.strip().lower()
    vk = VK.get(key)
    if vk is None:
        raise ValueError(f"неизвестная клавиша {name!r}")
    scan = user32.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16) | ((1 << 24) if vk in _EXTENDED else 0)
    user32.PostMessageW(hwnd, 0x100, vk, _lp(down))
    if len(key) == 1 or key == "space":
        user32.PostMessageW(hwnd, 0x102, ord(" " if key == "space" else key), _lp(down))
    elif vk == 0x0D:
        user32.PostMessageW(hwnd, 0x102, 0x0D, _lp(down))
    time.sleep(hold)
    user32.PostMessageW(hwnd, 0x101, vk, _lp(down | (1 << 30) | (1 << 31)))


def _deepest_child(hwnd: int, x: int, y: int) -> tuple[int, int, int]:
    """Самое глубокое дочернее окно под точкой и координаты в нём (tk/Qt рисуют в дочерних окнах)."""
    user32.ChildWindowFromPointEx.argtypes = [ctypes.c_void_p, wintypes.POINT, wintypes.UINT]
    user32.ChildWindowFromPointEx.restype = ctypes.c_void_p
    user32.MapWindowPoints.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
                                       wintypes.UINT]
    for _ in range(10):
        child = user32.ChildWindowFromPointEx(hwnd, wintypes.POINT(x, y), 1 | 2)  # пропуск невидимых/выкл.
        if not child or child == hwnd:
            break
        pt = wintypes.POINT(x, y)
        user32.MapWindowPoints(hwnd, child, ctypes.byref(pt), 1)
        hwnd, x, y = child, pt.x, pt.y
    return hwnd, x, y


def click(hwnd: int, x: int, y: int) -> None:
    """Левый клик в точке (x, y) клиентской области."""
    hwnd, x, y = _deepest_child(hwnd, x, y)
    lp = _lp(((y & 0xFFFF) << 16) | (x & 0xFFFF))
    user32.PostMessageW(hwnd, 0x200, 0, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x201, 1, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x202, 0, lp)
