"""预测有效性摘要服务（只读）。

数据源：``<project_root>/reports/research/prediction/latest.json``（固定路径，不接受用户输入）。

契约：
- 无评估文件 / 损坏 / schema 非法 / 样本不足 / 评估时间晚于本地时钟 -> ``availability=unavailable``，
  ``data=null`` 且 ``warning`` 仅含脱敏公开文案，绝不泄露路径、堆栈或内部文件名。
- 禁止把 Westock forecast、机构评级或历史回测胜率直接称为"预测准确率"。
- ``gate_status`` 一律由后端按固定门槛派生，绝不信任输入文件的 ``passed``。
- ``net_return`` / ``max_drawdown`` / ``benchmark_return`` 为比例值（0.08 = 8.00%），后端不做乘换。
- 本服务不调用 MCP、不触发刷新、不写入任何 state。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SOURCE = "biga-evaluation"
TRANSPORT = "local_evaluation"
IS_REALTIME = False

# 评估摘要 TTL：7 天（评估流程低频产出）
EVALUATION_TTL_SECONDS = 7 * 24 * 3600
# 展示门槛：样本数低于该值视为"样本不足"（unavailable，不展示伪准确率）
MIN_SAMPLE_COUNT = 30
# 时钟漂移容忍：evaluated_at 晚于本地时钟超过该值即视为 future（不可用）
FUTURE_CLOCK_DRIFT_SECONDS = 5 * 60

# 任务参数范围
HORIZON_DAYS_MIN, HORIZON_DAYS_MAX = 1, 250
TARGET_RETURN_MIN, TARGET_RETURN_MAX = -1.0, 10.0

# 文本字段最大长度（防滥用/注入）
_MAX_TEXT_LEN = 128

# 文件顶层字段严格白名单（其余键一律拒绝）
_ALLOWED_KEYS = frozenset(
    {
        "model_version",
        "task_name",
        "horizon_days",
        "target_return",
        "accuracy",
        "precision",
        "recall",
        "auc",
        "sample_count",
        "test_start",
        "test_end",
        "net_return",
        "max_drawdown",
        "sharpe",
        "benchmark_return",
        "gate_status",  # 允许存在但值被忽略（后端派生，不信任输入）
        "evaluated_at",
    }
)

# 版本化固定评估门槛（项目默认；不因输入文件改变）
GATE_VERSION = "gate-v1"
GATE_THRESHOLDS: dict[str, Any] = {
    "sample_count_min": 1000,
    "accuracy_min": 0.80,
    "precision_min": 0.70,
    "recall_min": 0.50,
    "net_return_above_benchmark": True,
    "max_drawdown_min": -0.30,
}
_GATE_PASSED = "passed"
_GATE_NOT_PASSED = "not_passed"
_GATE_INSUFFICIENT = "insufficient_data"

_NA_MSG = "暂无经过严格样本外验证的预测准确率"
_FUTURE_MSG = "评估时间晚于本地时钟"

# 字符串字段中禁止的路径/脚本特征（路径分隔、盘符、HTML/脚本注入、模板语法）
_FORBIDDEN_TEXT = ("\\", "/", ":", "<", "{", "}", "..", "`", "$(", ";")

# 带时区 ISO 8601：必需 T 分隔、秒、时区（Z 或 ±HH:MM）；拒绝 naive / 纯日期 / 前后空格
_TZ_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _unavailable(warning: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "as_of": None,
        "fetched_at": _utcnow().isoformat(),
        "cache_status": "unavailable",
        "is_realtime": IS_REALTIME,
        "transport": TRANSPORT,
        "availability": "unavailable",
        "data": None,
        "warnings": [warning],
    }


class _RejectConstant:
    """json.loads 的 parse_constant：拒绝 NaN / Infinity / -Infinity。"""

    def __call__(self, name: str) -> Any:
        raise ValueError(f"非法常量 {name!r}")


class PredictionSummaryService:
    """只读预测有效性摘要（严格 schema 校验 + 脱敏 + 后端派生门槛）。"""

    def __init__(self, project_root: Path):
        self.root = Path(project_root).resolve()
        self.latest_file = (
            self.root / "reports" / "research" / "prediction" / "latest.json"
        )

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #

    def summary(self) -> dict[str, Any]:
        raw = self._read_file()
        if raw is None:
            return _unavailable(_NA_MSG)

        parsed = self._parse_json(raw)
        if parsed is None:
            return _unavailable(f"评估摘要文件损坏或非法；{_NA_MSG}")

        data = self._validate(parsed)
        if data is None:
            return _unavailable(f"评估摘要 schema 校验失败；{_NA_MSG}")

        if int(data["sample_count"]) < MIN_SAMPLE_COUNT:
            return _unavailable(
                f"样本数不足（{data['sample_count']}<{MIN_SAMPLE_COUNT}）；{_NA_MSG}"
            )

        # future 检测（允许 5 分钟时钟漂移）；不得用 max(0, age) 把未来判为 fresh
        ev_dt = self._parse_tz_datetime(data["evaluated_at"])
        if ev_dt is not None and ev_dt > _utcnow() + timedelta(
            seconds=FUTURE_CLOCK_DRIFT_SECONDS
        ):
            return _unavailable(_FUTURE_MSG)

        status = self._staleness(data["evaluated_at"])
        return {
            "schema_version": SCHEMA_VERSION,
            "source": SOURCE,
            "as_of": data["evaluated_at"],
            "fetched_at": _utcnow().isoformat(),
            "cache_status": status,
            "is_realtime": IS_REALTIME,
            "transport": TRANSPORT,
            "availability": status,
            "data": data,
            "warnings": [],
        }

    def summary_safe_unavailable(self) -> dict[str, Any]:
        """路由兜底：任何意外异常时的脱敏返回（不泄露堆栈/路径）。"""
        return _unavailable(_NA_MSG)

    # ------------------------------------------------------------------ #
    # 读取与解析
    # ------------------------------------------------------------------ #

    def _read_file(self) -> bytes | None:
        try:
            return self.latest_file.read_bytes()
        except (OSError, UnicodeError):
            return None

    def _parse_json(self, raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8"), parse_constant=_RejectConstant())
        except (ValueError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------------ #
    # 严格 schema 校验
    # ------------------------------------------------------------------ #

    def _validate(self, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if set(value.keys()) - _ALLOWED_KEYS:
            return None

        out: dict[str, Any] = {}

        # 文本字段
        model_version = value.get("model_version")
        task_name = value.get("task_name")
        if not isinstance(model_version, str) or not self._clean_text(model_version):
            return None
        if not isinstance(task_name, str) or not self._clean_text(task_name):
            return None
        out["model_version"] = model_version.strip()[:_MAX_TEXT_LEN]
        out["task_name"] = task_name.strip()[:_MAX_TEXT_LEN]

        # horizon_days：1–250
        horizon_days = value.get("horizon_days")
        if (
            not isinstance(horizon_days, int)
            or isinstance(horizon_days, bool)
            or not (HORIZON_DAYS_MIN <= horizon_days <= HORIZON_DAYS_MAX)
        ):
            return None
        out["horizon_days"] = horizon_days

        # target_return：finite 且 -1 ~ 10
        target_return = value.get("target_return")
        if (
            not self._finite_number(target_return)
            or not (TARGET_RETURN_MIN <= float(target_return) <= TARGET_RETURN_MAX)
        ):
            return None
        out["target_return"] = float(target_return)

        for key in ("accuracy", "precision", "recall"):
            v = value.get(key)
            if not self._finite_number(v) or not (0.0 <= float(v) <= 1.0):
                return None
            out[key] = float(v)

        auc = value.get("auc")
        if auc is not None:
            if not self._finite_number(auc) or not (0.0 <= float(auc) <= 1.0):
                return None
            out["auc"] = float(auc)
        else:
            out["auc"] = None

        sample_count = value.get("sample_count")
        if (
            not isinstance(sample_count, int)
            or isinstance(sample_count, bool)
            or sample_count < 1
        ):
            return None
        out["sample_count"] = sample_count

        # 日期区间
        test_start = value.get("test_start")
        test_end = value.get("test_end")
        if not isinstance(test_start, str) or not self._valid_date(test_start):
            return None
        if not isinstance(test_end, str) or not self._valid_date(test_end):
            return None
        if test_start > test_end:
            return None  # test_start 不得晚于 test_end
        out["test_start"] = test_start
        out["test_end"] = test_end

        # 比例字段：原样保留（不乘换）；None 或 finite 浮点
        for key in ("net_return", "max_drawdown", "sharpe", "benchmark_return"):
            v = value.get(key)
            if v is None:
                out[key] = None
            elif self._finite_number(v):
                out[key] = float(v)
            else:
                return None

        # evaluated_at：必填，严格带时区 ISO 8601
        evaluated_at = value.get("evaluated_at")
        if not isinstance(evaluated_at, str):
            return None
        ev_dt = self._parse_tz_datetime(evaluated_at)
        if ev_dt is None:
            return None
        # evaluated_at 的日期不得早于 test_end
        if ev_dt.strftime("%Y-%m-%d") < test_end:
            return None
        out["evaluated_at"] = evaluated_at

        # gate_status 由后端派生（输入值被忽略）
        gate, reasons = self._derive_gate(out)
        out["gate_status"] = gate
        out["gate_version"] = GATE_VERSION
        out["gate_reasons"] = reasons

        return out

    # ------------------------------------------------------------------ #
    # 门槛派生（版本化固定阈值，不信任输入）
    # ------------------------------------------------------------------ #

    def _derive_gate(self, data: dict[str, Any]) -> tuple[str, list[str]]:
        t = GATE_THRESHOLDS
        reasons: list[str] = []

        if data["sample_count"] < t["sample_count_min"]:
            reasons.append(
                f"样本数不足（{data['sample_count']}<{t['sample_count_min']}）"
            )
            return _GATE_INSUFFICIENT, reasons

        if data["accuracy"] < t["accuracy_min"]:
            reasons.append(f"样本外准确率 {data['accuracy']:.2f} < {t['accuracy_min']:.2f}")
        if data["precision"] < t["precision_min"]:
            reasons.append(f"Precision {data['precision']:.2f} < {t['precision_min']:.2f}")
        if data["recall"] < t["recall_min"]:
            reasons.append(f"Recall {data['recall']:.2f} < {t['recall_min']:.2f}")

        net = data.get("net_return")
        bench = data.get("benchmark_return")
        if net is None or bench is None or net <= bench:
            reasons.append("扣费后收益未超过基准（或缺失）")

        dd = data.get("max_drawdown")
        if dd is None or dd < t["max_drawdown_min"]:
            reasons.append(f"最大回撤未达标（需 ≥ {t['max_drawdown_min']:.2f}）")

        if not reasons:
            return _GATE_PASSED, []
        return _GATE_NOT_PASSED, reasons

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _finite_number(v: Any) -> bool:
        return (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(float(v))
        )

    @staticmethod
    def _clean_text(s: str) -> bool:
        s = s.strip()
        if not s or len(s) > _MAX_TEXT_LEN:
            return False
        return not any(tok in s for tok in _FORBIDDEN_TEXT)

    @staticmethod
    def _valid_date(s: str) -> bool:
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    @staticmethod
    def _parse_tz_datetime(s: str) -> datetime | None:
        """严格带时区 ISO 8601；拒绝 naive / 纯日期 / 前后空格 / 非法时区与日期。"""
        if not isinstance(s, str):
            return None
        if not _TZ_DATETIME_RE.match(s):
            return None
        normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return None
        return dt

    # ------------------------------------------------------------------ #
    # stale 判定
    # ------------------------------------------------------------------ #

    def _staleness(self, evaluated_at: str) -> str:
        """fresh / stale。未来时间已在 summary 层拦截。"""
        dt = self._parse_tz_datetime(evaluated_at)
        if dt is None:
            return "stale"
        age = (_utcnow() - dt).total_seconds()
        return "fresh" if age <= EVALUATION_TTL_SECONDS else "stale"


def build_prediction_service(project_root: Path) -> PredictionSummaryService:
    """create_app 工厂（与 stocks/market/screener 等服务一致）。"""
    return PredictionSummaryService(project_root)
