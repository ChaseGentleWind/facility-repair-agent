"""固定话术模板：替代 generate_confirmation_stream 之类"用 LLM 写固定文本"的旧实现。

只在 nodes 里被引用。保留 generate_reply_stream（追问）走 LLM，那是唯一需要自然语言变化的场景。

设计目标：
- 输出与旧 confirmation_system_prompt 的产出格式一致，但措辞与 nodes.confirming 实际行为对齐
  （旧 prompt 写"确认后我将为您提交报修单"，但代码只生成预览 → 这里改为"确认后我将生成报修单预览"）
- 节点用 sentence_chunks() 分句切片以保留前端 SSE 体感
"""
from __future__ import annotations

from app.agent.state import TicketDraft


def _location_line(draft: TicketDraft) -> str:
    parts: list[str] = []
    if draft.estate:
        parts.append(draft.estate)
    if draft.building:
        parts.append(draft.building)
    if draft.floor:
        parts.append(draft.floor)
    if draft.area:
        parts.append(draft.area)
    elif draft.room:
        parts.append(draft.room)
    return " ".join(parts) if parts else "（暂未提供）"


def render_confirmation(draft: TicketDraft, visit_time: str | None) -> str:
    """rag_and_confirm / re_confirm 用：展示工单摘要并请求用户确认。"""
    location = _location_line(draft)
    description = draft.description or "（暂未提供）"
    visit = visit_time or "（暂未提供）"
    return (
        "好的，我来帮您确认一下报修信息：\n"
        f"  • 位置：{location}\n"
        f"  • 问题：{description}\n"
        f"  • 上门时间：{visit}\n"
        "\n以上信息是否正确？确认后我将生成报修单预览。如有现场照片，也可以现在上传补充。"
    )


def render_preview_updated(draft: TicketDraft) -> str:
    """preview_edit 修改后用：告知用户预览已更新。"""
    location = _location_line(draft)
    description = draft.description or "（暂未提供）"
    visit = draft.visit_time or "（暂未提供）"
    return (
        "已更新预览：\n"
        f"  • 位置：{location}\n"
        f"  • 问题：{description}\n"
        f"  • 上门时间：{visit}\n"
        "\n请确认或继续修改。"
    )


def render_missing_fields_prompt(missing: list[str]) -> str:
    """LLM 流式追问失败时的确定性兜底话术。"""
    labels = {
        "description": "现场具体是什么故障现象",
        "estate": "楼盘/项目名称",
        "building": "哪栋楼",
        "floor": "几楼",
        "visit_time": "希望什么时候上门",
    }
    known = [field for field in missing if field in labels]
    if not known:
        return "请再补充一下报修信息。"
    if known == ["description"]:
        return "请问现场具体是什么故障现象？"
    if known == ["visit_time"]:
        return "请问您希望什么时候上门？"

    parts = [labels[field] for field in known]
    if len(parts) == 1:
        return f"请问{parts[0]}？"
    return f"还需要补充一下{ '、'.join(parts) }。"


def sentence_chunks(text: str) -> list[str]:
    """把模板文本按"行/句"切成分块，让前端 SSE 体感保持流式。

    切分粒度：
    - 先按换行拆段
    - 段内长度 > 12 时按"，。！？"再切一次，避免一段过长
    """
    chunks: list[str] = []
    for line in text.splitlines(keepends=True):
        if not line.strip():
            chunks.append(line)
            continue
        if len(line) <= 16:
            chunks.append(line)
            continue
        buf = ""
        for ch in line:
            buf += ch
            if ch in "，。！？" and len(buf) >= 4:
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)
    return chunks
