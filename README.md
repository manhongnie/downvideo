# video-scout

Ubuntu / Python 3.11+ 终端视频发现工具，使用 Textual。输入起始网页、唯一视频上限 N 和下载目录，跨页扫描、实时查看媒体格式，扫描结束后勾选下载或导出。

默认 **N=100、普通链接深度 3、页面上限 500、截止时间 30 分钟、页面并发 1、下载并发 2**。扫描本身不下载完整视频。不足 N 个是正常结果；停止扫描保留已发现记录。

## Ubuntu 准备与启动

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg
python3 --version  # 要求 3.11 或更新；Ubuntu 24.04 自带 3.12
```

推荐用已安装的 [uv](https://docs.astral.sh/uv/) 按锁文件安装：

```bash
uv sync --locked
uv run video-scout
# 或
uv run python -m video_scout --url 'http://127.0.0.1:8765/catalog?p=1'
```

不用 uv 时可使用随项目导出的锁定依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
video-scout
```

使用 conda 时：

```bash
conda create -y -n downvideo -c conda-forge --override-channels python=3.12 ffmpeg pip
conda activate downvideo
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
video-scout
```

`uv.lock` 包含解析后的精确版本；基础依赖为 Textual、HTTPX、Beautiful Soup 和 yt-dlp。FFmpeg 为系统依赖，分离的音视频轨道需要它合并。使用实际容器扩展名，不强制伪装成 MP4。

可选浏览器增强：

```bash
uv sync --locked --extra browser
uv run playwright install chromium
# Ubuntu 如提示缺系统库：uv run playwright install-deps chromium
```

在高级设置勾选浏览器增强。未安装 Playwright 或 Chromium 会显示明确提示并继续 HTTP 基础扫描。普通 HTML 分页无需浏览器。

若所在网络需要 HTTP 代理，启动时显式传入代理地址：

```bash
video-scout --proxy 'http://127.0.0.1:10808' --url 'https://example.com/'
```

## 使用

1. 上方填 HTTP/HTTPS 起始网址、下载目录、正整数 N。Linux 终端通常用 Ctrl+Shift+V 或右键粘贴外部网址；Ctrl+V 是 Textual 的应用内粘贴。也可通过 `--url` 在启动时预填。高级设置可改深度、页数、时间、并发、允许主机/路径、限速、重试、队列和响应大小。
2. 点击「扫描」或 F5。列表只计已解析确认的唯一视频，疑似页、失败候选和分片不计入 N。日志显示失败原因。
3. F6 暂停/继续派发，F7 停止。暂停期间已发出的请求可能收尾；停止会先显示「停止派发」，清理完成后显示「所有在途任务已退出」。30 分钟截止时间包含暂停时间。
4. 在视频结果表回车或「勾选/取消」。筛选与标题排序不会改变勾选对象；「全选当前」只增加当前可见结果，「取消选择」清空全部选择。
5. 「查看详情」显示来源网页、播放页、所有已解析格式和完整媒体地址。未知的时长、分辨率、编码显示未知。窄屏使用弹窗详情，整个工作区可滚动，Tab 可依次聚焦按钮；F5/F6/F7/Ctrl+Q 不占用普通输入字符。
6. 扫描完全结束后「下载选中项」。任务创建时固定目标目录，之后改输入框不改变旧任务。下载任务页可重试失败/取消项、取消当前/全部任务。目录自动创建，支持中文和空格。
7. 「导出设置」选择全部/当前勾选、TXT/CSV/JSON 和文件路径。TXT 仅写实际媒体 URL；CSV/JSON 分开保存来源页、播放页和媒体格式。不会覆盖已有导出文件。复制使用终端剪贴板协议，终端不支持时可在详情中选择文本或导出。
8. Ctrl+Q 或「退出」会停止扫描、下载并释放数据库、HTTP、浏览器及媒体子进程。

媒体 URL 可能带有效签名且会过期，**不要公开分享导出文件**。正常导出不含 Cookie 或认证请求头，日志会遮盖 URL 查询参数与认证信息。页面标题及错误按纯文本显示。

## 本地演示（可真实下载）

在第一个终端启动测试站点：

```bash
uv run python -m demo.server --port 8765 --total 137
```

第二个终端：

```bash
uv run video-scout --url 'http://127.0.0.1:8765/catalog?p=1'
```

设 N=3 可快速验证，设 N=100 可测试严格限量。站点包含多层详情、相对地址、连续分页、循环和重复链接。`demo/tiny.mp4` 是实际小型视频，各 URL 表示不同视频条目（测试夹具复用同一文件内容；程序不按文件内容或标题去重）。`--total 37` 用于验证不足 N 正常结束。更多路由和重建视频方法见 [demo/README.md](demo/README.md)。

## 范围、身份和保护

- 默认网页导航限起始 **hostname**，额外主机显式配置；路径前缀按目录边界匹配。后续 HTTP/浏览器导航重定向同样检查范围。已发现媒体可以位于外部 CDN；不会沿 CDN 继续遍历网页。
- 详情优先于分页，分页优先于普通链接。一批并发页面不跨优先级。明确分页链不消耗普通链接深度，但仍受总页数/时间限制。不猜 `page=2`，也不丢弃查询参数或前端路由片段。
- 统一原子入口检查会话、身份与额度，第 N 个视频立即停止新派发并取消可取消的在途操作。已发出的底层请求可能短暂收尾，程序不会声称远端请求被撤回。
- 解析器名+稳定视频 ID 优先；无可靠 ID 时保留完整媒体 URL，包括签名。明确属于同一 HTML video 元素的 source 合并。仅标题相同不会合并，不确定相同的资源可能保守显示为多条。
- 媒体后缀仅是线索；有限探测或 yt-dlp 返回有效格式后才收录。媒体探测最多读取 4 KiB 样本；网页响应默认不超过 2 MB。候选/页面队列有界，播放列表按消费者请求逐项解析，受候选保护额度约束。
- 下载支持 yt-dlp 能力范围内的 `.part`/分片续传；服务器忽略 Range、动态签名或部分协议可能只能重下。未完成内容留在目标目录 `.video-scout-partials/`，不会冒充完整成品。最终发布采用不覆盖的原子操作。已完成文件同名时任务失败，请检查文件或换目录。

## 历史与数据库

默认数据库：`${XDG_DATA_HOME:-~/.local/share}/video-scout/scout.sqlite3`。也可指定：

```bash
video-scout --database '/path/中文 历史.sqlite3'
```

SQLite 保存扫描会话、视频所有格式/来源、候选错误和下载记录。启动载入最近一次扫描，可通过历史下拉框切换会话；上次进程中断的下载会恢复为可重试状态，不自动下载。记录可能含签名媒体 URL，应按私人文件保存。

数据库 schema v1，事务写入，独立单线程执行器处理数据库 I/O。无旧版项目迁移；打开更新版本数据库会拒绝写入。备份需退出程序后复制数据库；删除数据库会丢历史但不会删除已下载文件。替换 Repository 必须实现相同事务/取消契约，不表示数据库格式能自动兼容。

## 测试

```bash
uv sync --locked
uv run python -m pytest -q
uv run ruff check .
```

测试使用本地 HTTP 站点和实际 FFmpeg/FFprobe；Textual 使用 `App.run_test()` 无头交互。受控替身仅用于确定性竞态、暂停、故障和取消测试，不代表外部站点联通。已执行结果、跳过项和范围见 [docs/verification.md](docs/verification.md)。

## 架构与限制

[流程规范及节点映射](docs/design/flow-spec.md)、[架构图](docs/architecture.mmd)、[细化运行流程](docs/runtime.mmd)。原始需求图 [docs/main.mmd](docs/main.mmd) 保留。`domain` 定义模型与端口，`services` 编排扫描和下载，`adapters` 隔离第三方实现，`tui` 只负责展示交互；`bootstrap.py` 集中组装。替换一个适配器无需改业务流程；测试替身验证这一边界。

支持普通 HTML 的 video/source、媒体链接、嵌入线索、明确分页，以及 yt-dlp 当前可处理的站点、HLS/DASH。**不承诺任意网站可用**：混淆播放器、需要登录、复杂 JavaScript 分页/点击、非标准接口和反自动化机制可能需要专用适配器。浏览器增强提供有次数/时间预算的渲染、滚动及网络媒体发现；不会猜测含糊的页面按钮含义。默认不加载浏览器用户配置或 Cookie，不实现登录态导入、DRM、付费限制或验证码绕过。仅用于你有权访问和下载的内容。

yt-dlp 封装在独立进程组中，使用 Python API 和结构化 JSON 消息，退出时清理其 FFmpeg 子进程。第三方站点变化可能需更新 yt-dlp 并重新测试锁文件。官方接口参考：[Textual 测试](https://textual.textualize.io/guide/testing/)、[DataTable 行键](https://textual.textualize.io/widgets/data_table/)、[yt-dlp Python API](https://github.com/yt-dlp/yt-dlp#embedding-yt-dlp)。
