from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# V3 agent 调用的角色识别：OpenAI provider（proseforge/providers/openai.py）只把
# model/input/text 等字段放上网线，GenerationRequest.metadata 不透传；角色提示词
# （prompts.build_task_prompt / review_handlers._review_user_prompt /
# chief_handler._compose_appendix）首行固定为“任务：<key>（角色 <role>）”，
# 从这里解析角色。request JSON 若携带 metadata.role（前向兼容通道）则优先采纳。
# 非 V3 调用（无角色标记）保持既有行为逐字节不变。
_ROLE_PATTERN = re.compile(r"（角色\s*([a-z_]+)）")
_ARTIFACT_ID_PATTERN = re.compile(r"artifact_id=([^\s\[\]]+)")

_V3_ROLES: frozenset[str] = frozenset({
    "chief_planner",
    "story_architect",
    "world_builder",
    "character_designer",
    "timeline_analyst",
    "scene_writer",
    "style_editor",
    "continuity_reviewer",
    "adversarial_reviewer",
    "merge_editor",
    "chief_editor",
})
_REVIEWER_ROLES: frozenset[str] = frozenset({"continuity_reviewer", "adversarial_reviewer", "style_editor"})

# V3 角色调用用小 usage：一次 5 任务 run 实测结算 ~60 token，不撑爆 e2e 的小 budget_limit。
_ROLE_USAGE: dict[str, int] = {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}


def _role_of(request: dict[str, object]) -> str | None:
    metadata = request.get("metadata")
    if isinstance(metadata, dict):
        role = metadata.get("role")
        if isinstance(role, str) and role in _V3_ROLES:
            return role
    for block in request.get("input") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            match = _ROLE_PATTERN.search(text)
            if match and match.group(1) in _V3_ROLES:
                return match.group(1)
    return None


def _first_artifact_id(request: dict[str, object]) -> str | None:
    # 评审提示词逐行列出“artifact_id=<id> [type] ...”；取首个作为 findings 的目标。
    for block in request.get("input") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            match = _ARTIFACT_ID_PATTERN.search(block["text"])
            if match:
                return match.group(1)
    return None


def _role_payload(role: str, request: dict[str, object]) -> dict[str, object]:
    if role in _REVIEWER_ROLES:
        #  findings 文本与证据 quote 对所有评审角色保持一致：detect_conflicts 只在
        # 同证据且不同结论时成组，雷同输出保证 happy path 不会意外造出跨评审冲突。
        target = _first_artifact_id(request)
        return {
            "summary": f"Mock {role} review.",
            "findings": [
                {
                    "finding": "Mock review finding.",
                    "severity": "warning",
                    "target_artifact_id": target,
                    "evidence_spans": [{"artifact_id": target or "", "start": 0, "end": 4, "quote": "Mock"}],
                    "verdict": "warning",
                }
            ],
        }
    if role == "scene_writer":
        # SceneDraft 形态（title/content），但类型必须落在 RolePolicy allowlist
        # （report/candidate）内，否则 executor 的服务端校验会把任务置 FAILED。
        return {"artifact_type": "candidate", "title": "Mock scene", "content": "Mock scene content: the checkpoint holds."}
    if role == "world_builder":
        # world_builder 的 allowlist 只有 story_fact。
        return {"artifact_type": "story_fact", "rule": "Mock world rule.", "scope": "mock"}
    if role == "chief_editor":
        # chief_handler._compose_appendix 读取 output["appendix"]。
        return {"appendix": "Mock chief merge appendix."}
    # chief_planner / story_architect / character_designer / timeline_analyst /
    # merge_editor（确定性路径不调模型，走到这里也安全）：candidate 在 allowlist 内，
    # payload 只需非空 JSON 对象。
    return {
        "artifact_type": "candidate",
        "summary": f"Mock {role} output.",
        "outline": [{"title": "Mock chapter", "summary": "Mock outline beat."}],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json({"data": [{"id": "gpt-4.1-mini"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        # e2e professional-flow step 9 以 model="mock-slow" 请求拖慢生成：
        # 章节生成太快会让 pause/resume/retry/cancel 的持久状态迁移错过轮询窗口。
        if request.get("model") == "mock-slow":
            time.sleep(2)
        role = _role_of(request)
        if role is not None:
            response_text = json.dumps(_role_payload(role, request), ensure_ascii=False)
            usage = _ROLE_USAGE
        else:
            is_review = isinstance(request.get("text"), dict)
            response_text = '{"status":"PASS","summary":"mock review","issues":[],"preserve":[],"rewrite_scope":[]}' if is_review else "Mock provider response"
            usage = {"input_tokens": 24, "output_tokens": 12, "total_tokens": 36}
        events = [
            {"type": "response.created", "id": "mock-response"},
            {"type": "response.output_text.delta", "delta": response_text},
            {"type": "response.completed", "usage": usage},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
