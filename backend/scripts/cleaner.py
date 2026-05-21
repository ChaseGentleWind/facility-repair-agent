"""
离线数据清洗模块：调用 DeepSeek-V3 对历史工单逐条标准化。
输出 cleaned_tickets.json 供后续向量化入库。
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
你是一个企业设施维保的数据清洗专家。你需要分析一条历史工单记录，判断它是否为有效的"用户原始报修描述"，并将其标准化。

【数据过滤规则 - 遇到以下情况，判定为无效 (is_valid: false)】：
1. 维修人员日志：包含"已处理"、"已沟通"、"上门无人"、"配件已申领"等售后跟进话术。
2. 隐私与无关信息：纯电话号码、人名，或类似"来一下"、"测试单"等无意义的模糊词汇。
3. 非用户侧描述：明显是后台客服或系统自动生成的系统级备注。

【数据标准化规则 - 如果有效 (is_valid: true)】：
1. 结合提供的"楼栋单元"和"故障类型"上下文，理解真实的故障意图。
2. 彻底剔除位置信息（如1102室、茶水间、T7栋等），因为我们要提取的是纯粹的故障通用模式。
3. 严格输出"物理实体 + 故障现象"的短句（例如："门锁损坏，需要更换锁芯"）。

【输出格式要求】：必须严格输出 JSON 格式：
{
  "is_valid": true/false,
  "normalized_text": "清洗后的标准描述（如果is_valid为false，此项留空）",
  "reason": "简述判断有效/无效的理由"
}"""


def _build_user_prompt(building_unit: str, fault_type: str, description: str) -> str:
    return (
        f"楼栋单元：{building_unit}\n"
        f"故障类型：{fault_type}\n"
        f"原始描述：{description}"
    )


async def _clean_one(
    client: AsyncOpenAI,
    model: str,
    row: dict[str, str],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """清洗单条记录，返回有效结果或 None。"""
    description = row.get("报修描述", "").strip()
    if not description or description == "(null)":
        return None

    building_unit = row.get("楼栋单元号", "")
    fault_type = row.get("故障类型", "")
    priority = row.get("报修优先级", "MEDIUM")
    repair_type = row.get("报修类型", "")

    user_prompt = _build_user_prompt(building_unit, fault_type, description)

    async with semaphore:
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            content = resp.choices[0].message.content
            result = json.loads(content)
        except Exception as exc:
            logger.warning("清洗失败 [%s]: %s", description[:30], exc)
            return None

    if not result.get("is_valid"):
        return None

    normalized = result.get("normalized_text", "").strip()
    if not normalized:
        return None

    # 解析 fault_type JSON
    ft_code, ft_name = "000", "未分类"
    try:
        ft = json.loads(fault_type) if isinstance(fault_type, str) and fault_type.startswith("{") else {}
        ft_code = ft.get("code", "000")
        ft_name = ft.get("displayName", "未分类")
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "normalized_text": normalized,
        "fault_type_code": ft_code,
        "fault_type_name": ft_name,
        "repair_priority": priority,
        "repair_type": repair_type,
        "raw_description": description,
    }


async def clean_csv(
    csv_path: Path,
    output_path: Path,
    api_key: str,
    base_url: str,
    model: str,
    concurrency: int = 15,
) -> list[dict]:
    """
    读取 CSV，并发调用 DeepSeek 清洗，输出有效记录到 JSON 文件。
    返回有效记录列表。
    """
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    semaphore = asyncio.Semaphore(concurrency)

    rows: list[dict[str, str]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    total = len(rows)
    logger.info("读取 %d 条原始记录，开始清洗...", total)

    tasks = [_clean_one(client, model, row, semaphore) for row in rows]

    results: list[dict] = []
    done_count = 0
    for coro in asyncio.as_completed(tasks):
        result = await coro
        done_count += 1
        if result:
            results.append(result)
        if done_count % 100 == 0:
            logger.info("进度: %d/%d, 有效: %d", done_count, total, len(results))

    logger.info("清洗完成: 总计 %d 条, 有效 %d 条, 过滤 %d 条", total, len(results), total - len(results))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("结果已保存到 %s", output_path)
    return results
