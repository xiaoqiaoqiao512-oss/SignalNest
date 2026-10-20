"""Deterministic, bounded plain-text rendering of already selected decisions.

This module neither evaluates rules nor chooses a delivery route. HTML and
remote media are deliberately absent; the official page remains the full text.
"""

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from signalnest.contracts import NoticeContent, WebUrl
from signalnest.mail.contracts import RENDERING_VERSION, MailError, MailItem, RenderedMail
from signalnest.notifications.contracts import Decision

_WEB_URL = TypeAdapter(WebUrl)
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
_MESSAGE_ID = re.compile(
    r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?>"
)
_KINDS = {
    "new": "新通知",
    "update": "内容更新",
    "activation_recent": "启用时的近期候选",
    "opportunity_review": "仍开放机会回顾（非新发布）",
}


def _address(value: str) -> str:
    # Match the activation addr-spec contract; headers/display names are not addresses.
    if (
        not isinstance(value, str)
        or len(value) > 254
        or not _ADDRESS.fullmatch(value)
        or ".." in value
    ):
        raise MailError("render_input_invalid")
    return value


def _time(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MailError("render_input_invalid")
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise MailError("render_input_invalid") from exc


def _visible(value: str, limit: int) -> tuple[str, bool]:
    # Flatten untrusted notice text into one visible line, including bidi controls.
    if not isinstance(value, str):
        raise MailError("render_input_invalid")
    visible = " ".join(
        "".join(
            " " if c.isspace() or unicodedata.category(c).startswith("C") else c for c in value
        ).split()
    )
    return (visible[:limit] + "…", True) if len(visible) > limit else (visible, False)


def _url(value: str) -> str:
    if not isinstance(value, str) or any(
        c.isspace() or unicodedata.category(c).startswith("C") for c in value
    ):
        raise MailError("render_input_invalid")
    try:
        _WEB_URL.validate_python(value)
    except ValidationError as exc:
        raise MailError("render_input_invalid") from exc
    # Preserve the actual frozen official URI rather than truncate/rewrite its path.
    return value


def _lines(item: MailItem) -> list[str]:
    if (
        not isinstance(item, MailItem)
        or item.kind not in _KINDS
        or not isinstance(item.content, NoticeContent)
        or not isinstance(item.decision, Decision)
        or any(
            isinstance(n, bool) or not isinstance(n, int) or n < 1
            for n in (item.event_id, item.decision_id, item.document_id)
        )
    ):
        raise MailError("render_input_invalid")
    if item.decision.content_sha256 != item.content.content_sha256():
        raise MailError("render_content_mismatch", event_id=item.event_id)
    title, _ = _visible(item.content.title, 180)
    summary, summary_cut = _visible(item.content.body_text, 400)
    evaluated = item.decision.evaluated_at.astimezone(UTC)
    occurred = _time(item.occurred_at)
    lines = [
        title,
        f"站点发布日期：{item.content.published_date.isoformat()}",
        f"事件：{_KINDS[item.kind]}；发生时间：{occurred.isoformat(timespec='seconds')}",
        f"规则判断时间：{evaluated.isoformat(timespec='seconds')}",
        f"已保存的决策：{item.decision.action.value}",
    ]
    if item.decision.needs_review:
        lines.append("【待核对】以下信息或资格尚未确认；提醒不代表已经符合报名条件。")
    lines.append("决策理由：")
    if item.decision.reasons:
        for reason in item.decision.reasons[:8]:
            text, cut = _visible(reason, 280)
            lines.append(f"- {text}" + ("［理由已截断］" if cut else ""))
        if len(item.decision.reasons) > 8:
            lines.append(f"- 另有 {len(item.decision.reasons) - 8} 条理由保存在决策记录中。")
    else:
        lines.append("- 决策记录未提供文字理由；请核对原文。")
    if item.decision.unknowns:
        lines.append("尚未确认：")
        for unknown in item.decision.unknowns[:8]:
            text, cut = _visible(unknown.message, 280)
            lines.append(f"- {text}" + ("［说明已截断］" if cut else ""))
        if len(item.decision.unknowns) > 8:
            lines.append(f"- 另有 {len(item.decision.unknowns) - 8} 项保存在决策记录中。")
    lines.append("正文摘录：")
    lines.append(summary or "正文没有可摘录的文字；图片与附件请查看原文。")
    if summary_cut:
        lines.append("［摘录已截断；完整要求、日期与数字以原文为准。］")
    if item.content.images or item.content.attachments:
        lines.append("图片与附件仅保留引用，本邮件没有下载或附带文件。")
    lines.extend((f"官方原文：{_url(item.page_url)}", ""))
    return lines


def render_mail(
    items: tuple[MailItem, ...],
    *,
    kind: Literal["immediate", "digest"],
    sender: str,
    recipient: str,
    message_id: str,
    date_at: int,
    digest_slot: int | None = None,
    part: int = 1,
) -> RenderedMail:
    """Freeze plain-text bytes from explicit inputs, without clocks or randomness."""
    if (
        not isinstance(items, tuple)
        or not items
        or len(items) > 100
        or kind not in {"immediate", "digest"}
        or (kind == "immediate" and len(items) != 1)
        or isinstance(part, bool)
        or not isinstance(part, int)
        or part < 1
        or not isinstance(message_id, str)
        or len(message_id) > 980
        or not _MESSAGE_ID.fullmatch(message_id)
        or ".." in message_id
    ):
        raise MailError("render_input_invalid")
    sender, recipient = _address(sender), _address(recipient)
    at = _time(date_at)
    # Validate all items before accessing title/decision fields in subject/grouping.
    rendered = [(item, _lines(item)) for item in items]
    lines = [
        "SignalNest 校园信息提醒",
        "以下内容来自已保存的通知与决策，不代表资格确认。",
        "事件时间保留原始记录；积压补计划不将旧事件写成刚发生。",
        "",
    ]
    if kind == "immediate":
        if digest_slot is not None:
            raise MailError("render_input_invalid")
        item = items[0]
        title, _ = _visible(item.content.title, 180)
        marker = "【待核对】" if item.decision.needs_review else ""
        subject = f"SignalNest {marker}{title}"
        lines.extend(rendered[0][1])
    else:
        if digest_slot is None:
            raise MailError("render_input_invalid")
        slot = _time(digest_slot)
        subject = f"SignalNest 信息摘要（{slot:%Y-%m-%d %H:%M UTC}，第 {part} 份）"
        lines.extend((f"摘要时段：{slot.isoformat(timespec='seconds')}；第 {part} 份", ""))
        for needs_review, heading in ((False, "常规摘要"), (True, "待核对信息")):
            selected = [
                text for item, text in rendered if item.decision.needs_review == needs_review
            ]
            if selected:
                lines.extend((f"=== {heading} ===", ""))
                for index, text in enumerate(selected, 1):
                    lines.append(f"{index}.")
                    lines.extend(text)
    body_text = "\n".join(lines).rstrip() + "\n"
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(at, usegmt=True)
    message.set_content(body_text, subtype="plain", charset="utf-8", cte="quoted-printable")
    payload = message.as_bytes()
    return RenderedMail(
        sender=sender,
        recipient=recipient,
        subject=subject,
        message_id=message_id,
        date_at=date_at,
        rendering_version=RENDERING_VERSION,
        body_text=body_text,
        payload=payload,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )
