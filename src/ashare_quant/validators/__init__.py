"""数据源校验器（validator）包。

与 providers 的区别：providers 负责从网络抓取原始数据（进入正式数据链），
validators 负责**旁路核验**——对已入库数据与第三方数据源做交叉差异检测，
不参与回测主链，也不生成任何前复权字段。
"""

from .westock_validator import (
    AVAILABLE,
    NO_DATA,
    UNAVAILABLE,
    ValidationResult,
    WestockValidator,
)

__all__ = [
    "WestockValidator",
    "ValidationResult",
    "AVAILABLE",
    "UNAVAILABLE",
    "NO_DATA",
]
