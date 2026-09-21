---
name: jev-cua
description: 用本地 Jev 决策服务当"大脑"、cua-driver（trycua/cua）当"手"来控制 Windows 电脑：截屏、读 UIA 元素树、点击、打字、快捷键、滚动。当用户要求自动化操作本机软件/GUI（"帮我点开 xx""在记事本里输入 xx""操作这个窗口"）、提到 cua / computer use / 控制电脑 / jev 操作，或要让 jev 驱动桌面动作时使用。
---

# JEV + CUA 电脑控制

Jev（本地决策服务）作为 System-1 提议器，你（ZCode）作为 System-2 验证者和兜底决策者，
cua-driver 负责执行（UIA 元素级后台操作，不抢焦点）。分工：**jev 提议 → 你核实 → 执行 → 重新观察**。

## 前置

先确保守护进程（脚本自动用绝对路径调 cua-driver，无需 PATH）：

```bash
python ~/.agents/skills/jev-cua/scripts/jev_cua.py daemon
```

## 核心循环（每个动作四步）

```bash
# 1) 观察：找窗口
python ~/.agents/skills/jev-cua/scripts/jev_cua.py windows
# 2) 快照：元素树 + 截图（element_index 和 snapshot_id 以这次快照为准）
python ~/.agents/skills/jev-cua/scripts/jev_cua.py state --pid PID --window-id WID --out /tmp/cua
# 3) 决策：jev 提议"下一步动作 + 目标元素"（附完整分布）
#    中文目标务必走文件或 stdin（Windows argv 会把中文打成 GBK 乱码）
python ~/.agents/skills/jev-cua/scripts/jev_cua.py decide --goal-file goal.txt --state-file /tmp/cua/state_PID.json
# 4) 执行：优先 element_index（后台 UIA 送达）；然后回到 2) 重新快照验证
python ~/.agents/skills/jev-cua/scripts/jev_cua.py act click --pid PID --window-id WID --element 5
```

其他动作：`act type --text "..."`（先确保焦点在输入区）、`act key --key enter`、
`act hotkey --keys ctrl+s`、`act scroll --direction down`；
像素兜底 `act click --x X --y Y`（窗口内截图像素坐标，先 zoom 确认）。
不确定工具参数时直接 `cua-driver describe <tool>` 查 schema。

## 决策规则（必须遵守）

- **快照后必须先读截图**（state 输出的 `screenshot` 路径，用 Read 看）并与元素树互相印证——
  官方明确说"the tree lies on some surfaces"，两边对不上时以截图为准。
- `decide` 输出 `low_confidence: true`（置信度 < 0.6）时：**由你自己根据截图和元素树决定动作**，
  jev 分布只作参考，并在汇报时说明是本地模型置信不足。动作被你推翻时，
  `target_distribution` 的元素排名通常仍可参考（元素标签匹配是本地模型的强项）。
- **不可逆/破坏性动作**（删除文件、发送消息、支付、关机、覆盖保存）必须先问用户，jev 提议也不能豁免。
- 永远不要让脚本输入密码/密钥等机密；输入前和用户确认要输入的内容。
- `background_unavailable` 报错后才允许 `--foreground` 重试同一动作（前台送达会抢用户焦点）。
- 动作可能异步生效：每次执行后重新 `state` 验证，不要凭返回值断言成功（返回里 `effect` 常是 `unverifiable`）。
- 一个窗口一轮内连续元素动作前必须重新 `state`（element_index 缓存被新快照整体替换）。

## 后端选择

- 默认 `http://127.0.0.1:8767`：**本地 jevy 蒸馏 student**（`intent_server.py`，
  契约与 NanoJev/Laya 兼容），服务没起时脚本自动拉起（首次加载模型几十秒）
- `--endpoint http://127.0.0.1:8766`：Laya（421M 客服域微调）
- `--endpoint http://127.0.0.1:8765`：NanoJev（unified-games-v1，**游戏域蒸馏，GUI/文本判断属分布外**，仅当快速演示用）
- 官方云 API：`--endpoint https://api.typesafe.ai/v1/systemone --api-key $TYPESAFE_API_KEY`（jev-1.13.0）

自动拉起需要环境变量指向各后端服务目录（已起着的服务不受影响）：
`JEV_STUDENT_DIR`（含 intent_server.py）、`JEV_LAYA_DIR`（含 laya_server.py）、
`JEV_NANOJEV_DIR`（NanoJev 仓库根）。cua-driver.exe 默认从
`%LOCALAPPDATA%\Programs\Cua\cua-driver\bin` 探测，可用 `CUA_DRIVER_EXE` 覆盖。

本地 student/Laya 对"按钮/菜单中文标签匹配"类决策是强项，但都不是 GUI 专家：
关键步骤仍以你读截图的判断为准，jev 分布只做提议。

## 汇报规则

- 每步汇报：jev 提议（动作+目标+关键概率）→ 你的核实结论 → 实际效果。
- `low_confidence` 时明确说"本地模型置信不足，本次由我判断"。
- 目标完成后给出最终验证证据（快照里元素/窗口标题的变化），不要只说"应该好了"。
