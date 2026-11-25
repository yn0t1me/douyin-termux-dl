# 抖音无水印轻量下载器 (Termux 版)

参照 **QingZai（轻载）** 的解析思路实现的 **纯 Python 标准库** 脚本：

- **零 `pip` 依赖**：仅需 Termux 自带 `python`，无需装编译工具链，不踩 `cryptography`/`lxml` 编译坑。
- **免手动配置 Cookie**：模拟移动端 Safari 请求 `iesdouyin.com` 分享页 SSR，自动持久化首次下发的 `ttwid` Cookie 并重试，再从 `window._ROUTER_DATA` 中解析无水印视频（`playwm` → `play`）、无损原图和 `.mp3` 背景音乐。
- **极简手机触发**：同时支持 **Termux:Widget 桌面图标**（剪贴板一键下载）、**系统分享菜单**（`termux-url-opener` / `termux-file-editor`）、以及 **单独提取音频**。

---

## 一、手机端准备

从 **GitHub Releases**（同一来源，勿混装 F-Droid）安装：

1. [Termux](https://github.com/termux/termux-app/releases)（`arm64-v8a` 或 `universal`）
2. [Termux:API](https://github.com/termux/termux-api/releases)（用于读剪贴板、弹 Toast、刷新相册）
3. [Termux:Widget](https://github.com/termux/termux-widget/releases)（可选，用于在桌面添加一键下载图标）

---

## 二、Termux 安装与更新

### 1. 首次安装（推荐通过 Git 克隆，方便后续一键更新）
在 Termux 中依次执行：

```bash
# 1. 安装 Git、Python、Termux API 与 ffmpeg
pkg update && pkg install -y git python termux-api ffmpeg

# 2. 授权访问手机存储（弹窗点允许）
termux-setup-storage

# 3. 克隆仓库并运行一键安装
cd ~
git clone https://github.com/yn0t1me/douyin-termux-dl.git
cd ~/douyin-termux-dl
bash install_termux.sh
source ~/.bashrc
```

### 2. 日常拉取更新
通过 Git 克隆安装后，后续只需在 Termux 里执行：

```bash
dy-update
```
或手动更新：
```bash
cd ~/douyin-termux-dl && git pull --ff-only && bash install_termux.sh
```
*(本地配置 `~/.config/dy-dl/config.json` 独立保存，更新代码不会覆盖你的 API Key。)*

---

## 三、日常使用

### 1. 桌面一键下载（最稳，推荐）
1. 在手机桌面添加 **Termux:Widget** 小组件，可按需选择 **`抖音下载`**、**`抖音下音频`**、**`抖音提字幕`**、**`抖音下视频+字幕`**。
2. 在抖音内点击 **分享 → 复制链接**。
3. 回到桌面点击对应图标，脚本自动从剪贴板提取链接并执行下载或转录。

### 2. 语音转录配置（默认硅基流动免费 SenseVoiceSmall）
首次使用转录（`dys` / `dyt` / `-s` / `-t`）前，配置一次 API Key（到 [硅基流动控制台](https://cloud.siliconflow.cn/) 创建 API 密钥）：

```bash
dy --set-key "sk-你的硅基流动Key"
```

如需切换为其他 OpenAI 兼容语音接口（例如 Groq `whisper-large-v3-turbo` 获取逐句精确时间轴）：
```bash
dy --set-key "gsk_你的GroqKey" \
  --asr-url "https://api.groq.com/openai/v1/audio/transcriptions" \
  --asr-model "whisper-large-v3-turbo"
```

### 3. 抖音系统分享直接触发
在抖音点击 **分享 → 更多 → Termux**（部分分享带文案会触发 Termux 编辑确认，点打开/确认即可自动识别链接下载）。

### 4. 命令行直接调用（Termux 或电脑都能用）
```bash
# 直接传入抖音分享口令或链接
dy "4.82 复制打开抖音... https://v.douyin.com/xxxxx/"

# 仅下载音频（优先下载独立音频直链；若分享页未提供，有 ffmpeg 时自动从视频提取 .mp3/.m4a）
dy -a "https://v.douyin.com/xxxxx/"
# 或使用快捷命令
dya "https://v.douyin.com/xxxxx/"

# 仅提字幕与逐字稿（不保留视频，生成 .txt + .srt 并自动复制逐字稿到剪贴板）
dy -s "https://v.douyin.com/xxxxx/"
# 或使用快捷命令
dys "https://v.douyin.com/xxxxx/"

# 下载视频并生成同名 .srt / .txt 字幕（自动复制到剪贴板）
dy -t "https://v.douyin.com/xxxxx/"
# 或使用快捷命令
dyt "https://v.douyin.com/xxxxx/"

# 转录后不自动覆盖剪贴板
dys --no-copy

# 下载视频/图集的同时也保存一份背景音乐
dy -m "https://v.douyin.com/xxxxx/"

# 指定保存目录
dy -o ~/downloads "https://v.douyin.com/xxxxx/"

# 动图作品不合并音频，分别保存视频和音频
dy --no-merge "https://v.douyin.com/xxxxx/"
```

---

## 四、默认保存位置

- **Android (Termux)**：
  - 视频 / 动图（含 `-t` 生成的同名 `.srt` / `.txt`）：`/storage/emulated/0/Movies/Douyin`
  - 图集：`/storage/emulated/0/Pictures/Douyin`
  - 音频：`/storage/emulated/0/Music/Douyin`
  - 仅提字幕（`dys` / `-s`）：`/storage/emulated/0/Documents/Douyin`
- **电脑 / 无共享存储权限时**：当前目录下的 `./DouyinDownloads`

> **关于 `.srt` 时间轴说明**：硅基流动 `FunAudioLLM/SenseVoiceSmall` 接口仅返回纯文本（无逐句时间戳），因此 `.txt` 逐字稿为接口原汁原味输出，`.srt` 时间轴按句长比例自动估算；若需要帧级精确字幕，可按上文切换为支持 `verbose_json` 的 Groq Whisper 接口。

---

## 五、与 QingZai 的能力边界说明

- **完全支持**：单条无水印视频、多图图文、独立音频下载（直链下载，或由 `ffmpeg` 自动从无水印视频流提取 `.mp3`/`.m4a`）。
- **部分差异（单图动图/实况）**：如果某个单图实况帖在分享页 SSR 中未直接包含视频流地址（QingZai 会起后台隐藏 WebView 去嗅探 JS 加载的视频流），纯命令行脚本没有浏览器内核可运行 JS，此时会自动提示并保存高清原图，加 `-m` 可同时保存背景音乐。
- **不支持批量主页**：本脚本聚焦免 Cookie 单条极简下载；博主主页（`/user/`）或收藏夹批量抓取仍需依赖带登录态 Cookie 的工具（如 `f2`、`TikTokDownloader`）。
