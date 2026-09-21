# jev-cua

用本地 [Jev](https://github.com/slatinwine/jevy) 决策服务当"大脑"、[trycua/cua](https://github.com/trycua/cua) 的 cua-driver 当"手"，在 Windows 上实现**观察 → 决策 → 执行 → 验证**的电脑控制循环。以 [ZCode](https://zcode.ai) skill 形式分发（也兼容同类 agent skill 规范）。

**分工**：Jev（本地小模型，System-1）对"下一步动作"和"目标元素"两级打分，输出完整概率分布；agent（System-2）对照截图与 UIA 元素树核实后执行，置信不足（<0.6）时接管决策。

## 工作原理

1. **观察**：`cua-driver call get_window_state` 一次拿到窗口的 UIA 元素树（element_index / role / label / 可用动作）+ 窗口截图（base64 PNG）。
2. **决策**：把目标 + 元素清单作为 `state` 发给 Jev 的 `POST /api/v1/decide`（choice 题），两级打分：
   - 动作层：click / type_text / press_key / hotkey / scroll / done
   - 目标层：从可操作元素中选出该点哪个（元素标签匹配，小模型的强项）
3. **执行**：优先 `element_index + snapshot_id`（UIA 后台送达，不抢焦点）；像素点击仅作兜底。驱动报 `background_unavailable` 后才允许升级 foreground。
4. **验证**：动作可能异步生效——重新拉快照确认，元素索引缓存随新快照整体替换。

## 安装

```powershell
# 1) cua-driver（Windows；需 Windows 10/11）
irm https://cua.ai/driver/install.ps1 | iex
cua-driver autostart kick   # 启动守护进程（或设置登录自启）

# 2) 本 skill 放到 agent 的 skills 目录
#    ZCode: C:\Users\<你>\.agents\skills\jev-cua\
```

## 配置

| 环境变量 | 作用 |
|---|---|
| `CUA_DRIVER_EXE` | 覆盖 cua-driver.exe 路径（默认探测 `%LOCALAPPDATA%\Programs\Cua\cua-driver\bin`） |
| `JEV_STUDENT_DIR` | jev 蒸馏 student 服务目录（含 `intent_server.py`，端口 8767）→ 支持自动拉起 |
| `JEV_LAYA_DIR` | Laya 服务目录（`laya_server.py`，端口 8766）→ 支持自动拉起 |
| `JEV_NANOJEV_DIR` | NanoJev 仓库根（端口 8765）→ 支持自动拉起 |

本地后端没配环境变量也能用——只要服务已在跑。云端官方 API 无需任何本地服务：
`--endpoint https://api.typesafe.ai/v1/systemone --api-key $TYPESAFE_API_KEY`。

## 用法

```bash
S=~/.agents/skills/jev-cua/scripts/jev_cua.py

python $S daemon                 # 确保 cua-driver 守护进程在运行
python $S windows                # 列出可见窗口（pid / window_id / 标题）
python $S state --pid PID --window-id WID --out /tmp/cua   # 元素树 + 截图
python $S decide --goal-file goal.txt --state-file /tmp/cua/state_PID.json
python $S act click --pid PID --window-id WID --element 5
python $S act type --pid PID --window-id WID --text "hello"
python $S act key --pid PID --window-id WID --key escape
python $S act hotkey --pid PID --window-id WID --keys ctrl+s
python $S act scroll --pid PID --window-id WID --direction down
```

注意：

- **中文目标走 `--goal-file`（UTF-8）或 `--goal-stdin`**，不要放 argv——Windows 会按 GBK 打碎。
- `decide` 输出 `low_confidence: true` 时表示本地模型置信不足，应由调用方（agent）根据截图自行决策。
- 元素索引缓存按 (pid, window_id) 组织，**每次动作后必须重新 `state` 再取新索引**。

## 安全规则（内置在 SKILL.md，供 agent 遵循）

- 不可逆/破坏性动作（删除、发送、支付、关机）必须先征得用户同意，模型提议不可豁免
- 永远不输入密码/密钥等机密
- 前台送达（抢焦点）只作为 `background_unavailable` 之后的升级手段

## 相关仓库

- [slatinwine/jevy](https://github.com/slatinwine/jevy) — Jev 判决模型框架（训练/蒸馏/API）
- [trycua/cua](https://github.com/trycua/cua) — cua-driver（MIT）

## License

MIT
