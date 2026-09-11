# 美团 UI 自动化技能包（meituan-ui-automation）

给 Operit（AAswordman/Operit，手机端 AI 助手）及其 Agent（大猫猫）用的标准技能包：
打开美团 → 搜索关键词 → 筛选商家 → 自动加购 → 停在"去结算"页 → 强制提醒用户人工确认支付（指纹/密码）。

**核心安全承诺：全程绝不点击任何"支付/付款"确认按钮；到达结算页后自动化立即停止。**

## 目录结构

```
meituan-ui-automation/
├── SKILL.md                     # 技能说明（Operit Skill 协议，含步骤表与安全红线）
├── README.md                    # 本文件：安装与联调指南
├── config/
│   └── skill_config.yaml        # 可配置项：包名 / 文案关键词 / 超时 / 重试
├── scripts/
│   ├── meituan_automation.py    # 入口：状态机 + CLI（一键调用）
│   ├── device_layer.py          # 设备抽象层（uiautomator2 后端 + 节点模型）
│   ├── safety_core.py           # 支付护栏 / 强制提醒 / 广告弹窗 / 定位容错
│   └── run_tests.py             # 离线单元测试（无需真机）
└── requirements.txt
```

## 安装

### 方式 A：挂载到 Operit（推荐，无障碍通道）

1. 把整个 `meituan-ui-automation` 目录放入 Operit 的 skill 目录，或在 Operit 设置 → Skill 中"直接输入添加"（粘贴本包路径/SKILL.md）。
2. 系统设置 → 无障碍 → 开启 **Operit** 的无障碍服务授权。
3. 打开 Operit 应用授权（设置内的授权系统），确认"自动化/无障碍"通道可用。
4. 之后直接对大猫猫说："打开美团搜肯德基，把香辣鸡腿堡加到购物车，先别付款"。

> 大猫猫会按 SKILL.md 步骤表用无障碍通道执行，并在每个步骤前做页面体检；任何含"支付/付款"语义的节点只会被标记、不会被点击。

### 方式 B：脚本模式（uiautomator2，适合调试与自动化联测）

```bash
# 主机侧（或 Operit 内置 Ubuntu 终端内）
pip install -U uiautomator2 PyYAML
python -m uiautomator2 init          # 向设备部署 atx-agent（会请求安装小程序，用于辅助执行）
adb connect <设备IP:端口>            # 无线调试；或用 usb 直连

# 联调
python3 scripts/meituan_automation.py --plan                            # 1) 先看动作清单
python3 scripts/meituan_automation.py --analyze                         # 2) 体检当前页面
python3 scripts/meituan_automation.py --keyword 肯德基 --product 香辣鸡腿堡 --wait-user 10
```

## 安全机制说明

1. **支付黑名单拦截（safety_core.py）**：每次点击前 `guard_click()` 校验节点文本/描述/资源 ID，命中
   `PAYMENT_BLOCK_SUBSTRINGS`（确认支付、立即支付、确认付款、去支付、提交订单……）立即抛
   `PaymentBlockError` 并退出码 90，**任何调用方不得吞掉该异常**。
2. **付款前强制提醒**：进入结算页后依次触发 ① 控制台横幅 ② 设备 Toast ③ 系统通知
   （`cmd notification post`），文案明确"请人工核对订单并手动支付（指纹/密码），脚本已停止，绝不代付"。
3. **等支付阶段只读**：`--wait-user N` 期间仅轮询屏幕文本（支付成功/已完成支付等），不产生任何点击。
4. **弹窗与定位容错**：广告弹窗只点关闭类按钮；定位异常先点"重新定位/允许"后重试；均有次数上限，超限安全停止。
5. **可审计**：`--plan` 提前打印动作清单；`--analyze` 输出页面体检 JSON；日志按行打 `[MEITUAN-UI] key=value`。

## 真机联调清单（交付前检查项）

- [ ] Operit 无障碍服务已开启（方式 A） / `adb devices` 有设备且 atx-agent 在线（方式 B）
- [ ] 美团已登录，且首页可正常搜索
- [ ] `--plan` 输出与期望一致（关键词/商家/商品/规格）
- [ ] 运行一次短流程：能进入商家页面并成功加购
- [ ] 结算页出现后，脚本输出 `status=at_checkout_waiting_user`（退出码 42）且**未**点击任何支付按钮
- [ ] 人为把结算页文案改成含"确认支付"的按钮时（模拟），护栏应拦截并退出码 90

## 常见问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 找不到搜索框/商家/按钮 | 美团版本改了 UI 文案 | 在 `config/skill_config.yaml` 中补对应文案关键词 |
| 定位弹窗反复出现 | 定位权限未授予 / 城市切换 | 检查"设置→定位"给美团授权；开启 adb 模式时确认模拟定位未开启 |
| 广告弹窗点不完 | 弹窗类型不在关闭词表 | 把弹窗上的按钮文案加入 `ad_close_texts`；或手动关一次后重跑 |
| `uiautomator2` 未安装 | 依赖缺失 | `pip install -U uiautomator2`；Operit 内置 Ubuntu 里安装后同样可用 |
| 一直停在等待状态 | 用户未完成支付 | 是预期行为；确认支付后脚本检测"支付成功"并退出 |

## 多端适配（可选）

- **扣子（Coze）机器人**：按 SKILL.md 的"执行流程步骤表"编排 Bot 工作流节点（每一步一个"无障碍点击/输入"动作 + 页面校验），安全红线逐条写进 Bot 提示词即可。
- **其他 Android 无障碍框架（AutoJS 等）**：`device_layer.py` 的 `DeviceOps` 接口就是适配点——实现同样的 `open_app/dump_nodes/click/input_text/back` 即可复用状态机与护栏。

## 边界声明

仅用于用户本人账号、本人设备的购物流程自动化，且永远止步于支付确认之前。
禁止绕过支付确认、代付、抢购刷单等场景。本技能不收集、不上传任何账号或支付信息。