# dsh-copturex

把**任意 Windows 进程的可见窗口**抓成真实渲染画面，直接输出 PNG，抓完就能被 AI 读图工具读取。

这是 Codex 生态里 `copturex` 技能的 **DeepSeek Harness（DSH）适配版**。核心改动只有一件但很关键：

| | Codex 版 `copturex` | **DSH 版 `dsh-copturex`** |
|---|---|---|
| 输出格式 | BMP | **PNG**（手写编码器，不需要 Pillow） |
| 同一画面体积 | 16,008,454 B | **155,811 B**（约 100×） |
| 读图兼容 | 需先转格式（`read_image` 只吃 PNG/JPEG/WebP/GIF） | 抓完直接读 |
| 缩放控费 | 无 | `--max-width` / `--scale` |
| 黑屏兜底 | 无 | 自动改用屏幕抓取重试 |
| 机读输出 | 无 | `--json` |

## 安装

```bash
git clone https://github.com/ArbeiterKlassen/dsh-Copturex-SKILL.git \
  "$USERPROFILE/.dsh/skills/dsh-copturex"
```

DSH 会热发现新技能目录，**不用重启**。装好后让 agent「截个图看看」即可触发。

手工安装也行：把本仓库目录整个复制成 `%USERPROFILE%\.dsh\skills\dsh-copturex\`。

## 用法

```bash
cd "$USERPROFILE/.dsh/skills/dsh-copturex/scripts"

python dsh_capture.py --list                     # 列出可见顶层窗口（pid / 进程 / 尺寸 / 标题）
python dsh_capture.py --dsh                      # 抓 DSH 主窗口
python dsh_capture.py --process MTool            # 按进程名关键字抓
python dsh_capture.py --pid 36736 --max-width 1200
python dsh_capture.py --hwnd 0xb70ebe --json     # 机读结果
```

默认输出目录优先级：`--out` → `--dir` → 环境变量 `DSH_SHOT_DIR` → `%TEMP%\dsh-shots`，
文件名形如 `shot-20261030-182331-<sessionid>-<进程>.png`。

## 参数

| 参数 | 说明 |
|---|---|
| `--list` | 列出全部可见顶层窗口后退出 |
| `--dsh` | 抓 DeepSeek Harness 主窗口（面积最大的那个） |
| `--process` | 进程名关键字，大小写不敏感 |
| `--pid` / `--hwnd` | 直接指定进程号 / 窗口句柄（如 `0xb70ebe`） |
| `--title` | 窗口标题关键字，多个窗口里挑一个 |
| `--out` / `--dir` | 输出路径 / 默认输出目录 |
| `--format {png,bmp}` | 默认 `png`；`bmp` 保留给旧流程 |
| `--max-width` / `--scale` | 超宽自动整数缩小 / 指定整数缩小倍数（省读图 token） |
| `--level` | PNG 压缩级别 0-9，默认 6 |
| `--no-fallback` | 全黑时不自动改用屏幕抓取重试 |
| `--json` | 输出机读结果 |

每次运行都会打印**非黑像素比例**和平均色：比例接近 0 基本就是黑屏/卡死，比例接近 1 说明抓到了真实画面。

## 实测

| 目标 | 命令 | 结果 |
|---|---|---|
| DSH 主窗口 2582x1550 | `--dsh` | 155,811 B PNG，ratio 1.000 |
| DSH 主窗口 | `--max-width 1200` | 860x516，60,312 B，scale 3，约 2.9 s |
| MTool（魔法少女苍蓝星）1578x1529 | `--process MTool --max-width 900 --json` | 789x764，254,535 B，UI 文字可读 |
| 同上 | `--format bmp --max-width 600` | 516x310，479,934 B |

## 实现要点

- **PrintWindow + `PW_RENDERFULLCONTENT`(2)**：能抓到被遮挡/后台窗口的真实 DWM 合成画面，不是屏幕上"看得见的那块"。
- 全部走 `ctypes` 直连 `user32`/`gdi32`/`shcore`，**纯标准库**，Windows 自带 Python 就能跑。
- 所有收句柄的函数都显式声明 `argtypes`，否则 64 位下句柄会被截断报 `OverflowError`。
- DPI 感知用 `SetProcessDpiAwareness(2)`（per-monitor v2），回退 `SetProcessDPIAware()`，否则高分屏抓出来是模糊缩放图。
- PNG 是手写的：`zlib.compress` + `zlib.crc32`，8-bit truecolor、filter 0、IHDR/IDAT/IEND 三块。
- 抓取失败（全黑）时自动改用 `GetDC(NULL)` + `BitBlt` 屏幕抓取重试，取更亮的结果，并在输出里报告实际用的 `method`。

## 局限

- 仅 Windows。
- 独占全屏 / 部分 UWP / DirectComposition 直出的窗口可能抓成黑屏——此时会走屏幕抓取兜底，兜底也黑就说明该窗口不响应 `WM_PRINT`。
- 最小化窗口通常抓不到内容。

## 许可

MIT
