# Vibe-Trading 扩展与优化指南

> 更新时间：2026-05-08  
> 适用分支：`dev`

---

## 核心原则

- **不修改上游文件**（除 `.gitignore` 和 `ext_bridge.py`）
- **所有自定义代码**放在 `extensions/` 目录或项目外的 `~/.vibe-trading/`
- **定期 rebase** 而非 merge，保持线性历史，冲突概率最低

---

## 分支结构

```
main    ← 纯净跟踪上游（只执行 git pull，不做任何修改）
dev     ← 个人工作分支（所有扩展和优化在此分支）
```

---

## 目录结构

```
Vibe-Trading/
├── extensions/                          ← 自定义代码根目录（上游不存在）
│   ├── tools/                           # 自定义 Agent 工具
│   │   ├── __init__.py
│   │   ├── example_tool.py              # 示例工具（可删除）
│   │   └── 你的工具.py
│   └── config/
│       ├── .env.local.example           # 配置模板
│       └── .env.local                   # 本地配置（已 gitignore）
│
├── agent/src/tools/
│   └── ext_bridge.py                    ← 唯一加入上游目录的桥接文件
│
~/.vibe-trading/skills/user/             ← 自定义技能（项目外，官方支持）
    └── 你的技能/
        └── SKILL.md
```

---

## 扩展点速查

| 扩展类型 | 存放位置 | 冲突风险 | 说明 |
|---------|---------|---------|------|
| 自定义 Skill | `~/.vibe-trading/skills/user/<name>/SKILL.md` | 无 | 官方原生支持，优先级高于内置 |
| 自定义 Tool | `extensions/tools/<name>.py` | 无 | ext_bridge 自动发现注册 |
| 本地配置覆盖 | `extensions/config/.env.local` | 无 | gitignore 保护，不提交 |
| 前端扩展 | `extensions/frontend/`（按需创建） | 极低 | 需同步修改 vite 配置 |

---

## 如何添加自定义工具（Tool）

1. 在 `extensions/tools/` 创建新文件，继承 `BaseTool`：

```python
# extensions/tools/my_tool.py
from __future__ import annotations
import json
from typing import Any
from src.agent.tools import BaseTool

class MyTool(BaseTool):
    name = "my_tool"                    # 工具唯一标识（LLM 调用时使用）
    description = "工具功能描述"
    parameters = {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "参数说明"},
        },
        "required": ["param1"],
    }
    is_readonly = True                  # 只读工具设为 True，写操作设为 False

    @classmethod
    def check_available(cls) -> bool:
        # 检查依赖是否满足，返回 False 则此工具不注册
        return True

    def execute(self, **kwargs: Any) -> str:
        param1 = kwargs.get("param1", "")
        result = {"status": "ok", "data": param1}
        return json.dumps(result, ensure_ascii=False)
```

2. **无需注册**，`ext_bridge.py` 在服务启动时自动发现并加载。

---

## 如何添加自定义技能（Skill）

1. 创建技能目录和 SKILL.md：

```bash
mkdir -p ~/.vibe-trading/skills/user/my-skill
```

2. 编写 `SKILL.md`，遵循 frontmatter 格式：

```markdown
---
name: my-skill
description: 技能一句话描述（显示在 Agent 技能列表中）
category: analysis   # data-source | strategy | analysis | asset-class | crypto | tool | other
---

# My Skill 使用指南

## 功能说明
...

## 使用示例
...
```

3. 系统自动加载，用户技能**优先级高于**同名内置技能（可用于覆盖/增强内置技能）。

---

## 如何覆盖本地配置

```bash
cp extensions/config/.env.local.example extensions/config/.env.local
# 编辑 .env.local，填写真实的 API Key 等配置
```

`.env.local` 已加入 `.gitignore`，不会被提交。

---

## 日常 Git 操作

### 远程仓库说明

| 远程名 | 地址 | 用途 |
|--------|------|------|
| `origin` | `https://github.com/zihpzhong/Vibe-Trading.git` | 个人 fork（推送目标） |
| `upstream` | `https://github.com/HKUDS/Vibe-Trading.git` | 上游原仓库（只拉取）|

---

### 日常开发流程

```bash
# 1. 确认在 dev 分支
git checkout dev

# 2. 开发、修改文件...

# 3. 提交
git add <文件>
git commit -m "feat[ext]: adds ..."

# 4. 推送到自己的 fork
git push origin dev
```

---

### 同步上游最新代码

```bash
# Step 1：拉取上游最新到 main
git checkout main
git pull upstream main

# Step 2：同步到自己 fork 的 main
git push origin main

# Step 3：切回 dev，rebase 到最新 main
git checkout dev
git rebase main

# Step 4：如有冲突（极少发生），解决后继续
# git add <冲突文件>
# git rebase --continue

# Step 5：强制推送 dev（rebase 后需要）
git push origin dev --force-with-lease
```

---

### 冲突概率分析

| 文件/目录 | 冲突概率 | 原因 |
|---------|---------|------|
| `extensions/` | **0%** | 上游不存在此目录 |
| `~/.vibe-trading/` | **0%** | 在项目目录外 |
| `ext_bridge.py` | **<1%** | 上游不会创建此同名文件 |
| `.gitignore` | **偶发** | 双方都可能追加条目，保留双方内容即可 |

---

### 查看状态

```bash
# 查看当前分支和远程
git branch -a
git remote -v

# 查看 dev 比 main 多了哪些提交
git log main..dev --oneline

# 查看上游有哪些新提交（未合并）
git fetch upstream
git log main..upstream/main --oneline
```

---

### 撤销操作

```bash
# 撤销最后一次提交（保留文件改动）
git reset --soft HEAD~1

# 丢弃工作区改动
git checkout -- <文件>

# rebase 出错时中止
git rebase --abort
```

---

## ext_bridge.py 工作原理

项目工具注册机制（`src/tools/__init__.py`）通过 `pkgutil.iter_modules` 自动发现 `src/tools/` 下所有模块并导入，再通过 `BaseTool.__subclasses__()` 收集所有子类注册进 `ToolRegistry`。

`ext_bridge.py` 利用这个机制：被自动导入后，它再去动态 import `extensions/tools/` 下的所有模块，从而让自定义工具也进入 `BaseTool.__subclasses__()` 的发现范围，完成无侵入注册。

```
服务启动
  └─ _discover_subclasses()
       └─ 导入 src/tools/ext_bridge.py       ← 自动
            └─ 导入 extensions/tools/*.py    ← ext_bridge 触发
                 └─ 自定义 BaseTool 子类注册  ← 自动
```

---

## 提交规范

遵循 Conventional Commits，自定义提交加 `[ext]` 标记便于区分：

```
feat[ext]: adds xxx tool for ...
fix[ext]: resolves issue in ...
chore[ext]: updates extension dependencies
```

---

## 注意事项

1. **不要直接修改上游文件**（`agent/src/` 内的原有文件），否则 rebase 时会产生冲突
2. 需要修改上游行为时，优先考虑：技能覆盖 → 工具扩展 → 包装/代理模式
3. `ext_bridge.py` 是唯一例外，它在上游目录内但由我们创建，rebase 冲突风险极低
4. 提交前确认当前在 `dev` 分支：`git branch --show-current`
