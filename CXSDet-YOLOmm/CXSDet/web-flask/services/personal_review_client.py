"""把本人的模型连接适配成旧复核服务要的结构化核查协议（INT-06）。

旧页面（单条/批量/健康检查）与 Agent 的 ``review.ai`` 都通过这里，
内部只用 ``agent.connections.model_client(actor)`` 那一份凭据——
**不存在第二份全局 Key**，也不回退到 ``algo.llm.config`` 的本地配置。

协议与 ``algo.llm.llm_vl_client.LLMVLClient`` 保持一致，但多两个可选入参：
``class_names`` 与 ``secondary_image_bytes``（真实的第二模态图）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

from agent.errors import AgentError
from agent.identity import ActorContext

LOGGER = logging.getLogger(__name__)

PROMPT = """你是光伏板缺陷检测复核员。请对照图片与 YOLO 检测框给出第二意见。
YOLO 结果：{detections}
类别表：{class_names}
{modality_note}
只返回一个 JSON 对象，不要 Markdown，不要额外解释。字段必须为：
{{
  "status": "consistent | possible_false_positive | possible_missed_detection | needs_human_review",
  "summary": "简短中文结论",
  "possible_false_positives": [检测框序号],
  "possible_missed_detections": [{{"class_name":"类别","bbox":[x1,y1,x2,y2],"reason":"原因"}}],
  "attention_regions": [{{"bbox":[x1,y1,x2,y2],"reason":"人工关注原因"}}]
}}
possible_false_positives 中的序号必须使用 YOLO 结果数组从 0 开始的下标。
bbox 必须使用相对原始图片的 0 到 1 归一化坐标。
AI 只能给出复核建议，不能声称已修改模型或训练标签。
"""


class PersonalReviewClient:
    """本人连接上的结构化视觉核查；不持有、也不读取任何全局密钥。"""

    def __init__(self, gateway, *, actor: Optional[ActorContext] = None) -> None:
        self.gateway = gateway
        self.actor = actor
        model = getattr(gateway.credentials, "model", "") or "unknown"
        self.model_name = str(model)
        self.config = {"model": self.model_name}

    # -- 协议 ---------------------------------------------------------------
    def review_detection_structured(
        self,
        image_bytes: bytes,
        detection_results: Sequence[Any],
        *,
        class_names: Optional[Sequence[str]] = None,
        secondary_image_bytes: Optional[bytes] = None,
        secondary_modality: Optional[str] = None,
    ) -> str:
        if not image_bytes:
            raise ValueError("缺少待复核的 RGB 图片")
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": PROMPT.format(
                    detections=json.dumps(list(detection_results or []), ensure_ascii=False),
                    class_names=json.dumps(list(class_names or []), ensure_ascii=False),
                    modality_note=(
                        f"第二张图是同一位置的{secondary_modality or '红外'}配图，"
                        "请结合两张图判断。"
                        if secondary_image_bytes
                        else "本记录只有 RGB 一张图。"
                    ),
                ),
            },
            {"type": "image_url", "image_url": {"url": _data_url(image_bytes)}},
        ]
        if secondary_image_bytes:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(secondary_image_bytes)}}
            )
        turn = self.gateway.complete(
            [{"role": "user", "content": content}], temperature=0.1
        )
        return str(getattr(turn, "text", "") or "")

    def health_check(self) -> dict[str, Any]:
        """认证 + 模型可见性；仅认证成功不代表能看图，能力另由 probe 负责。"""
        client = self.gateway._get_client()  # 与聊天共用同一个 SDK 客户端
        models = client.models.list()
        model_ids = {getattr(item, "id", None) for item in getattr(models, "data", [])}
        return {
            "ok": True,
            "model": self.model_name,
            "available": self.model_name in model_ids,
        }


def _data_url(image_bytes: bytes) -> str:
    import base64
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffered.getvalue()).decode()


def create_personal_review_client(
    actor: ActorContext, *, verify_vision: bool = True
) -> PersonalReviewClient:
    """服务端入口：按当前用户构造复核客户端。

    未配置连接 → ``AgentError(MODEL_NOT_CONFIGURED)``，提示去设置里填；
    连接存在但「看图」能力未通过检查 → ``MODEL_CAPABILITY_MISSING``。
    两者都是**阻断**，不是回退。
    """
    from agent import connections

    gateway = connections.model_client(actor)
    if verify_vision:
        connections.require_capability(actor, "vision")
    return PersonalReviewClient(gateway, actor=actor)
