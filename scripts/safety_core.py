"""安全护栏核心（与设备无关，可离线单测）。

职责：
1. 支付黑名单拦截：任何含"支付/付款确认"语义的节点一律禁止点击（guard_click）；
2. 付款前强制提醒：到达去结算页后触发通知/提示，绝不继续点击任何按钮；
3. 广告弹窗跳过、定位漂移回退的识别与动作选择。

所有词表均可由 config/skill_config.yaml 覆写（见 SafetyRules）。
"""
from __future__ import annotations

import re
import time
from typing import List, Optional

from device_layer import UiNode

EXIT_OK = 0
EXIT_WAITING_USER = 42  # 已停在去结算页，等待用户人工确认支付
EXIT_PAYMENT_BLOCKED = 90  # 支付护栏拦截触发（防御性停止，未点击支付按钮）
EXIT_SAFETY_STOP = 91  # 流程异常/超时安全停止


class PaymentBlockError(Exception):
    """试图点击支付类按钮时抛出。任何上层均不得吞掉此异常。"""


DEFAULT_PAYMENT_SUBSTRINGS = (
    "确认支付", "立即支付", "确认付款", "立即付款", "确认订单并支付",
    "提交订单并支付", "去支付", "去付款", "立即购买并支付", "确认购买",
    "确认下单", "确认支付订单",
)
DEFAULT_PAYMENT_PATTERNS = (
    r"(确认|立即|提交|去)[\s\u00a0]*(支付|付款)",
    r"^提交订单$",
)
DEFAULT_PAYMENT_SUCCESS = ("支付成功", "已完成支付", "付款成功", "交易成功", "订单完成")
DEFAULT_AD_HINTS = ("广告", "推广", "红包", "领券", "优惠券", "抽奖", "会员", "首单", "新人", "活动")
DEFAULT_AD_CLOSE_TEXTS = ("关闭", "跳过", "我知道了", "以后再说", "暂不需要", "不感兴趣", "知道了", "Close", "Skip")
DEFAULT_CLOSE_RID_KEYWORDS = ("close", "dismiss", "cancel", "delete")
DEFAULT_LOCATION_DRIFT = ("定位不准", "定位失败", "重新定位", "定位服务未开启", "检测到位置变化", "城市已切换")
DEFAULT_LOCATION_PERMISSION = ("授权定位", "允许定位", "定位权限", "仅使用期间允许", "仅在使用期间允许", "使用时允许")
DEFAULT_LOCATION_CONFIRM = ("重新定位", "确定", "允许", "仅使用期间允许", "仅在使用期间允许")
DEFAULT_CHECKOUT_LANDMARKS = ("提交订单", "确认支付", "去支付", "收银台", "订单金额", "支付方式")


def _merge_lists(base, extra) -> tuple:
    out = list(base)
    for item in extra or []:
        if item not in out:
            out.append(item)
    return tuple(out)


def _cfg_list(cfg, key: str) -> list:
    if cfg is None:
        return []
    value = cfg.get(key, None)
    return list(value) if isinstance(value, (list, tuple)) else []


class SafetyRules:
    """从配置构造的护栏规则集（未给配置时用内置默认值）。"""

    def __init__(self, cfg=None):
        self.payment_substrings = _merge_lists(
            DEFAULT_PAYMENT_SUBSTRINGS, _cfg_list(cfg, "payment_block_substrings"))
        self.payment_patterns = DEFAULT_PAYMENT_PATTERNS + tuple(
            _cfg_list(cfg, "payment_block_patterns"))
        self.payment_success = _merge_lists(
            DEFAULT_PAYMENT_SUCCESS, _cfg_list(cfg, "payment_success_texts"))
        self.ad_hints = _merge_lists(DEFAULT_AD_HINTS, _cfg_list(cfg, "ad_hints"))
        self.ad_close_texts = _merge_lists(
            DEFAULT_AD_CLOSE_TEXTS, _cfg_list(cfg, "ad_close_texts"))
        self.close_rid_keywords = DEFAULT_CLOSE_RID_KEYWORDS
        self.location_drift = _merge_lists(
            DEFAULT_LOCATION_DRIFT, _cfg_list(cfg, "location_drift_texts"))
        self.location_permission = _merge_lists(
            DEFAULT_LOCATION_PERMISSION, _cfg_list(cfg, "location_permission_texts"))
        self.location_confirm = _merge_lists(
            DEFAULT_LOCATION_CONFIRM, _cfg_list(cfg, "location_confirm_texts"))
        self.checkout_landmarks = _merge_lists(
            DEFAULT_CHECKOUT_LANDMARKS, _cfg_list(cfg, "checkout_landmarks"))

    def scan_payment_hit(self, text: str, desc: str = "") -> Optional[str]:
        """返回命中的支付黑名单模式；未命中返回 None。"""
        for sub in self.payment_substrings:
            if sub in text or sub in desc:
                return f"substring:{sub}"
        for pat in self.payment_patterns:
            if re.search(pat, text) or re.search(pat, desc):
                return f"pattern:{pat}"
        return None

    def guard_click(self, node: UiNode, context: str = "") -> None:
        """点击前强制校验：命中黑名单直接抛 PaymentBlockError。"""
        hit = self.scan_payment_hit(node.text, node.desc)
        if hit:
            raise PaymentBlockError(
                f"支付安全拦截: text={node.text!r} desc={node.desc!r} "
                f"rid={node.resource_id!r} hit={hit} ctx={context}")

    def is_ad_dialog(self, nodes: List[UiNode]) -> bool:
        joined = " ".join(n.text + " " + n.desc for n in nodes)
        return any(h in joined for h in self.ad_hints)

    def find_close_button(self, nodes: List[UiNode]) -> Optional[UiNode]:
        """在疑似广告弹窗中找"关闭类"按钮（白名单），并先过支付护栏。"""
        for n in nodes:
            t = n.text.strip()
            if t in self.ad_close_texts:
                self.guard_click(n, "ad-popup-close")
                return n
        for n in nodes:
            rid = n.resource_id.lower()
            if any(k in rid for k in self.close_rid_keywords) and len(n.text.strip()) <= 4:
                self.guard_click(n, "ad-popup-close-rid")
                return n
        return None

    def is_location_drift(self, nodes: List[UiNode]) -> bool:
        joined = " ".join(n.text + " " + n.desc for n in nodes)
        return any(t in joined for t in self.location_drift) or any(
            t in joined for t in self.location_permission)

    def find_location_action(self, nodes: List[UiNode]) -> Optional[UiNode]:
        """返回可点击的定位纠偏按钮（重新定位/允许等），并先过支付护栏。"""
        for n in nodes:
            t = n.text.strip()
            if t in self.location_confirm:
                self.guard_click(n, "fix-location")
                return n
        return None


def analyze_screen(nodes: List[UiNode], rules: SafetyRules) -> dict:
    """页面体检：供 Agent 决策使用（只读，不产生任何点击）。"""
    hits = []
    for n in nodes:
        hit = rules.scan_payment_hit(n.text, n.desc)
        if hit:
            hits.append({"text": n.text, "desc": n.desc, "hit": hit})
    joined = " ".join(n.text + " " + n.desc for n in nodes)
    close_btn = rules.find_close_button(nodes)
    return {
        "has_payment_button": bool(hits),
        "payment_hits": hits,
        "has_ad_popup": rules.is_ad_dialog(nodes),
        "ad_close_candidates": [close_btn.label()] if close_btn is not None else [],
        "has_location_quirk": rules.is_location_drift(nodes),
        "at_checkout": any(k in joined for k in rules.checkout_landmarks),
    }


def show_payment_reminder(device, product_info: str = "") -> None:
    """付款前强制提醒：控制台横幅 + 设备 Toast + 系统通知。绝不代表用户支付。"""
    banner_lines = [
        "=" * 64,
        '  [强制提醒] 已到达"去结算"页面，本次自动化到此停止。',
        "  请人工核对订单，并手动完成支付（指纹/密码）。",
        "  自动化不会点击任何『支付/付款』类按钮。",
        "=" * 64,
    ]
    banner = "\n" + "\n".join(banner_lines) + ("\n商品: " + product_info if product_info else "")
    print(banner)
    try:
        device.toast("请人工确认支付（指纹）")
    except Exception:  # noqa: BLE001
        pass
    try:
        title = "付款前提醒"
        body = "美团自动化已停在去结算页，请人工核对订单并手动支付（指纹/密码）。脚本已停止，绝不代付。"
        device.shell(f'cmd notification post -t "{title}" "{body}"')
    except Exception as exc:  # noqa: BLE001
        print(f"[提醒] 系统通知发送失败(不影响安全,仅建议): {exc}")


def wait_for_manual_payment(device, timeout_min: float = 10.0,
                            rules: Optional[SafetyRules] = None) -> bool:
    """被动等待用户手动支付完成；期间绝不点击任何节点，仅轮询屏幕文本。"""
    rules = rules or SafetyRules()
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        try:
            nodes = device.dump_nodes()
        except Exception:  # noqa: BLE001
            nodes = []
        joined = " ".join(n.text + " " + n.desc for n in nodes)
        if any(s in joined for s in rules.payment_success):
            print("[等待结束] 已检测到支付成功提示。")
            return True
        time.sleep(5)
    print("[等待结束] 等待超时，请人工确认订单状态。")
    return False