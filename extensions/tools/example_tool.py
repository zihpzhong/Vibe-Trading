"""示例自定义工具 - 展示如何扩展 BaseTool。

复制此文件为你自己的工具名（如 my_indicator_tool.py），
继承 BaseTool，实现 execute() 方法即可自动注册。
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool


class ExampleExtTool(BaseTool):
    """示例扩展工具（删除或替换为真实工具）。"""

    name = "example_ext"
    description = "示例扩展工具 - 请替换为真实功能"
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "输入消息",
            }
        },
        "required": ["message"],
    }
    is_readonly = True

    @classmethod
    def check_available(cls) -> bool:
        # 返回 False 可禁用此工具
        return False  # 默认禁用示例工具

    def execute(self, **kwargs: Any) -> str:
        message = kwargs.get("message", "")
        return json.dumps(
            {"status": "ok", "echo": message},
            ensure_ascii=False,
        )
