---
name: dsh-copturex
description: 抓取任意 Windows 进程可见窗口的真实渲染画面（PrintWindow + PW_RENDERFULLCONTENT，DPI 感知），直接导出 PNG 并可缩放压 token，同时给出非黑像素比例判断是否黑屏。当需要看某个正在运行的进程（游戏、应用、IDE、DSH 自己）当前渲染成什么样，或要判断画面是否黑屏、卡死、正常时使用。仅 Windows，只用 Python 标准库，不需要 Pillow。Codex 版 copturex 的 DSH 适配版（输出由 BMP 改为 PNG）。
---

# dsh-copturex

把指定进程的可见窗口抓成图片，看运行中程序的真实渲染结果。适用于普通截图拿不到的窗口：全屏、独占交换链、独立渲染的 IL2CPP 游戏，以及远程桌面里的子窗口。

和原版 `copturex` 的唯一动机差别：**原版输出 BMP，`read_image` 吃不了 BMP**（只支持 PNG/JPEG/WebP/GIF），必须先跑一遍 Pillow 转换，多一层依赖还多一步。这个版本直接写 PNG，抓完就能读。顺带补了按宽度缩放（省读图 token）、DSH 主窗口一键抓取、全黑时自动改屏幕抓取重试。

## 用法

```bash
# 先看有哪些可见窗口（pid、进程名、尺寸、标题）
python scripts/dsh_capture.py --list

# 抓 DSH 客户端主窗口（进程名含 deepseek harness、取面积最大者）
python scripts/dsh_capture.py --dsh --out shot.png

# 按进程名抓，默认写到 %TEMP%\dsh-shots\shot-<时间>-<会话id>-<进程>.png
python scripts/dsh_capture.py --process SiNiSistar

# 精确指定窗口，并压到 1200 宽再读（省 token）
python scripts/dsh_capture.py --hwnd 0xb70ebe --max-width 1200 --out small.png

# 机读结果，便于脚本判断
python scripts/dsh_capture.py --dsh --json
```

输出内容：

```
进程 DeepSeek Harness.exe（pid 36736）窗口 0xb70ebe 2582x1550 标题 '...'
已写出 C:\...\shot-20260902-181500-94811e6d-DeepSeek_Harness.png（png，155811 字节，抓取方式 print）
非黑像素比例 1.000，平均色 #1d1d1f
下一步：直接用 read_image 读 C:\...\shot-....png
```

## 参数

| 参数 | 说明 |
|---|---|
| `--list` | 列出全部可见顶层窗口后退出 |
| `--dsh` | 抓 DSH 客户端主窗口（面积最大的 `deepseek harness` 窗口） |
| `--process` | 进程名关键字，大小写不敏感；匹配多个 pid 时取第一个并 WARN |
| `--pid` / `--hwnd` | 直接给进程号 / 窗口句柄（`0xb70ebe` 这种十六进制也行） |
| `--title` | 在候选窗口里按标题关键字挑一个 |
| `--out` / `--dir` | 输出路径 / 默认输出目录（缺省 `%TEMP%\dsh-shots`，可设 `DSH_SHOT_DIR`） |
| `--format png\|bmp` | 默认 `png`；需要走老流程才用 `bmp` |
| `--max-width N` | 超过 N 就整数倍盒式缩小（只缩不放大），用于压读图 token |
| `--scale N` | 显式整数缩小倍数 |
| `--level 0-9` | PNG zlib 压缩级别，默认 6 |
| `--no-fallback` | 全黑时不自动改用屏幕抓取重试 |
| `--json` | 输出一行机读 JSON（path/width/height/bytes/ratio/mean/method/scale/hwnd/pid/process/title） |

## 读完图之后怎么判断

- `非黑像素比例` ≈ 1.000 → 抓到的就是真实渲染画面，可以直接下结论。
- `ratio` < 0.001 → 画面几乎全黑。先别断言"程序卡死"：有些窗口（UWP、部分 GPU 合成、最小化窗口）本来就不响 `PrintWindow`。脚本默认会自动改用屏幕抓取重试一次，`method` 会变成 `screen`；仍全黑时再去看该进程自己的日志（游戏看 `Player.log`），或用 `--hwnd` 精确指定窗格再试。
- 平均色偏暗（如 `#1d1d1f`）是深色主题的正常表现，不代表黑屏。

## 常见坑

1. **必须先 DPI 感知**：脚本已经做（`shcore.SetProcessDpiAwareness(2)`，回退 `SetProcessDPIAware()`）。不做的话高 DPI 屏上只能抓到缩放后的一部分，看起来像"窗口被裁了一半"。
2. **64 位句柄要用 `argtypes`**：所有收 HWND/HDC/HBITMAP 的函数都已声明，否则 ctypes 按 `c_int` 截断，`DeleteObject` 阶段会抛 `OverflowError`。
3. **`GetDIBits` 的 alpha 不可信**：统一按 `biHeight = -height`（自上而下）+ 32bpp `BI_RGB` 读，用 BGR 三个通道，alpha 直接丢掉。
4. **面积最大的那个窗口不一定是主窗口**：Electron 应用常有隐藏的 1x1 或不可见辅助窗口，用 `--list` 看清 pid/尺寸/标题再选。
5. **抓 DSH 自己是有用的**：客户端的渲染画面里能看到对话流、工具卡片、任务栏状态；要让别人确认"当前界面长什么样"时直接 `--dsh`。

## 实测（本机 2560x1600 / DSH 2582x1550 窗口）

| 场景 | 输出 | 字节 | 耗时 |
|---|---|---|---|
| `--dsh` 原尺寸 | png 2582x1550 | 155,811 | ~1.5s |
| `--dsh --max-width 1200` | png 860x516（scale 3） | 60,312 | 共 2.9s（含两次抓取缩放） |
| `--dsh --format bmp --max-width 600` | bmp 516x310 | 479,934 | ~2s |

同一画面原版走 BMP 是 16,008,454 字节，转 PNG 还要另装 Pillow；这里一次到位。

## 与原版 copturex 的差异

| | copturex | dsh-copturex |
|---|---|---|
| 输出 | BMP（`read_image` 读不了） | **PNG**，直接可读 |
| 依赖 | Pillow（转换时） | 纯标准库（zlib 手写 PNG） |
| 缩放 | 无 | `--max-width` / `--scale` 盒式缩小 |
| 全黑 | 只报告比例 | 自动改屏幕抓取重试并换 `method` |
| DSH 窗口 | 手动查 pid | `--dsh` 一键 |
| 机读 | 无 | `--json` |

两个技能可以共存：老脚本仍在 `C:\Users\deepd\.dsh\skills\copturex\scripts\capture_window.py`，需要维持旧流程时用它。
