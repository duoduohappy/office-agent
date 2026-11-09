"""端边云协同层（方案 3.1）。

`edge.coordination`：三级节点模型、按隐私/复杂度/在线状态的路由、
上送脱敏、离线队列、端↔边同步（复用 A2A）、云侧权重通道与数据流向策略。

按需导入：`from edge import coordination`。
三节点默认在本机不同端口模拟，未在真实飞腾/鲲鹏/私有云机器上部署；
边界详见模块 docstring 与 README「当前缺失项」。
"""

__all__ = ["coordination"]
