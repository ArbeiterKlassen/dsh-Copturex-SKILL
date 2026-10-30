# -*- coding: utf-8 -*-
"""抓取 Windows 进程可见窗口的真实渲染画面，直接导出 PNG（无需 Pillow）。

这是 DSH 适配版：默认输出 PNG，直接给 read_image 吃；支持按 DSH 主窗口一键抓取、
按宽度缩放（省读图 token）、PrintWindow 失败时自动回退屏幕抓取。

用法：
  python dsh_capture.py --list
  python dsh_capture.py --dsh                       # 抓 DeepSeek Harness 主窗口
  python dsh_capture.py --process SiNiSistar --out shot.png
  python dsh_capture.py --pid 1234 --max-width 1400 --out shot.png
  python dsh_capture.py --hwnd 0xb70ebe --format bmp --out shot.bmp

原理：先声明 per-monitor v2 DPI 感知，枚举目标进程的可见顶层窗口，用
PrintWindow(PW_RENDERFULLCONTENT) 取窗口交换链里实际渲染的内容，读成 32 位
BGRA，转 RGB 后用 zlib 手写 PNG（filter 0，color type 2）落盘。不依赖任何第三方包。

打印非黑像素比例 + 平均色，用于判断是否真黑屏；全黑时默认自动用屏幕抓取重试一次。

只依赖 Python 标准库，仅支持 Windows。
"""

import argparse
import ctypes
import datetime
import json
import os
import struct
import sys
import zlib
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

PW_RENDERFULLCONTENT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SRCCOPY = 0x00CC0020

# 64 位下句柄是 8 字节，凡是收句柄的函数都要声明 argtypes，
# 否则 ctypes 按 c_int 截断，清理资源时会报 OverflowError。
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = (wintypes.HWND,)
user32.GetWindowDC.restype = wintypes.HDC
user32.GetDC.argtypes = (wintypes.HWND,)
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, ctypes.c_uint)
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = (wintypes.HDC,)
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = (
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
)
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = (
    wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def use_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def set_dpi_awareness():
    """按物理像素抓图，否则高 DPI 屏上会只抓到缩放后的一部分。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return "per-monitor v2"
    except Exception:
        try:
            user32.SetProcessDPIAware()
            return "system aware"
        except Exception:
            return "none"


def process_image_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_rect(hwnd):
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0, 0, 0
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def enum_visible_windows(pid=None):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pid is not None and owner.value != pid:
            return True
        left, top, width, height = window_rect(hwnd)
        found.append({
            "hwnd": int(hwnd) if hwnd else 0,
            "pid": owner.value,
            "title": window_title(hwnd),
            "rect": (left, top, width, height),
            "iconic": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(callback, 0)
    return found


# ---------------------------------------------------------------- 抓取

def read_bgra(hwnd, width, height, method):
    """返回 BGRA 字节串；失败返回 None。method = print | screen。"""
    left, top, _, _ = window_rect(hwnd)
    if method == "screen":
        source_dc = user32.GetDC(None)          # 整个屏幕
        release_target = None
    else:
        source_dc = user32.GetWindowDC(hwnd)
        release_target = hwnd
    if not source_dc:
        return None

    memory_dc = gdi32.CreateCompatibleDC(source_dc)
    bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
    if not memory_dc or not bitmap:
        user32.ReleaseDC(release_target or 0, source_dc)
        return None
    gdi32.SelectObject(memory_dc, bitmap)

    if method == "screen":
        gdi32.BitBlt(memory_dc, 0, 0, width, height, source_dc, left, top, SRCCOPY)
    else:
        user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height      # 负数表示自上而下，省一次翻转
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0       # BI_RGB
    header.biSizeImage = width * height * 4

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    raw = buffer.raw

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(release_target or 0, source_dc)
    return raw


def bgra_to_rgb(raw, width, height):
    """BGRA -> RGB（丢掉 alpha；PrintWindow 出来的 alpha 一般是 0，不能直接用）。"""
    source = bytearray(raw)
    rgb = bytearray(width * height * 3)
    rgb[0::3] = source[2::4]
    rgb[1::3] = source[1::4]
    rgb[2::3] = source[0::4]
    return rgb


def stats(rgb, width, height):
    """非黑像素比例 + 平均 RGB，用来判断是不是抓到黑屏。"""
    lit = 0
    sum_r = sum_g = sum_b = 0
    step = 3
    for index in range(0, len(rgb) - 2, step):
        red = rgb[index]
        green = rgb[index + 1]
        blue = rgb[index + 2]
        if red or green or blue:
            lit += 1
        sum_r += red
        sum_g += green
        sum_b += blue
    total = max(1, width * height)
    return {
        "ratio": lit / float(total),
        "mean": (sum_r // total, sum_g // total, sum_b // total),
    }


def box_downscale(rgb, width, height, factor):
    """整数倍盒式平均缩小，用于压读图 token。"""
    new_w = max(1, width // factor)
    new_h = max(1, height // factor)
    out = bytearray(new_w * new_h * 3)
    for y in range(new_h):
        base_y = y * factor
        for x in range(new_w):
            base_x = x * factor
            r = g = b = 0
            for dy in range(factor):
                offset = ((base_y + dy) * width + base_x) * 3
                for dx in range(factor):
                    r += rgb[offset]
                    g += rgb[offset + 1]
                    b += rgb[offset + 2]
                    offset += 3
            count = factor * factor
            index = (y * new_w + x) * 3
            out[index] = r // count
            out[index + 1] = g // count
            out[index + 2] = b // count
    return out, new_w, new_h


# ---------------------------------------------------------------- PNG

def encode_png(rgb, width, height, level=6):
    """手写 PNG：8-bit、truecolor(RGB)、filter 0。"""
    stride = width * 3
    raw = bytearray((stride + 1) * height)
    source = memoryview(rgb)
    for y in range(height):
        target = y * (stride + 1)
        raw[target] = 0                       # filter type 0 (None)
        raw[target + 1:target + 1 + stride] = source[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + chunk(b"IEND", b""))


def write_bmp(path, rgb, width, height):
    stride = width * 3
    padding = (4 - stride % 4) % 4
    body = bytearray()
    for y in range(height - 1, -1, -1):        # BMP 自下而上
        row = rgb[y * stride:(y + 1) * stride]
        body += row + b"\x00" * padding
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = height
    header.biPlanes = 1
    header.biBitCount = 24
    header.biCompression = 0
    header.biSizeImage = len(body)
    with open(path, "wb") as handle:
        handle.write(b"BM")
        handle.write(struct.pack("<IHHI", 54 + len(body), 0, 0, 54))
        handle.write(bytes(header))
        handle.write(body)


# ---------------------------------------------------------------- 目标解析

def default_out_path(directory, name, extension):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    session = (os.environ.get("DSH_SESSION_ID") or "nosession")[:8]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "window"))[:24]
    return os.path.join(directory, "shot-%s-%s-%s.%s" % (stamp, session, safe, extension))


def print_window_table(windows):
    print("%-12s %-8s %-26s %-13s %s" % ("hwnd", "pid", "进程", "尺寸", "标题"))
    for item in windows:
        left, top, width, height = item["rect"]
        print("%-12s %-8d %-26s %-13s %s" % (
            hex(item["hwnd"]), item["pid"], process_image_name(item["pid"])[:26],
            "%dx%d" % (width, height), item["title"][:44],
        ))


def pick_dsh_window(windows):
    """DSH 桌面端主窗口：进程名匹配且面积最大。"""
    matches = [item for item in windows
               if "deepseek harness" in process_image_name(item["pid"]).lower()]
    if not matches:
        return None
    return max(matches, key=lambda item: item["rect"][2] * item["rect"][3])


def resolve_pid(args, windows):
    if args.pid:
        return args.pid, []
    keyword = args.process.lower()
    matches = [item for item in windows if keyword in process_image_name(item["pid"]).lower()]
    if not matches:
        print("[ERROR] 没有进程名包含 %r 的可见窗口，用 --list 看一遍。" % args.process)
        return None, []
    pids = sorted({item["pid"] for item in matches})
    return pids[0], pids


def main():
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="列出全部可见顶层窗口后退出")
    parser.add_argument("--dsh", action="store_true", help="抓 DeepSeek Harness 主窗口（面积最大的那个）")
    parser.add_argument("--process", help="进程名关键字，大小写不敏感，例如 SiNiSistar")
    parser.add_argument("--pid", type=int, help="直接给进程号")
    parser.add_argument("--hwnd", type=lambda text: int(text, 0), help="直接给窗口句柄，如 0xb70ebe")
    parser.add_argument("--title", help="窗口标题关键字，用于在多个窗口里挑一个")
    parser.add_argument("--out", help="输出路径；默认写到 <dir>/shot-<时间>-<会话>-<进程>.png")
    parser.add_argument("--dir", help="默认输出目录（DSH_SHOT_DIR 或系统 temp）")
    parser.add_argument("--format", choices=("png", "bmp"), default="png", help="输出格式，默认 png")
    parser.add_argument("--max-width", type=int, help="超过该宽度就整数倍缩小（省读图 token）")
    parser.add_argument("--scale", type=int, default=1, help="整数缩小倍数，默认 1（原尺寸）")
    parser.add_argument("--level", type=int, default=6, help="PNG 压缩级别 0-9，默认 6")
    parser.add_argument("--no-fallback", action="store_true", help="全黑时不自动改用屏幕抓取重试")
    parser.add_argument("--json", action="store_true", help="输出机读结果")
    args = parser.parse_args()

    awareness = set_dpi_awareness()
    windows = enum_visible_windows()
    if args.list:
        print("DPI 感知：%s，可见顶层窗口 %d 个" % (awareness, len(windows)))
        print_window_table(windows)
        return 0

    if not (args.dsh or args.process or args.pid or args.hwnd):
        parser.error("要给 --dsh、--process、--pid 或 --hwnd 之一；只想看窗口列表用 --list")

    if args.hwnd:
        target = next((item for item in windows if item["hwnd"] == args.hwnd), None)
        if target is None:
            # 句柄可能属于未在列表里的窗口，仍然尝试直接抓
            left, top, width, height = window_rect(args.hwnd)
            target = {"hwnd": args.hwnd, "pid": 0, "title": window_title(args.hwnd),
                      "rect": (left, top, width, height), "iconic": False}
    elif args.dsh:
        target = pick_dsh_window(windows)
        if target is None:
            print("[ERROR] 没找到 DeepSeek Harness 的可见窗口（客户端没开？用 --list 看一遍）。")
            return 2
    else:
        pid, candidates = resolve_pid(args, windows)
        if pid is None:
            return 2
        if len(candidates) > 1:
            print("[WARN] 进程名匹配到多个 pid：%s，取第一个；要精确指定用 --pid。"
                  % ", ".join(str(value) for value in candidates))
        targets = [item for item in windows if item["pid"] == pid]
        if args.title:
            filtered = [item for item in targets if args.title in item["title"]]
            if filtered:
                targets = filtered
        if not targets:
            print("[ERROR] pid %d 没有可见窗口。" % pid)
            return 2
        target = max(targets, key=lambda item: item["rect"][2] * item["rect"][3])

    if target["iconic"]:
        print("[WARN] 目标窗口已最小化，PrintWindow 可能返回空白。")

    left, top, width, height = target["rect"]
    if width <= 0 or height <= 0:
        print("[ERROR] 窗口尺寸异常：%dx%d。" % (width, height))
        return 1

    method = "print"
    raw = read_bgra(target["hwnd"], width, height, method)
    if raw is None:
        print("[ERROR] 抓取失败：拿不到窗口 DC 或兼容位图。")
        return 1
    rgb = bgra_to_rgb(raw, width, height)
    info = stats(rgb, width, height)

    if info["ratio"] < 0.001 and not args.no_fallback:
        fallback = read_bgra(target["hwnd"], width, height, "screen")
        if fallback is not None:
            fallback_rgb = bgra_to_rgb(fallback, width, height)
            fallback_info = stats(fallback_rgb, width, height)
            if fallback_info["ratio"] > info["ratio"]:
                rgb, info, method = fallback_rgb, fallback_info, "screen"

    scale = max(1, args.scale)
    if args.max_width and width // scale > args.max_width:
        scale = max(scale, (width + args.max_width - 1) // args.max_width)
    if scale > 1:
        rgb, width, height = box_downscale(rgb, width, height, scale)

    directory = args.dir or os.environ.get("DSH_SHOT_DIR") or os.path.join(
        os.environ.get("TEMP") or os.getcwd(), "dsh-shots")
    extension = "png" if args.format == "png" else "bmp"
    out = args.out or default_out_path(directory, os.path.splitext(process_image_name(target["pid"]))[0]
                                       or "window", extension)
    out = os.path.abspath(out)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if args.format == "png":
        with open(out, "wb") as handle:
            handle.write(encode_png(rgb, width, height, max(0, min(9, args.level))))
    else:
        write_bmp(out, rgb, width, height)

    size_bytes = os.path.getsize(out)
    payload = {
        "path": out,
        "format": extension,
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "ratio": round(info["ratio"], 4),
        "mean": list(info["mean"]),
        "method": method,
        "scale": scale,
        "dpi": awareness,
        "hwnd": hex(target["hwnd"]),
        "pid": target["pid"],
        "process": process_image_name(target["pid"]) if target["pid"] else "",
        "title": target["title"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print("进程 %s（pid %s）窗口 %s %dx%d 标题 %r" % (
        payload["process"] or "?", payload["pid"] or "?", payload["hwnd"],
        width, height, payload["title"]))
    print("已写出 %s（%s，%d 字节，抓取方式 %s%s）" % (
        out, extension, size_bytes, method,
        "，缩小 1/%d" % scale if scale > 1 else ""))
    print("非黑像素比例 %.3f，平均色 #%02x%02x%02x" % (
        info["ratio"], info["mean"][0], info["mean"][1], info["mean"][2]))
    if args.format == "png":
        print("下一步：直接用 read_image 读 %s" % out)
    if info["ratio"] < 0.001:
        print("[WARN] 画面几乎全黑。别急着当成抓不到，先读该进程自己的日志确认是不是真的卡住"
              "（游戏可看 Player.log），或加 --no-fallback 前先试 --hwnd 精确指定窗口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
