"""设备操作抽象层：统一 Operit 无障通道 / uiautomator2 / 模拟设备的操作接口。

核心状态机（meituan_automation.py）只依赖本层方法，与具体自动化后端解耦：
- U2Device：uiautomator2 后端（脚本/adb 模式，依赖 atx-agent）
- 方式 A（Operit 无障碍通道）由 Agent 按 SKILL.md 步骤表执行，每步用
  analyze_screen（safety_core）做页面体检决策，无需本层 U2 后端；
- FakeDevice（run_tests.py）：离线单测用。

依赖说明：uiautomator2 延迟到首次使用才导入，保证离线单测不需要安装。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class UiNode:
    """界面节点快照（与具体框架的节点格式解耦）。"""

    text: str = ""
    desc: str = ""
    resource_id: str = ""
    cls: str = ""
    bounds: str = ""
    clickable: bool = False

    def label(self) -> str:
        return self.text or self.desc or self.resource_id or self.cls or "(empty)"

    def has(self, keyword: str) -> bool:
        return keyword in self.text or keyword in self.desc


class DeviceOps:
    """自动化后端最小接口。鸭子类型即可，不必强制继承。"""

    def open_app(self, package: str) -> None:
        raise NotImplementedError

    def dump_nodes(self) -> List[UiNode]:
        raise NotImplementedError

    def click(self, node: UiNode) -> None:
        raise NotImplementedError

    def input_text(self, node: UiNode, text: str) -> None:
        raise NotImplementedError

    def press_key(self, key: str) -> None:
        raise NotImplementedError

    def back(self) -> None:
        raise NotImplementedError

    def toast(self, text: str) -> None:
        raise NotImplementedError

    def shell(self, cmd: str) -> str:
        raise NotImplementedError

    def screenshot(self, path: str) -> Optional[str]:
        raise NotImplementedError


def _attr(el, key: str) -> str:
    """兼容 uiautomator2 的 XmlElement（dict 子类或带 attrib 的对象）。"""
    if hasattr(el, "attrib"):
        return str(el.attrib.get(key, "") or "")
    if isinstance(el, dict):
        return str(el.get(key, "") or "")
    return str(getattr(el, key, "") or "")


class U2Device(DeviceOps):
    """uiautomator2 后端。

    依赖：pip install -U uiautomator2；python -m uiautomator2 init 部署 atx-agent。
    """

    def __init__(self, addr: str = "usb"):
        self._addr = addr
        self._d = None

    def _device(self):
        if self._d is None:
            try:
                import uiautomator2 as u2  # 延迟导入
            except ImportError as exc:
                raise RuntimeError(
                    "未安装 uiautomator2：请执行 pip install -U uiautomator2 "
                    "并运行 python -m uiautomator2 init 部署 atx-agent"
                ) from exc
            self._d = u2.connect(self._addr)
        return self._d

    def open_app(self, package: str) -> None:
        self._device().app_start(package)

    def dump_nodes(self) -> List[UiNode]:
        nodes: List[UiNode] = []
        for el in self._device().xpath("//*").all():
            nodes.append(
                UiNode(
                    text=_attr(el, "text"),
                    desc=_attr(el, "content-desc"),
                    resource_id=_attr(el, "resource-id"),
                    cls=_attr(el, "class"),
                    bounds=_attr(el, "bounds"),
                    clickable=(_attr(el, "clickable") == "true"),
                )
            )
        return nodes

    def click(self, node: UiNode) -> None:
        self._selector(node).click()

    def input_text(self, node: UiNode, text: str) -> None:
        self._selector(node).set_text(text)

    def press_key(self, key: str) -> None:
        self._device().press(key)

    def back(self) -> None:
        self._device().press("back")

    def toast(self, text: str) -> None:
        self._device().toast(text)

    def shell(self, cmd: str) -> str:
        return self._device().shell(cmd)

    def screenshot(self, path: str) -> Optional[str]:
        return self._device().screenshot(path)

    def _selector(self, node: UiNode):
        d = self._device()
        rid = node.resource_id
        if rid:
            return d(resourceId=rid)
        if node.text:
            return d(textContains=node.text)
        if node.desc:
            return d(descriptionContains=node.desc)
        raise ValueError(f"无法为节点构造选择器: {node.label()}")