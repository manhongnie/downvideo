# 本地演示站点

在仓库根目录运行 `python -m demo.server --port 8765 --total 137`，再在 video-scout
中输入 `http://127.0.0.1:8765/catalog?p=1`。`--total 37 --per-page 3` 可验证不足上限
以及超过普通链接深度的正常分页链。测试站点只监听本机。

目录使用相对下一页地址、两层详情页、重复视频链接、重复 source 标签和循环链接。
`/outside` 重定向到同一端口的 `localhost` 主机，用于验证起始主机为 `127.0.0.1`
时不会继续扫描范围外网页。`/stats` 返回实际请求计数；集成测试还直接读取
`DemoServer.requests` 检查达到上限后没有请求下一页。

其他测试端点：`/flaky` 首次返回 503，`/limited` 始终返回 429，`/restricted`
返回 403，`/oversized` 提供 100 KB 响应，`/not-video.mp4` 实际返回 HTML。

`tiny.mp4` 是仓库自带的 0.6 秒蓝色画面，包含有效 H.264 视频，生成命令：

```bash
ffmpeg -f lavfi -i color=c=blue:s=64x48:r=5 -t 0.6 -c:v libx264 \
  -pix_fmt yuv420p -movflags +faststart -y demo/tiny.mp4
```

每个 `/media/<编号>.mp4` 都提供该真实文件，支持 HEAD 和 Range。相同字节的不同
地址模拟不同视频标识，应用不会根据标题或内容猜测它们是同一视频；重复的完整
地址则应去重。这是受控测试数据，不是第三方网站兼容性证明。

运行 `python -m pytest tests/test_integration.py tests/test_download.py tests/test_e2e.py`
验证真实 HTTP 获取、扫描、持久化、下载、最终文件与无头 Textual 交互。
