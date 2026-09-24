# video-scout 流程规范与实施记录

来源：用户原图 `docs/main.mmd`，SHA256 `bb1b412ae055b94a2ddcf43da9b17e7373cd9cee8e94bcdcdfc6df1904594c9b`。目录初始仅此图，无 Git 版本；原图不改。以下细化同时依据用户文字要求，工程假设单列。

## 流程复述

A 输入网址、N、目录 → B 校验并创建会话 → C 初始化队列、访问集合和视频集合。D 检查任务及预算；可继续则 E 检查范围、去重和限速 → F 获取页面 → G 判断成功。失败 G→H 记录并有限重试→D；成功 G→I 发现候选、详情、分页。

J 尚有候选且未停止 → K 解析 → L 有效且未收录？否→J；是→M 原子判重/额度/持久化/事件 → N 达到上限？否→J；是→O 停止派发并取消→S。J 无候选→P 按优先级入队→D；用户停止→O。D 无任务或保护限制→S。S 保留列表并显示准确原因，包括在途退出状态。

T 用户选择：查看或导出→U；下载勾选→V 固定目标目录/安全文件名→W 按需刷新→X 下载/合并→Y 验证最终文件，保存成功/失败/取消，允许重试。

异常补充（文字需求）：无效输入不得创建会话；暂停只停止派发，在途允许收尾；旧会话结果拒收；范围外重定向拒绝；失败候选单独保存且不计数；退出取消并释放资源；保护限制及不足 N 正常保留结果。

## 缺口与假设

- 原图未列暂停、历史、重定向、队列/响应大小、播放列表惰性消费、取消/退出：按用户文字补齐。
- 页面规范化保留完整 query 与 fragment，不猜分页结构。HTML 中 rel=next、明确下一页文本或分页容器是分页线索。
- 同一 video 元素下 sources 为明确同一视频；独立媒体只有完整地址相同或解析器稳定 ID 相同才合并；标题不参与判重。
- 默认限起始 hostname（端口可变化），允许路径按目录边界匹配。CDN 仅作媒体获取，不作网页导航。
- 默认无浏览器；可选浏览器依赖不可用时提示并回退 HTTP。站点专用登录、DRM、付费墙、验证码和无限滚动不作通用保证。
- 首版下载只能在扫描完全退出后开始。下载目录在任务创建时固定，SQLite v1 为本地单用户存储。

## 边界和契约

`domain`：值类型、URL 范围与稳定身份；不导入 HTTP、数据库、Textual 或 yt-dlp。
`services`：扫描调度和原子收录；下载任务生命周期；依赖 `domain.ports`。
`adapters`：HTTPX 获取/探测、Beautiful Soup 发现、可终止 yt-dlp 子进程、可选 Playwright、SQLite。
`tui`：参数输入、稳定 ID 表格、事件展示、勾选、详情和操作；不处理扫描规则。
`bootstrap`：唯一生产组装入口；注入 fetcher/discoverer/extractor/resolver/downloader/repository；退出统一关闭。

契约见 `src/video_scout/domain/ports.py`：fetch 为异步有界获取；discover/extract 对有界文档纯解析；resolve 是异步惰性迭代器、可关闭、受预算限制；download 返回已生成最终文件才允许成功；Repository 异步串行事务，不阻塞消息循环。可预期失败统一 ScoutError；取消保持 asyncio.CancelledError。

主骨架：`TUI → ScanService.run(config) → fetch → extract → resolve → admit(session_id,item) → repository + Event`；候选处理后入队页面。`TUI → DownloadService.enqueue(items,dir) → downloader.download → verify → repository + Event`。

## 分阶段执行

阶段 1 契约与骨架、阶段 2 本地主链路、阶段 3 TUI/历史/导出/可选适配器、阶段 4 回归与交付均已实施。下表由初始化时的计划映射更新为实际符号与测试；执行环境、命令、最终计数及未验证部分见 [../verification.md](../verification.md)。

| 节点/分支 | 规则与结果 | 实际代码 | 测试文件及关键用例 | 实现 / 验证 |
|---|---|---|---|---|
| A–C，B 成功/失败 | 校验 HTTP/HTTPS、正整数；非法输入无会话 | `domain.models.ScanConfig.validate`、`services.scan.ScanService.run` | `test_scan::test_invalid_limit_does_not_create_session`、`test_domain_export::test_reject_non_http_and_embedded_credentials` | 已实现 / 通过 |
| D 继续/耗尽 | N=1/3/100 精确；仅37个正常耗尽 | `ScanService.run` | `test_integration::test_local_real_media_strict_limit_and_no_later_pagination`、`test_37_results_normal_completion_deep_details_pagination_and_history` | 已实现 / 本地真实通过 |
| D 页面/时间/队列保护 | 保留结果并显示准确保护原因 | `ScanService._enqueue/run` | `test_integration::test_page_protection_limit_preserves_partial_results`、`test_queue_truncation_is_reported_instead_of_claiming_exhaustion`、`test_scan::test_deadline_interrupts_stalled_request` | 已实现 / 通过 |
| E–H 成功/重试/拒绝 | 重定向范围；403不重试；429退避；响应大小限制 | `adapters.http.HttpPageFetcher`、`domain.urls.in_scope` | `test_integration::test_http_retry_access_limits_body_limit_and_redirect_scope`、`test_security_review::test_path_scope_cannot_be_bypassed_before_httpx_normalization` | 已实现 / 通过 |
| I/P 详情/分页/普通/循环 | 详情优先；分页不耗深度；同批不跨优先级；保守URL | `adapters.http.HtmlDiscovery`、`ScanService._enqueue/run` | `test_integration` 的37条、多层详情、分页与并发3；`test_security_review::test_queue_budget_keeps_later_priority_links` | 已实现 / 通过 |
| J–L 有效/失败/重复 | 4KiB样本/解析器确认；候选不计数；标题不去重 | `adapters.media.HybridVideoResolver`、`ScanService.admit` | `test_media::test_probe_bound_when_server_ignores_range`、`test_scan::test_duplicate_titles_are_distinct_but_stable_ids_merge_formats`、`test_failed_candidates_are_recorded_without_counting` | 已实现 / 通过 |
| K 播放列表循环 | 逐项pull、没有提前枚举；重复不花唯一额度 | `HybridVideoResolver.resolve`、`ytdlp_worker.info_records` | `test_media::test_playlist_enumeration_is_lazy_and_generic_ids_are_not_stable`、`test_duplicate_playlist_entries_do_not_consume_unique_quota` | 已实现 / 受控惰性迭代测试通过，未对公网站点验收 |
| M–N 未满/恰满/竞争 | 原子会话判重与预算，第N个立即关派发；保存失败停止 | `ScanService.admit`、`SQLiteRepository.save_video` | `test_scan::test_atomic_admission_near_99_never_overshoots`、`test_persistence_failure_cannot_report_successful_limit` | 已实现 / 通过 |
| O–S 达标/用户/暂停/异常 | 取消并等待清理；旧会话拒收；保持已发现项 | `ScanService.stop/pause/resume/close` | `test_scan` 的pause/stop/close/old_session/old_finish；`test_security_review::test_repeated_cancel_preserves_adapter_cleanup` | 已实现 / 通过 |
| T–U 详情/复制/导出 | 稳定ID勾选；TXT实际媒体；JSON/CSV无认证头；纯文本 | `tui.app.VideoScoutApp/DetailScreen`、`services.export.export_items` | `test_tui`、`test_domain_export::test_exports_media_and_preserve_signatures_but_exclude_context` | 已实现 / 无头交互通过；实际终端剪贴板见限制 |
| V 固定任务与目标 | 中文/空格目录；安全标题+稳定短ID；扫描期拒绝下载 | `DownloadService.enqueue`、`safe_filename` | `test_download` 的real_scan/failed_task_retry/safe_filename/downloading_requires_finished_scan | 已实现 / 通过 |
| W–X 解析/下载/合并 | 子进程JSON；请求上下文隔离；HLS/DASH；实际FFmpeg | `adapters.media.YtDlpDownloader`、`ytdlp_worker.download/safe_youtube_dl` | `test_media::test_real_hls_resolve_and_download`、`test_real_dash_multiple_formats_and_ffmpeg_merge`、`test_ytdlp_redirect_context_domain_isolation_and_scheme_validation` | 已实现 / localhost真实通过；公网地址刷新未验收 |
| Y 完成/失败/取消/重试 | 真实非空最终文件、原子无覆盖、暂存续传、持久状态 | `DownloadService._execute/retry`、`YtDlpDownloader.download` | `test_download`、`test_media::test_cancel_download_leaves_no_final_file/test_worker_cancellation_reaps_process_group` | 已实现 / 本地+受控取消通过 |
| F 可选浏览器 | 默认关闭；缺依赖明确回退；有预算渲染/滚动/媒体发现 | `adapters.browser.OptionalBrowserFetcher` | `test_media::test_browser_disabled_never_initializes_sdk/test_missing_browser_warns_and_keeps_basic_mode` | 已实现 / 禁用与回退通过；真实Chromium未执行 |
| 工程支撑 | 历史恢复、导入边界、纯HTTP方案、整条TUI路径 | `bootstrap.Runtime`、`SQLiteRepository`、`__main__` | `test_domain_export::test_architecture_dependencies_and_no_shell_execution`、`test_download::test_restart_restores_interrupted_task_for_retry`、`test_e2e` | 已实现 / 通过 |

## 实现约束及图代码差异

- 保留原图未改；`docs/runtime.mmd` 细化原图省略的暂停、会话隔离、关闭资源、保护限制和错误路径。
- SQLite 在专属单线程执行器内运行；一次视频写入为事务。原子收录锁覆盖内存判重/额度及事务完成；达上限先禁止新派发，存储失败回滚内存并把会话结束原因改为失败。会话完成写入前不允许开启新会话。
- 页面调度使用优先堆及访问/已入队集合；候选使用有界 deque。HTML适配器最多返回队列上限+1项，额外一项只作为溢出信号，服务实际不突破队列容量；选取时优先保留详情/分页。
- `budget` 是剩余唯一条目额度提示。解析器不知道全局重复集合，因此原始枚举另受 `max_queue` 限制，逐次 `anext` 才允许解析下一个项目，业务达到唯一N立即关闭迭代器。
- 页面与HTML解析可在工作线程执行，有大小约束；退出等待不可取消的解析收尾。yt-dlp及FFmpeg使用可终止进程组。生产实现均由bootstrap注入；服务测试中的MemoryRepository/ControlledFetcher不是生产实现。
- 原图“复制”经终端OSC52请求实现，终端可能禁用，提供可选中文本和文件导出退路。
- 可选浏览器不猜测任意JavaScript“下一页”按钮；常规URL分页由统一队列调度，避免渲染阶段提前点击下一页违反N限额。复杂无URL按钮需要站点专用适配器，该增强未实现；无浏览器基础链路已验收。
- 自动登录态导入、DRM/付费/验证码绕过、动态插件市场、Redis/服务拆分为明确范围外。公网服务变化与真实浏览器渲染不由本地测试证明。
