"""离线单元测试（无需真机、无需 uiautomator2）。

用法：python3 scripts/run_tests.py
覆盖：支付护栏拦截/放行、广告弹窗识别与关闭、定位漂移识别、
      页面体检 analyze_screen、配置默认值、端到端状态机（Fake 设备）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from device_layer import UiNode  # noqa: E402
from safety_core import (  # noqa: E402
    EXIT_OK,
    EXIT_PAYMENT_BLOCKED,
    EXIT_SAFETY_STOP,
    EXIT_WAITING_USER,
    PaymentBlockError,
    SafetyRules,
    analyze_screen,
)
from meituan_automation import Config, MeituanFlow  # noqa: E402

PASSED: list = []
FAILED: list = []


def check(name: str, cond: bool) -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}")


class FakeDevice:
    """按已编排 stage 返回节点，记录全部点击/输入/按键/提醒动作。"""

    def __init__(self, stages):
        self.stages = stages
        self.stage = 0
        self.apps: list = []
        self.clicks: list = []
        self.inputs: list = []
        self.keys: list = []
        self.toasts: list = []
        self.shell_cmds: list = []

    def open_app(self, package):
        self.apps.append(package)

    def dump_nodes(self):
        return self.stages[self.stage]

    def click(self, node):
        self.clicks.append(node.label())
        mapping = {
            "搜索": 1,
            "商家": 1,
            "肯德基测试店": 2,
            "香辣鸡腿堡": 3,
            "加入购物车": 4,
            "去结算": 5,
        }
        if node.text in mapping:
            self.stage = mapping[node.text]

    def input_text(self, node, text):
        self.inputs.append((node.label(), text))
        # 模拟输入法：输入后搜索框文本变为关键词（真实设备如此）
        for n in self.stages[self.stage]:
            if n.label() == node.label() and n.text:
                n.text = text

    def press_key(self, key):
        self.keys.append(key)
        if key == "enter":
            self.stage = 1

    def back(self):
        self.stage = max(0, self.stage - 1)

    def toast(self, text):
        self.toasts.append(text)

    def shell(self, cmd):
        self.shell_cmds.append(cmd)
        return ""


def node(text="", rid="", desc="", clickable=True):
    return UiNode(text=text, desc=desc, resource_id=rid, clickable=clickable)


def build_stages():
    home = [node("搜索商家、地点或关键词")]
    results = [
        node("商家", clickable=True),
        node("肯德基测试店", clickable=True),
        node("奶茶店", clickable=True),
    ]
    shop = [node("肯德基测试店", clickable=False), node("香辣鸡腿堡", clickable=True)]
    detail = [node("香辣鸡腿堡", clickable=False), node("加入购物车", clickable=True)]
    cart = [node("购物车", clickable=True), node("去结算", clickable=True)]
    checkout = [
        node("订单金额 ¥42.5", clickable=False),
        node("确认支付", clickable=True),  # 结算页上必然出现，但绝不允许被点击
        node("支付方式", clickable=False),
    ]
    return [home, results, shop, detail, cart, checkout]


# ---------- 测试用例 ----------
def test_guard():
    print("== 支付护栏 ==")
    rules = SafetyRules()
    bad = ("确认支付", "立即支付", "确认付款", "去支付", "提交订单",
           "确认下单", "立即付款并提交订单", "确认订单并支付")
    for text in bad:
        try:
            rules.guard_click(node(text))
            check(f"拦截 {text}", False)
        except PaymentBlockError:
            check(f"拦截 {text}", True)
    # 描述字段也应拦截
    try:
        rules.guard_click(node(desc="立即支付"))
        check("拦截 desc=立即支付", False)
    except PaymentBlockError:
        check("拦截 desc=立即支付", True)
    good = ("去结算", "加入购物车", "搜索", "商家", "确定", "完成",
            "关闭", "重新定位", "购物车", "肯德基", "香辣鸡腿堡")
    for text in good:
        try:
            rules.guard_click(node(text))
            check(f"放行 {text}", True)
        except PaymentBlockError:
            check(f"放行 {text}", False)


def test_ad_popup():
    print("== 广告弹窗 ==")
    rules = SafetyRules()
    popup = [node("限时红包 领取", clickable=False), node("关闭", clickable=True)]
    check("识别广告弹窗", rules.is_ad_dialog(popup))
    btn = rules.find_close_button(popup)
    check("关闭按钮命中", btn is not None and btn.text == "关闭")
    normal = [node("香辣鸡腿堡", clickable=True), node("加入购物车", clickable=True)]
    check("普通页面不误判", not rules.is_ad_dialog(normal))
    check("普通页面无关闭按钮", rules.find_close_button(normal) is None)


def test_location():
    print("== 定位漂移 ==")
    rules = SafetyRules()
    drift = [node("定位不准，点击重试", clickable=False), node("重新定位", clickable=True)]
    check("识别定位漂移", rules.is_location_drift(drift))
    act = rules.find_location_action(drift)
    check("定位动作命中", act is not None and act.text == "重新定位")
    permission = [node("申请定位权限", clickable=False), node("仅使用期间允许", clickable=True)]
    check("识别定位授权弹窗", rules.is_location_drift(permission))
    act2 = rules.find_location_action(permission)
    check("授权按钮命中", act2 is not None and act2.text == "仅使用期间允许")


def test_analyze():
    print("== 页面体检 ==")
    rules = SafetyRules()
    checkout = [
        node("订单金额 ¥42.5", clickable=False),
        node("确认支付", clickable=True),
        node("支付方式", clickable=False),
    ]
    report = analyze_screen(checkout, rules)
    check("体检发现支付按钮", report["has_payment_button"] is True)
    check("体检判定结算页", report["at_checkout"] is True)
    check("体检不含广告误报", report["has_ad_popup"] is False)
    home = [node("搜索商家、地点或关键词")]
    report2 = analyze_screen(home, rules)
    check("首页体检无支付按钮", report2["has_payment_button"] is False)
    check("首页体检非结算页", report2["at_checkout"] is False)


def test_config_defaults():
    print("== 配置默认值 ==")
    cfg = Config(None)
    check("默认包名", cfg.get("package") == "com.sankuai.meituan")
    check("默认等待分钟", cfg.get("wait_user_min") == 10)
    check("搜索框rid默认空", cfg.get("search_box_rid", "") == "")


def test_end_to_end():
    print("== 端到端状态机（Fake 设备） ==")
    dev = FakeDevice(build_stages())
    cfg = Config(None)
    rules = SafetyRules(cfg)
    flow = MeituanFlow(dev, cfg, rules,
                       keyword="肯德基", merchant="肯德基测试店",
                       products=["香辣鸡腿堡"], spec=None)
    code = flow.run(wait_user_min=0)
    check("流程返回等待用户状态码42", code == EXIT_WAITING_USER)
    expected = ["商家", "肯德基测试店", "香辣鸡腿堡", "加入购物车", "购物车", "去结算"]
    check("点击序列正确", dev.clicks == expected)
    check("从未点击支付按钮", "确认支付" not in dev.clicks)
    check("搜索框输入关键词", dev.inputs == [("搜索商家、地点或关键词", "肯德基")])
    check("已触发付款前Toast提醒", any("指纹" in t for t in dev.toasts))
    check("已发送系统通知提醒", any("cmd notification post" in c for c in dev.shell_cmds))


def test_exit_codes():
    print("== 退出码常量 ==")
    check("OK=0", EXIT_OK == 0)
    check("WAITING_USER=42", EXIT_WAITING_USER == 42)
    check("PAYMENT_BLOCKED=90", EXIT_PAYMENT_BLOCKED == 90)
    check("SAFETY_STOP=91", EXIT_SAFETY_STOP == 91)


def main():
    test_guard()
    test_ad_popup()
    test_location()
    test_analyze()
    test_config_defaults()
    test_end_to_end()
    test_exit_codes()
    print(f"\n结果: {len(PASSED)} 通过, {len(FAILED)} 失败")
    if FAILED:
        print("失败项:", ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())