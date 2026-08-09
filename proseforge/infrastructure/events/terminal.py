"""SSE 流的 terminal 事件集合（V2-002）。

subscribe 在回放 + live tail 过程中遇到其中任一事件即结束生成器；
路由层的心跳注释帧与断连清理由路由负责。
"""

TERMINAL_EVENTS = frozenset({"message.completed", "message.failed"})
