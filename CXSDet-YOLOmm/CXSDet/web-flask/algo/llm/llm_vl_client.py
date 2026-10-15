

import os
import numpy as np
from PIL import Image
import io
import base64
import json
import cv2
import requests
from openai import OpenAI
from algo.llm.config import DEFAULT_VL_MODEL, VL_MODEL_CONFIGS


class LLMResponseError(RuntimeError):
    """Raised when a structured completion cannot safely be consumed."""


class LLMVLClient:
    """多模态大语言模型客户端（用于光伏板缺陷检测图片分析）"""

    def __init__(self, model_name: str = None):
        """
        初始化视觉模型客户端

        Args:
            model_name: 模型名称，默认为qwen-vl-plus
        """
        requested_model = model_name or DEFAULT_VL_MODEL
        self.model_name = requested_model
        config_key = requested_model if requested_model in VL_MODEL_CONFIGS else next(
            (
                key for key, value in VL_MODEL_CONFIGS.items()
                if value.get('model') == requested_model
            ),
            None
        )
        if config_key is None:
            raise ValueError(f"不支持的模型: {requested_model}")

        self.config = VL_MODEL_CONFIGS[config_key]
        self.client = OpenAI(
            api_key=self.config['api_key'],
            base_url=self.config['api_url']
        )

    @staticmethod
    def _image_data_url(image_bytes: bytes) -> str:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode != 'RGB':
            image = image.convert('RGB')
        buffered = io.BytesIO()
        image.save(buffered, format='JPEG')
        encoded = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/jpeg;base64,{encoded}"

    def review_detection_structured(self, image_bytes: bytes, detection_results: list) -> str:
        """Ask the configured VLM for a strict, machine-readable second opinion."""
        prompt = f"""
你是光伏板缺陷检测复核员。请对照图片与YOLO检测框进行第二意见复核。
YOLO结果：{json.dumps(detection_results or [], ensure_ascii=False)}

只返回一个JSON对象，不要Markdown，不要额外解释。字段必须为：
{{
  "status": "consistent | possible_false_positive | possible_missed_detection | needs_human_review",
  "summary": "简短中文结论",
  "possible_false_positives": [检测框序号],
  "possible_missed_detections": [{{"class_name":"类别","bbox":[x1,y1,x2,y2],"reason":"原因"}}],
  "attention_regions": [{{"bbox":[x1,y1,x2,y2],"reason":"人工关注原因"}}]
}}
possible_false_positives中的检测框序号必须使用YOLO结果数组从0开始的下标。
possible_missed_detections和attention_regions的bbox必须使用相对原始图片的0到1归一化坐标。
AI只能给出复核建议，不能声称已修改模型或训练标签。
        """.strip()
        completion = self.client.chat.completions.create(
            model=self.config['model'],
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._image_data_url(image_bytes)}}
                ]
            }]
        )
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise LLMResponseError("模型没有返回候选结果")

        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise LLMResponseError("模型响应被截断")
        if finish_reason == "content_filter":
            raise LLMResponseError("模型响应触发内容过滤")

        message = getattr(choice, "message", None)
        if message is None:
            raise LLMResponseError("模型没有返回消息")
        if getattr(message, "refusal", None):
            raise LLMResponseError("模型拒绝处理复核请求")

        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("模型返回空内容")
        return content

    def health_check(self) -> dict:
        """Check authentication/model visibility without returning credentials."""
        models = self.client.models.list()
        model_ids = {getattr(item, 'id', None) for item in getattr(models, 'data', [])}
        configured = self.config['model']
        return {
            'ok': True,
            'model': configured,
            'available': configured in model_ids,
        }

    def analyze_defects(self, image_bytes: bytes, detection_results: list = None) -> str:
        """
        使用多模态大语言模型分析光伏板缺陷检测图像和结果

        Args:
            image_bytes: 输入图片的字节数据
            detection_results: 检测结果数据（可选）

        Returns:
            str: 光伏板缺陷分析结果
        """
        try:
            # 读取图片
            image = Image.open(io.BytesIO(image_bytes))
            
            # 将图片转换为base64编码
            buffered = io.BytesIO()
            # 确保保存为RGB格式
            if image.mode != 'RGB':
                image = image.convert('RGB')
            image.save(buffered, format="JPEG")
            img_str = base64.b64encode(buffered.getvalue()).decode()
            img_base64_url = f"data:image/jpeg;base64,{img_str}"
            
            # 构建检测结果摘要
            detection_summary = ""
            if detection_results and len(detection_results) > 0:
                detection_summary = f"\n**检测结果摘要：**\n"
                detection_summary += f"- 检测到 {len(detection_results)} 个缺陷目标\n"
                for i, result in enumerate(detection_results[:10], 1):  # 最多显示10个
                    detection_summary += f"- 缺陷{i}: {result.get('class_name', '未知')} (置信度: {result.get('confidence', 0):.2%})\n"
                if len(detection_results) > 10:
                    detection_summary += f"- ...以及其他 {len(detection_results) - 10} 个缺陷\n"
            else:
                detection_summary = "\n**检测结果：** 未检测到明显缺陷目标\n"
            
            # 构建光伏板缺陷分析提示词
            enhanced_prompt = f"""
作为一名专业的光伏板缺陷检测分析专家，请深入分析这张光伏板图像以及对应的检测结果：

{detection_summary}

**分析要求：**

1. **光伏板整体状态评估**：
   - 分析光伏板的整体清洁度和完整性
   - 评估表面污染程度
   - 识别可见的物理损伤或缺陷

2. **缺陷严重性分级**：
   - 根据检测到的缺陷类型进行严重性分级（严重/中等/轻微）
   - 分析每种缺陷对发电效率的潜在影响
   - 指出哪些缺陷可能导致光伏板性能下降或失效

3. **缺陷成因分析**：
   - 分析各种缺陷的可能成因（环境因素、安装问题、材料老化等）
   - 识别潜在的维护疏漏
   - 评估是否为系统性问题

4. **性能影响评估**：
   - 评估这些缺陷对发电效率的影响程度
   - 预测可能的性能衰减趋势
   - 分析对整个光伏阵列的影响

5. **维护改进建议**：
   - 针对发现的问题提出具体的清洁和维护措施
   - 建议调整的运维流程
   - 推荐相应的预防性维护措施

请提供专业、详细的分析结果，重点关注缺陷对发电性能的影响和维护建议。
            """.strip()
            
            # 调用通义千问VL模型进行分析
            completion = self.client.chat.completions.create(
                model=self.config['model'],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": enhanced_prompt},
                        {"type": "image_url", "image_url": {"url": img_base64_url}}
                    ]
                }]
            )
            
            # 解析返回结果
            response = completion.choices[0].message.content
            return response
            
        except Exception as e:
            print(f"光伏板缺陷分析失败: {str(e)}")
            return f"光伏板缺陷分析失败：{str(e)}"

    def generate_improvement_suggestions(self, analysis_result: str) -> str:
        """
        根据缺陷分析结果生成改进建议

        Args:
            analysis_result: 缺陷分析结果

        Returns:
            str: 改进建议
        """
        try:
            # 构建改进建议生成提示词
            suggestion_prompt = f"""
基于以下光伏板缺陷分析结果，请作为一名资深的光伏电站运维专家，提供详细的改进建议和解决方案：

**缺陷分析结果：**
{analysis_result}

**请提供以下方面的具体建议：**

1. **立即处理措施**：
   - 针对当前光伏板的清洁或维修建议
   - 紧急维护措施
   - 风险缓解方案

2. **运维流程优化**：
   - 具体的清洁周期和方法建议
   - 巡检频次调整方案
   - 操作流程改进

3. **预防性维护**：
   - 定期检查计划
   - 环境监测建议
   - 保护措施加强

4. **长期改进计划**：
   - 系统性维护方案
   - 运维人员培训建议
   - 设备升级优化

5. **效益分析**：
   - 维护措施的投入成本估算
   - 预期发电效率提升
   - 投资回报分析

请提供具体、可操作的建议，确保每个建议都有明确的执行步骤和预期效果。
            """.strip()
            
            # 调用模型生成建议
            completion = self.client.chat.completions.create(
                model=self.config['model'],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": suggestion_prompt}
                    ]
                }]
            )
            
            # 解析返回结果
            response = completion.choices[0].message.content
            return response
            
        except Exception as e:
            print(f"改进建议生成失败: {str(e)}")
            return f"改进建议生成失败：{str(e)}"


