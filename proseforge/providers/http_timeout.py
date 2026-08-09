"""Provider adapters 共享的 httpx 超时配置。

流式生成在长思考阶段可能数分钟没有字节，单一 30s 标量超时会掐断正常的长流：
read 放宽到 300s，connect/pool 保持短超时快速失败。
"""

from __future__ import annotations

import httpx

DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
