"""美团 UI 自动化主入口：状态机 + CLI（一键调用）。

用法示例：
  python3 meituan_automation.py --keyword 肯德基 --product 香辣鸡腿堡 --wait-user 10
  python3 meituan_automation.py --keyword 奶茶 --merchant 一点点 \\
      --product "杨枝甘露,波霸奶茶" --spec 中杯
  python3 meituan_automation.py --analyze     # 只读页面体检（Agent 决策用）
  python3 meituan_automation.py --plan --product 香辣鸡腿堡   # 预览动作清单

安全设计（safety_core.py）：
- 点击前强制过 guard_click 支付黑名单校验；
- 到达"去结算"页即停止并强制提醒，等待用户人工指纹/密码支付；
- 广告弹窗/定位漂移按有限次数容错，超限安全停止（退出码 91）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from device_layer import UiNode, U2Device
from safety_core import (
    EXIT_OK,
    EXIT_PAYMENT_BLOCKED,
    EXIT_SAFETY_STOP,
    EXIT_WAITING_USER,
    PaymentBlockError,
    SafetyRules,
    analyze_screen,
    show_payment_reminder,
    wait_for_manual_payment,
)

DEFAULT_CONFIG = {
    "package": "com.sankuai.meituan",
    "search_box_rid": "",
    "wait_stable_sec": 1.0,
    "timeout_node_sec": 8,
    "max_dismiss_popups": 3,
    "max_location_retries": 2,
    "wait_user_min": 10,
}


class Config:
    """配置加载：yaml/json 兜底 -> 与内置默认值合并。"""

    def __init__(self, path: Optional[str] = None):
        self.data: Dict[str, Any] = dict(DEFAULT_CONFIG)
        if path and os.path.exists(path):
            loaded = self._load(path)
            if loaded:
                self._merge(loaded, self.data)

    @staticmethod
    def _load(path: str) -> dict:
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
        except ImportError:
            print("[MEITUAN-UI] warn=pyyaml 未安装，跳过配置文件（仍可用命令行参数与内置默认）")
            return {}
        except Exception as exc:  # noqa: BLE001
            print(f"[MEITUAN-UI] warn=配置文件读取失败({exc})，使用内置默认")
            return {}

    @staticmethod
    def _merge(src: dict, dst: dict) -> None:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                Config._merge(value, dst[key])
            else:
                dst[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class StepError(RuntimeError):
    pass


class MeituanFlow:
    """美团自动化状态机：启动 -> 搜索 -> 筛商 -> 加购 -> 去结算 -> 等待人工支付。"""

    def __init__(self, device, cfg: Config, rules: SafetyRules,
                 keyword: str, merchant: Optional[str], products: List[str],
                 spec: Optional[str] = None):
        self.device = device
        self.cfg = cfg
        self.rules = rules
        self.keyword = keyword
        self.merchant = merchant or keyword
        self.products = products
        self.spec = spec
        self._dismiss_left = int(cfg.get("max_dismiss_popups", 3))
        self._loc_left = int(cfg.get("max_location_retries", 2))
        self._stable = float(cfg.get("wait_stable_sec", 1.0))
        self._timeout = float(cfg.get("timeout_node_sec", 8))

    # ---------- 基础工具 ----------
    def _report(self, key: str, value) -> None:
        print(f"[MEITUAN-UI] {key}={value}")

    def _wait_stable(self) -> None:
        time.sleep(self._stable)

    def _snapshot(self) -> List[UiNode]:
        """带容错的页面快照：先处理定位异常，再处理广告弹窗，返回稳定节点列表。"""
        last: List[UiNode] = []
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            try:
                nodes = self.device.dump_nodes()
                last = nodes
            except Exception:  # noqa: BLE001
                nodes = last
            if not nodes:
                time.sleep(0.5)
                continue
            if self.rules.is_location_drift(nodes) and self._loc_left > 0:
                act = self.rules.find_location_action(nodes)
                if act is not None:
                    self.rules.guard_click(act, "fix-location")
                    self.device.click(act)
                    self._loc_left -= 1
                    self._report("info", "已处理定位异常")
                    time.sleep(self._stable)
                    continue
            if self.rules.is_ad_dialog(nodes) and self._dismiss_left > 0:
                close = self.rules.find_close_button(nodes)
                if close is not None:
                    self.rules.guard_click(close, "dismiss-ad")
                    self.device.click(close)
                    self._dismiss_left -= 1
                    self._report("info", "已跳过广告弹窗")
                    time.sleep(self._stable)
                    continue
            return nodes
        return last

    def _find_first(self, keywords: Tuple[str, ...], what: str, must: bool = True,
                    exact: bool = False) -> Optional[UiNode]:
        """按文案找节点；优先命中可点击节点，无则退回首个文本命中。"""
        fallback: Optional[UiNode] = None
        for node in self._snapshot():
            t = node.text.strip()
            hit = (t in keywords) if exact else any(k and k in t for k in keywords)
            if hit:
                if node.clickable:
                    return node
                if fallback is None:
                    fallback = node
        if fallback is not None:
            return fallback
        if must:
            raise StepError(f"未找到目标节点: {what} (关键词={keywords})")
        return None

    def _find_label(self, keyword: str, what: str, must: bool = True) -> Optional[UiNode]:
        """按"文本或描述包含关键词"找节点。"""
        for node in self._snapshot():
            if node.has(keyword):
                return node
        if must:
            raise StepError(f"未找到目标节点: {what} (关键词={keyword})")
        return None

    def _find_rid(self, rid: str, what: str) -> UiNode:
        for node in self._snapshot():
            if node.resource_id == rid:
                return node
        raise StepError(f"未找到控件ID: {what} ({rid})")

    def _click(self, node: UiNode, ctx: str) -> None:
        self.rules.guard_click(node, ctx)
        self.device.click(node)
        self._wait_stable()

    # ---------- 流程步骤 ----------
    def step_open_app(self) -> None:
        pkg = str(self.cfg.get("package", "com.sankuai.meituan"))
        self.device.open_app(pkg)
        time.sleep(self._stable * 2)

    def step_search(self) -> None:
        rid = str(self.cfg.get("search_box_rid", "") or "")
        if rid:
            box = self._find_rid(rid, "首页搜索框")
        else:
            hints = tuple(self.cfg.get("search_box_hints", ["搜索"]))
            box = self._find_first(hints, "首页搜索框")
            assert box is not None, "首页搜索框查找失败"
        self.device.input_text(box, self.keyword)
        self._wait_stable()
        confirm = str(self.cfg.get("search_confirm", "搜索"))
        btn = self._find_first((confirm,), "搜索确认按钮", must=False)
        if btn is not None:
            self._click(btn, "search-confirm")
        else:
            self.device.press_key("enter")
            self._wait_stable()
        self._report("step", "search_done")

    def step_filter_merchant(self) -> None:
        tab = str(self.cfg.get("merchant_tab", "商家"))
        node = self._find_first((tab,), "商家筛选Tab", must=False)
        if node is not None:
            self._click(node, "filter-merchant-tab")
        shop = self._find_label(self.merchant, "目标商家")
        assert shop is not None, "目标商家查找失败"
        self._click(shop, "open-merchant")
        self._report("step", "merchant_selected")

    def _add_one(self, product: str) -> None:
        item = self._find_label(product, f"商品[{product}]")
        assert item is not None, f"商品[{product}]查找失败"
        self._click(item, f"open-product:{product}")
        spec_btn = self._find_first(
            tuple(self.cfg.get("spec_pick_texts", ["选规格"])),
            "选规格入口", must=False)
        if spec_btn is not None:
            confirms = tuple(self.cfg.get("spec_confirm_texts", ["确定", "完成", "加入购物车"]))
            if self.spec:
                opt = self._find_label(self.spec, f"规格[{self.spec}]")
                assert opt is not None, f"规格[{self.spec}]查找失败"
                self._click(opt, f"pick-spec:{self.spec}")
            conf = self._find_first(confirms, "规格确认按钮")
            assert conf is not None, "规格确认按钮查找失败"
            self._click(conf, "confirm-spec")
        add = self._find_first(
            tuple(self.cfg.get("add_to_cart_texts", ["加入购物车", "加购"])),
            f"加购按钮[{product}]")
        assert add is not None, f"加购按钮[{product}]查找失败"
        self._click(add, f"add-to-cart:{product}")
        self._report("step", f"added:{product}")

    def step_add_products(self) -> None:
        for product in self.products:
            self._add_one(product)

    def step_go_checkout(self) -> None:
        cart_entries = tuple(self.cfg.get("cart_entry_texts", ["购物车"]))
        cart = self._find_first(cart_entries, "购物车入口")
        assert cart is not None, "购物车入口查找失败"
        self._click(cart, "open-cart")
        checkout_btns = tuple(self.cfg.get("checkout_button_texts", ["去结算"]))
        checkout = self._find_first(checkout_btns, "去结算按钮")
        assert checkout is not None, "去结算按钮查找失败"
        self._click(checkout, "go-checkout")
        self._report("step", "checkout_clicked")

    def step_verify_checkout(self) -> bool:
        landmarks = tuple(
            self.cfg.get("checkout_landmarks",
                         ["提交订单", "确认支付", "收银台", "订单金额", "支付方式"]))
        joined = self._snapshot_joined()
        if not any(k in joined for k in landmarks):
            time.sleep(self._stable * 2)  # 结算页可能异步加载，再等一轮
            joined = self._snapshot_joined()
        ok = any(k in joined for k in landmarks)
        self._report("at_checkout", "true" if ok else "false")
        return ok

    def _snapshot_joined(self) -> str:
        nodes = self._snapshot()
        return " ".join(n.text + " " + n.desc for n in nodes)

    # ---------- 主流程 ----------
    def run(self, wait_user_min: float) -> int:
        self._report("keyword", self.keyword)
        self._report("merchant", self.merchant)
        self._report("products", ",".join(self.products))
        self.step_open_app()
        self.step_search()
        self.step_filter_merchant()
        self.step_add_products()
        self.step_go_checkout()
        if not self.step_verify_checkout():
            raise StepError("未检测到结算页标志，已安全停止（请人工检查界面）")
        self._report("status", "at_checkout_waiting_user")
        show_payment_reminder(self.device, ", ".join(self.products))
        if wait_user_min > 0:
            wait_for_manual_payment(self.device, timeout_min=wait_user_min,
                                    rules=self.rules)
        return EXIT_WAITING_USER


def print_plan(cfg: Config, args: argparse.Namespace) -> None:
    keyword = args.keyword or "肯德基"
    merchant = args.merchant or keyword
    products = [p.strip() for p in args.product.split(",") if p.strip()]
    spec = args.spec
    lines = [
        f"1. open_app   {cfg.get('package', 'com.sankuai.meituan')}",
        f"2. search     '{keyword}' (搜索框: {cfg.get('search_box_hints') or '搜索商家、地点或关键词'})",
        f"3. filter     '商家' Tab -> 点击商家 '{merchant}'",
    ]
    for p in products:
        extra = f" -> 规格 '{spec}'" if spec else ""
        lines.append(f"4. add        '{p}'{extra} -> 加入购物车")
    lines.append("5. cart       打开购物车 -> 点击 '去结算'")
    lines.append("6. verify     校验结算页标志 -> 强制提醒 -> 等待人工指纹/密码支付")
    lines.append("7. stop       绝不点击任何支付/付款按钮")
    print("[MEITUAN-UI] plan 动作清单（不连接设备）:")
    print("\n".join(f"  {line}" for line in lines))
    print("[MEITUAN-UI] status=ok")


def cmd_analyze(device) -> int:
    nodes = device.dump_nodes()
    rules = SafetyRules(Config())
    report = analyze_screen(nodes, rules)
    print("[MEITUAN-UI] analyze " + json.dumps(report, ensure_ascii=False))
    return EXIT_OK


def build_device(args: argparse.Namespace) -> U2Device:
    if args.device == "u2":
        return U2Device(addr=args.addr)
    raise StepError(f"未知设备后端: {args.device}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="美团 UI 自动化（停在去结算页，等待用户人工确认支付）")
    parser.add_argument("--keyword", default=None, help="搜索关键词，如 肯德基/奶茶")
    parser.add_argument("--merchant", default=None, help="目标商家名（默认=关键词）")
    parser.add_argument("--product", default=None, help="商品名，多个用英文逗号分隔")
    parser.add_argument("--spec", default=None, help="规格，如 中杯")
    parser.add_argument("--wait-user", type=float, default=None,
                        help="停在结算页后的被动等待分钟数（0=提醒后立即退出）")
    parser.add_argument("--device", default="u2", choices=["u2"],
                        help="自动化后端（当前支持 uiautomator2）")
    parser.add_argument("--addr", default="usb", help="u2 设备地址: usb 或 ip:port")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--analyze", action="store_true",
                        help="只输出当前页面体检 JSON（不执行动作）")
    parser.add_argument("--plan", action="store_true",
                        help="只打印动作清单（不连接设备）")
    args = parser.parse_args(argv)

    cfg = Config(args.config)

    if args.plan:
        if not args.product:
            parser.error("--plan 需要 --product")
        print_plan(cfg, args)
        return EXIT_OK

    device = build_device(args)

    if args.analyze:
        return cmd_analyze(device)

    if not args.product:
        parser.error("缺少必填参数 --product（商品名）")
    keyword = args.keyword or "肯德基"
    products = [p.strip() for p in args.product.split(",") if p.strip()]
    if not products:
        parser.error("--product 商品名不能为空")
    wait_min = (float(args.wait_user) if args.wait_user is not None
                else float(cfg.get("wait_user_min", 10)))

    rules = SafetyRules(cfg)
    flow = MeituanFlow(device, cfg, rules,
                       keyword=keyword, merchant=args.merchant,
                       products=products, spec=args.spec)
    try:
        code = flow.run(wait_user_min=wait_min)
    except PaymentBlockError as exc:
        print(f"[MEITUAN-UI] status=payment_blocked error={exc}")
        try:
            show_payment_reminder(device)
        except Exception:  # noqa: BLE001
            pass
        return EXIT_PAYMENT_BLOCKED
    except StepError as exc:
        print(f"[MEITUAN-UI] status=safe_stop error={exc}")
        try:
            device.back()
        except Exception:  # noqa: BLE001
            pass
        return EXIT_SAFETY_STOP
    return code


if __name__ == "__main__":
    sys.exit(main())