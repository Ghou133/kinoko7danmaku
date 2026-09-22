"""浮点数范围设置卡片"""

from PySide6.QtCore import Signal, QSignalBlocker
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QWidget
from qfluentwidgets import RangeSettingCard, qconfig
from qfluentwidgets.common.config import RangeConfigItem
from qfluentwidgets.common.icon import FluentIconBase


class FloatRangeSettingCard(RangeSettingCard):
    """支持浮点数和自定义步长的范围设置卡片

    相比 RangeSettingCard，支持：
    - 浮点数值
    - 自定义步长
    - 自定义显示精度
    """

    valueChanged = Signal(float)

    def __init__(
        self,
        configItem: RangeConfigItem,
        icon: str | QIcon | FluentIconBase,
        title: str,
        content: str | None = None,
        step: float = 0.1,
        decimals: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        """初始化浮点数范围设置卡片

        Args:
            configItem: 配置项
            icon: 图标
            title: 标题
            content: 描述
            step: 步长（默认 0.1）
            decimals: 显示小数位数（默认 1）
            parent: 父组件
        """
        self.step = step
        self.decimals = decimals
        self._internal_step = int(1 / step)  # 内部步长倍数

        # 调用父类初始化（会设置 slider）
        super().__init__(configItem, icon, title, content, parent)

        # Config values are real units; only slider signals use scaled integers.
        # Keeping these paths separate also makes several cards bound to one
        # config item safe when a value such as speed=1 arrives as an integer.
        configItem.valueChanged.disconnect(self.setValue)
        configItem.valueChanged.connect(self._sync_from_config)
        self.slider.valueChanged.disconnect(self._RangeSettingCard__onValueChanged)
        self.slider.valueChanged.connect(self._on_slider_changed)

        # 重新配置 slider 以支持浮点数
        self._reconfigure_slider()

    def _reconfigure_slider(self) -> None:
        """重新配置滑块以支持浮点数"""
        min_val, max_val = self.configItem.range

        # 阻止信号触发，避免在设置范围时触发 valueChanged
        self.slider.blockSignals(True)
        # 将浮点数范围转换为整数范围
        self.slider.setRange(int(min_val * self._internal_step), int(max_val * self._internal_step))
        self.slider.setSingleStep(1)  # 内部步长始终为 1
        self.slider.setValue(int(self.configItem.value * self._internal_step))

        # 恢复信号
        self.slider.blockSignals(False)
        # 更新显示标签
        self._update_label(self.configItem.value)

    def _update_label(self, value: float) -> None:
        """更新显示标签

        Args:
            value: 要显示的浮点数值
        """
        self.valueLabel.setText(f'{value:.{self.decimals}f}')
        self.valueLabel.adjustSize()

    def setValue(self, value: float) -> None:
        """设置滑块值（重写父类方法）

        Args:
            value: 浮点数值
        """
        qconfig.set(self.configItem, value)
        self._sync_from_config(self.configItem.value)

    def _sync_from_config(self, value: float) -> None:
        self._update_label(value)
        with QSignalBlocker(self.slider):
            self.slider.setValue(round(value * self._internal_step))

    def _on_slider_changed(self, value: int) -> None:
        self.setValue(round(value / self._internal_step, self.decimals))
        self.valueChanged.emit(float(self.configItem.value))
