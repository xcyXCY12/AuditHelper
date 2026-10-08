"""
soda_runner.py —— Soda Core v4 契约校验封装
============================================================
职责：
  1. 生成 Soda 数据源配置（ds_config.yml，type: duckdb，指向我们的内存库）
  2. 复用调用方传入的 DuckDB cursor —— 这是关键：
     Soda 通过 file 路径连接 DuckDB，而我们的视图存在于一个 :memory: 库里，
     因此这里把内存库 export/attach 为临时 .duckdb 文件供 Soda 连接，
     连接建立后视图与数据与调用方看见的完全一致。
  3. 封装 soda_core.contracts.verify_contract_locally()
  4. 【降级内核】当 soda-duckdb 未安装时，用纯 pandas 复刻核心 check 语义，
     使整个应用在没有 Soda 的环境下依然可运行（离线演示/单元测试友好）。

对外主入口：
    run_contract(con, contract_path, contract_name) -> ContractRunResult
"""
from __future__ import annotations

import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
import yaml

import config

# ---------------------------------------------------------------------------
# 可选依赖探测
# ---------------------------------------------------------------------------
SODA_IMPORT_ERROR: Optional[str] = None
try:
    from soda_core.contracts import verify_contract_locally as _verify_contract_locally

    SODA_AVAILABLE = True
except Exception as exc:      # pragma: no cover - 取决于环境
    _verify_contract_locally = None
    SODA_AVAILABLE = False
    SODA_IMPORT_ERROR = str(exc)


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------
@dataclass
class CheckOutcome:
    """单条检查的结果（Soda 原生结果与本降级内核共用的统一结构）。"""

    name: str
    level: str            # pass | warn | fail
    check_type: str = ""
    column: Optional[str] = None
    metric: Optional[float] = None
    message: str = ""

    @property
    def icon(self) -> str:
        return {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(self.level, "•")


@dataclass
class ContractRunResult:
    """一次契约校验的完整结果。"""

    contract_name: str
    dataset: str
    outcomes: list[CheckOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    engine: str = "soda"          # soda | fallback
    raw: Any = None

    # ---- 与 Soda ContractVerificationSessionResult 对齐的语义 ----
    @property
    def is_passed(self) -> bool:
        return not any(o.level == "fail" for o in self.outcomes)

    @property
    def is_warned(self) -> bool:
        return any(o.level == "warn" for o in self.outcomes)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def is_ok(self) -> bool:
        """无失败且无执行错误 —— 与 Soda 的 is_ok 语义一致，CI 网关应使用它。"""
        return self.is_passed and not self.has_errors

    @property
    def counts(self) -> dict[str, int]:
        return {
            "pass": sum(1 for o in self.outcomes if o.level == "pass"),
            "warn": sum(1 for o in self.outcomes if o.level == "warn"),
            "fail": sum(1 for o in self.outcomes if o.level == "fail"),
        }


# ---------------------------------------------------------------------------
# 数据源配置生成
# ---------------------------------------------------------------------------
def write_data_source_config(
    db_path: str,
    name: str = "audit_duckdb",
    out_dir: Optional[str] = None,
) -> str:
    """
    生成 Soda 数据源配置 YAML。

    结构参考 Soda 官方 data source 参考：
        name: <数据源名>
        type: duckdb
        connection:
          database: <路径 或 :memory:>
    """
    out_dir = out_dir or tempfile.mkdtemp(prefix="audit_soda_")
    path = os.path.join(out_dir, "ds_config.yml")
    payload = {
        "name": name,
        "type": "duckdb",
        "connection": {"database": db_path.replace("\\", "/")},
    }
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    return path


def materialize_duckdb(
    con,
    views: list[str],
    out_dir: Optional[str] = None,
) -> tuple[str, str]:
    """
    把内存库落成临时 .duckdb 文件，让 Soda 能通过路径连接。

    返回 (db_path, catalog_name)。

    【为什么要返回 catalog_name】
    Soda 契约里的 dataset 是全限定名 `数据源名/catalog/schema/表名`。
    DuckDB 的 catalog 名取决于连接方式：
      - :memory: 连接          → catalog = "memory"
      - 文件连接 xxx.duckdb    → catalog = 文件名（不含扩展名）
    我们把视图物化到文件库后，契约里的 catalog 段必须相应改写成文件名，
    否则 Soda 会报 `Catalog "xxx" does not exist`。
    """
    import duckdb

    out_dir = out_dir or tempfile.mkdtemp(prefix="audit_soda_")
    stem = f"audit_{uuid.uuid4().hex[:8]}"
    db_path = os.path.join(out_dir, f"{stem}.duckdb")

    target = duckdb.connect(database=db_path)
    try:
        for view in views:
            try:
                df = con.execute(f'SELECT * FROM "{view}"').df()
            except Exception:
                continue
            target.register(f"__src_{view}", df)
            target.execute(f'CREATE OR REPLACE TABLE "{view}" AS SELECT * FROM "__src_{view}";')
    finally:
        target.close()

    return db_path, stem


def _rewrite_dataset_catalog(
    contract_path: str,
    view_name: str,
    catalog: str,
    out_dir: Optional[str] = None,
) -> str:
    """
    把契约的 dataset 路径改写为 `数据源名/catalog/schema/表名`。

    DuckDB 没有独立的"数据库名"层，前缀只有 catalog + schema 两段，
    而 Soda 的 dataset 形如 `数据源名/db/schema/表`。
    因此统一生成为：  数据源名 / <实际catalog> / main / <表名>

    不改动原契约文件，另存一份临时副本，保证 contracts/ 下的模板保持可读。
    """
    contract = _load_contract(contract_path)
    contract["dataset"] = f"audit_duckdb/{catalog}/main/{view_name}"

    out_dir = out_dir or tempfile.mkdtemp(prefix="audit_soda_")
    path = os.path.join(out_dir, os.path.basename(contract_path))
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(contract, fh, allow_unicode=True, sort_keys=False)
    return path


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_contract(
    con,
    contract_path: str,
    *,
    views: Optional[list[str]] = None,
    out_dir: Optional[str] = None,
    locale: str = "zh",
) -> ContractRunResult:
    """
    执行一次契约校验。

    参数：
        con            复用同一个 DuckDB 连接（我们的视图就注册在这里）
        contract_path  契约 YAML 路径
        views          需要暴露给 Soda 的视图名列表（用于物化临时库）
        out_dir        临时目录

    优先使用 Soda；不可用时自动降级到内置 pandas 内核。
    """
    contract = _load_contract(contract_path)
    dataset = str(contract.get("dataset", ""))
    view_name = dataset.split("/")[-1] if dataset else ""

    # ---------- 路径 A：真实 Soda ----------
    if SODA_AVAILABLE and views:
        try:
            db_path, catalog = materialize_duckdb(con, views, out_dir)
            ds_path = write_data_source_config(db_path, out_dir=out_dir)
            # 契约里的 catalog 段必须与实际文件库的 catalog 名一致，
            # 否则 Soda 报 "Catalog does not exist"。
            effective_contract = _rewrite_dataset_catalog(
                contract_path, view_name, catalog, out_dir
            )
            result = _verify_contract_locally(
                data_source_file_path=ds_path,
                contract_file_path=effective_contract,
                publish=False,
            )
            return _adapt_soda_result(result, contract_path, dataset)
        except Exception as exc:
            # Soda 运行失败不应让整个应用崩溃：记录错误并降级
            fallback = _run_fallback(con, contract, contract_path, view_name)
            fallback.errors.append(f"Soda 执行异常，已降级到内置内核：{exc}")
            return fallback

    # ---------- 路径 B：降级内核 ----------
    result = _run_fallback(con, contract, contract_path, view_name)
    if not SODA_AVAILABLE:
        result.errors.append(
            f"未安装 soda-duckdb（{SODA_IMPORT_ERROR}），当前使用内置 pandas 内核。"
            "如需真实 Soda 校验，请执行：pip install soda-duckdb"
        )
    return result


def run_contract_on_dataframe(
    df: pd.DataFrame,
    contract_path: str,
    view_name: str = "__df_check__",
) -> ContractRunResult:
    """
    对已在内存中的 DataFrame 直接校验（无需 DuckDB）。

    用途：app.py 的"输出校验"步骤可以对对账结果直接校验，
         不必先把结果写回数据库，链路更短。
    """
    import duckdb

    con = duckdb.connect(database=":memory:")
    con.register(view_name, df)
    contract = _load_contract(contract_path)
    return _run_fallback(con, contract, contract_path, view_name)


# ---------------------------------------------------------------------------
# Soda 原生结果 → 统一结构
# ---------------------------------------------------------------------------
def _adapt_soda_result(result, contract_path: str, dataset: str) -> ContractRunResult:
    """
    把 Soda 的 ContractVerificationSessionResult 拍平成 CheckOutcome 列表。

    Soda Core v4 的实际对象结构（已在 v4.26 上验证）：
      result                              SessionResult
        .is_ok / .is_passed / .is_failed / .is_warned / .has_errors   → bool 属性（非方法！）
        .contract_verification_results    list[ContractVerificationResult]
            .check_results                list[CheckResult]
                .outcome                  CheckOutcome 枚举（PASSED / WARNED / FAILED / NOT_EVALUATED）
                .is_passed/.is_failed/.is_warned   bool 属性
                .threshold_value          阈值
                .diagnostic_metric_values dict，如 {'check_rows_tested': 3}
                .check                    内层 Check 对象
                    .name                 人类可读检查名
                    .type                 检查类型（row_count / missing / invalid / ...）
                    .column_name          列名
                    .qualifier            限定符
    """
    out = ContractRunResult(
        contract_name=os.path.basename(contract_path),
        dataset=dataset,
        engine="soda",
        raw=result,
    )

    try:
        for session in getattr(result, "contract_verification_results", None) or []:
            for chk in getattr(session, "check_results", None) or []:
                inner = getattr(chk, "check", None)

                name = _safe_get(inner, "name") or "check"
                ctype = str(_safe_get(inner, "type") or "")
                column = _safe_get(inner, "column_name") or _safe_get(chk, "column_name")
                qualifier = _safe_get(inner, "qualifier")
                if qualifier:
                    name = f"{name} [{qualifier}]"

                metric = _soda_metric(chk)
                threshold = _safe_get(chk, "threshold_value")

                out.outcomes.append(CheckOutcome(
                    name=str(name),
                    level=_soda_level(chk),
                    check_type=ctype,
                    column=column,
                    metric=metric,
                    message=_soda_message(chk, metric, threshold),
                ))

        # 执行期错误 / 警告
        for err in _as_list(_call_or_attr(result, "get_errors")):
            out.errors.append(str(err))
        for warn in _as_list(_call_or_attr(result, "get_warnings")):
            out.errors.append(f"警告：{warn}")

    except Exception as exc:
        out.errors.append(f"解析 Soda 结果失败：{exc}")

    if not out.outcomes and not out.errors:
        out.errors.append(
            "Soda 未返回任何检查结果，请确认契约中的 dataset 路径与视图名一致。"
        )
    return out


def _safe_get(obj, attr, default=None):
    """安全取属性。"""
    if obj is None:
        return default
    try:
        val = getattr(obj, attr)
        return default if val is None else val
    except Exception:
        return default


def _call_or_attr(obj, name):
    """Soda 的部分 API 在不同版本里是方法或属性，这里统一取值。"""
    try:
        val = getattr(obj, name)
    except Exception:
        return None
    try:
        return val() if callable(val) else val
    except Exception:
        return None


def _as_list(val) -> list:
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        return list(val)
    return [val]


def _soda_metric(chk) -> Optional[float]:
    """
    从 CheckResult 里提取"被检查的度量值"。

    Soda 把度量放在 diagnostic_metric_values 字典里，键名因检查类型而异：
      - missing / invalid / duplicate → 'check_rows_tested' 之外的失败计数
      - row_count                      → 'dataset_rows_tested'
    这里优先取与检查语义最匹配的计数键。
    """
    diag = _safe_get(chk, "diagnostic_metric_values") or {}
    if not isinstance(diag, dict):
        return None

    # 度量类检查：失败行数/缺失数通常以主度量键呈现
    for key in ("metric_value", "value", "check_value", "failed_rows", "count"):
        if key in diag:
            try:
                return float(diag[key])
            except (TypeError, ValueError):
                continue

    # row_count 场景：被测行数即结果
    if "dataset_rows_tested" in diag and _is_row_count(chk):
        try:
            return float(diag["dataset_rows_tested"])
        except (TypeError, ValueError):
            pass

    # 兜底：取第一个数值
    for val in diag.values():
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _is_row_count(chk) -> bool:
    inner = getattr(chk, "check", None)
    return str(_safe_get(inner, "type") or "") == "row_count"


def _soda_message(chk, metric, threshold) -> str:
    """拼装一行人类可读说明：优先展示语义化的度量明细（missing_count 等）。"""
    diag = _safe_get(chk, "diagnostic_metric_values") or {}
    parts: list[str] = []

    if isinstance(diag, dict) and diag:
        # 优先展示语义化键，避免与"度量="重复
        preferred = [
            "missing_count", "invalid_count", "duplicate_count",
            "failed_rows_count", "failed_rows_percent",
            "check_rows_tested", "dataset_rows_tested",
        ]
        shown = [(k, diag[k]) for k in preferred if k in diag]
        # 补上其余未被覆盖的键
        shown += [(k, v) for k, v in diag.items()
                  if k not in preferred and k != "metric_value"]
        parts.append("、".join(f"{k}={_fmt(v)}" for k, v in shown[:5]))
    elif metric is not None:
        parts.append(f"度量={_fmt(metric)}")

    if threshold is not None:
        parts.append(f"阈值={_fmt(threshold)}")
    return " | ".join(parts)


def _fmt(val) -> str:
    """统一数值展示：整数不带小数点，小数保留 4 位以内。"""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    return str(int(f)) if f == int(f) else f"{f:.4g}"


def _soda_level(chk) -> str:
    """
    从 Soda CheckResult 推断 pass / warn / fail。

    Soda 提供 outcome 枚举（PASSED / WARNED / FAILED / NOT_EVALUATED），
    同时也有 is_passed / is_warned / is_failed 布尔属性。
    """
    outcome = _safe_get(chk, "outcome")
    if outcome is not None:
        text = str(getattr(outcome, "value", outcome)).upper()
        if "PASS" in text:
            return "pass"
        if "WARN" in text:
            return "warn"
        if "FAIL" in text:
            return "fail"
        if "NOT_EVALUATED" in text or "EXCLUDED" in text:
            return "warn"          # 未评估按告警处理，提示人工确认

    if _safe_get(chk, "is_failed") is True:
        return "fail"
    if _safe_get(chk, "is_warned") is True:
        return "warn"
    if _safe_get(chk, "is_passed") is True:
        return "pass"
    return "pass"


# ---------------------------------------------------------------------------
# 降级内核：纯 pandas 复刻 Soda 的核心 check 语义
# ---------------------------------------------------------------------------
def _load_contract(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _run_fallback(con, contract: dict, contract_path: str, view_name: str) -> ContractRunResult:
    """
    内置校验内核。支持：row_count / missing / invalid / duplicate / failed_rows，
    以及 threshold（must_be / must_be_less_than / must_be_greater_than / must_be_between /
    must_not_be）+ metric: percent/count + additional（warn 分级）。

    语义与 Soda 对齐的关键点：
      - 阈值比较的是"检查自身的度量"（缺失数、无效数、重复数、行数），不是列值
      - schema 检查在降级内核中仅做"列存在性"校验（DuckDB 内无法完整复刻类型比对）
    """
    out = ContractRunResult(
        contract_name=os.path.basename(contract_path),
        dataset=str(contract.get("dataset", "")),
        engine="fallback",
    )

    try:
        df = con.execute(f'SELECT * FROM "{view_name}"').df()
    except Exception as exc:
        out.errors.append(
            f"视图 {view_name} 不存在或不可读：{exc}。"
            "请确认契约 dataset 最后一段与 DuckDB 视图名一致。"
        )
        return out

    total_rows = len(df)

    # ---------- 数据集级 checks ----------
    for item in contract.get("checks") or []:
        try:
            out.outcomes.append(_eval_dataset_check(item, df, total_rows))
        except Exception as exc:
            out.errors.append(f"数据集级检查执行失败 {item}：{exc}")

    # ---------- 列级 checks ----------
    for col_def in contract.get("columns") or []:
        col = col_def.get("name")
        if col is None:
            continue
        if col not in df.columns:
            out.outcomes.append(CheckOutcome(
                name=f"列 {col} 必须存在",
                level="fail",
                check_type="schema",
                column=col,
                message=f"视图中不存在该列，实际列为：{list(df.columns)}",
            ))
            continue
        for item in col_def.get("checks") or []:
            try:
                out.outcomes.append(_eval_column_check(item, df, col, total_rows))
            except Exception as exc:
                out.errors.append(f"列级检查执行失败 {col}/{item}：{exc}")

    return out


def _eval_dataset_check(item: dict, df: pd.DataFrame, total_rows: int) -> CheckOutcome:
    """数据集级：row_count / duplicate(多列) / schema / failed_rows。"""
    ctype = next(iter(item.keys()), "")
    cfg = item.get(ctype) or {}

    if ctype == "row_count":
        metric = float(total_rows)
        level = _apply_threshold(cfg.get("threshold"), metric, default_zero_ok=False)
        return CheckOutcome(
            name=cfg.get("name") or "行数检查",
            level=level, check_type="row_count", metric=metric,
            message=f"实际行数 {total_rows}",
        )

    if ctype == "duplicate":
        cols = cfg.get("columns") or []
        if not cols:
            return CheckOutcome("多列重复检查缺少 columns 配置", "fail", "duplicate",
                                message="配置错误")
        subset = [c for c in cols if c in df.columns]
        metric = float(df.duplicated(subset=subset, keep=False).sum()) if subset else 0.0
        level = _apply_threshold(cfg.get("threshold"), metric, default_zero_ok=True)
        return CheckOutcome(
            name=cfg.get("name") or f"多列重复检查 {cols}",
            level=level, check_type="duplicate", metric=metric,
            message=f"重复行数 {int(metric)}（按 {subset} 判定）",
        )

    if ctype == "schema":
        # 降级内核只做列存在性提示（完整性校验需真实 Soda）
        return CheckOutcome("schema 检查需真实 Soda 引擎", "warn", "schema",
                            message="内置内核无法完整复刻 schema/类型比对，已跳过")

    if ctype == "failed_rows":
        expr = cfg.get("expression")
        if not expr:
            return CheckOutcome(cfg.get("name") or "失败行检查", "warn", "failed_rows",
                                message="仅 expression 形式在内置内核中受支持")
        try:
            mask = df.eval(expr)
            metric = float(mask.sum())
        except Exception as exc:
            return CheckOutcome(cfg.get("name") or "失败行检查", "fail", "failed_rows",
                                message=f"表达式无法求值：{exc}")
        level = _apply_threshold(cfg.get("threshold"), metric, default_zero_ok=True)
        return CheckOutcome(
            name=cfg.get("name") or f"失败行检查：{expr}",
            level=level, check_type="failed_rows", metric=metric,
            message=f"失败行数 {int(metric)}",
        )

    return CheckOutcome(f"未支持的检查类型 {ctype}", "warn", ctype,
                        message="内置内核跳过")


def _eval_column_check(item: dict, df: pd.DataFrame, col: str, total_rows: int) -> CheckOutcome:
    """列级：missing / invalid / duplicate(单列) / aggregate。"""
    ctype = next(iter(item.keys()), "")
    cfg = item.get(ctype) or {}
    series = df[col]

    # ---------- missing ----------
    if ctype == "missing":
        blank = series.isna() | series.astype(str).str.strip().isin(
            ["", "nan", "None", "NaN", "NaT", "NULL"]
        )
        extra = cfg.get("missing_values") or []
        for val in extra:
            blank |= series.astype(str).str.strip() == str(val)
        metric = float(blank.sum())
        level = _apply_threshold(cfg.get("threshold"), metric, total_rows, default_zero_ok=True)
        return CheckOutcome(
            name=cfg.get("name") or f"{col} 缺失检查",
            level=level, check_type="missing", column=col, metric=metric,
            message=f"缺失 {int(metric)} / {total_rows} 行",
        )

    # ---------- invalid ----------
    if ctype == "invalid":
        if "valid_values" in cfg:
            valid = {str(v) for v in cfg["valid_values"]}
            bad = ~series.astype(str).isin(valid)
        elif "valid_format" in cfg:
            regex = cfg["valid_format"].get("regex", "")
            bad = ~series.astype(str).str.match(regex, na=False)
        elif "valid_min" in cfg or "valid_max" in cfg:
            # 注意：NaN 不计入"无效"。单边记录（bank_only / gl_only）天然没有日期差，
            # 其 date_diff_days 为空属于正常业务语义，不应被判定为非法值。
            # 空值的管控由 missing 检查负责，与 Soda 的语义保持一致（阈值比较的是度量本身）。
            non_null = series.notna() & ~series.astype(str).str.strip().isin(
                ["", "nan", "None", "NaN", "NaT", "NULL"]
            )
            num = pd.to_numeric(series, errors="coerce")
            bad = pd.Series(False, index=series.index)
            if "valid_min" in cfg:
                bad |= non_null & (num < float(cfg["valid_min"]))
            if "valid_max" in cfg:
                bad |= non_null & (num > float(cfg["valid_max"]))
        elif "invalid_values" in cfg:
            bad = series.astype(str).isin({str(v) for v in cfg["invalid_values"]})
        else:
            return CheckOutcome(cfg.get("name") or f"{col} 合法性检查", "warn", "invalid",
                                column=col, message="未识别的有效性配置，已跳过")
        metric = float(bad.sum())
        level = _apply_threshold(cfg.get("threshold"), metric, total_rows, default_zero_ok=True)
        return CheckOutcome(
            name=cfg.get("name") or f"{col} 合法性检查",
            level=level, check_type="invalid", column=col, metric=metric,
            message=f"无效 {int(metric)} / {total_rows} 行",
        )

    # ---------- duplicate（单列） ----------
    if ctype == "duplicate":
        metric = float(series.duplicated(keep=False).sum())
        level = _apply_threshold(cfg.get("threshold"), metric, total_rows, default_zero_ok=True)
        return CheckOutcome(
            name=cfg.get("name") or f"{col} 唯一性检查",
            level=level, check_type="duplicate", column=col, metric=metric,
            message=f"重复 {int(metric)} 行",
        )

    # ---------- aggregate ----------
    if ctype == "aggregate":
        func = (cfg.get("function") or "sum").lower()
        num = pd.to_numeric(series, errors="coerce")
        metric = float(getattr(num, func)()) if hasattr(num, func) else float(num.sum())
        level = _apply_threshold(cfg.get("threshold"), metric, default_zero_ok=False)
        return CheckOutcome(
            name=cfg.get("name") or f"{col} 聚合检查({func})",
            level=level, check_type="aggregate", column=col, metric=round(metric, 4),
            message=f"{func} = {round(metric, 4)}",
        )

    return CheckOutcome(f"未支持的检查类型 {ctype}", "warn", ctype, column=col,
                        message="内置内核跳过")


def _apply_threshold(
    threshold: Optional[dict],
    metric: float,
    total_rows: Optional[int] = None,
    default_zero_ok: bool = True,
) -> str:
    """
    按 Soda 阈值语义判定等级。

    - metric: count（默认）| percent —— percent 时先把度量换算成百分比
    - additional: 第二阈值，配 level: warn，实现"先告警后失败"
    - 无 threshold 时：default_zero_ok=True 表示"度量须为 0"，否则"须 > 0"
    """
    if not threshold:
        if default_zero_ok:
            return "pass" if metric == 0 else "fail"
        return "pass" if metric > 0 else "fail"

    is_percent = str(threshold.get("metric", "count")).lower() == "percent"
    denom = float(total_rows) if total_rows else 0.0

    def to_metric(cfg: dict) -> float:
        if is_percent and denom > 0:
            return metric / denom * 100.0
        return metric

    # 主阈值（fail）
    fail_level = "pass" if _meets(threshold, to_metric(threshold)) else "fail"

    # additional 阈值（warn）
    add = threshold.get("additional")
    if add:
        add_level_raw = _meets(add, to_metric(add))
        add_level = str(add.get("level", "warn")).lower()
        if not add_level_raw:
            # 突破 additional：若 additional 是更严格边界，则产生 warn
            return "warn" if add_level == "warn" else "fail"
    return fail_level


def _meets(cfg: dict, value: float) -> bool:
    """判断度量是否满足阈值内的比较条件。"""
    eps = 1e-9
    if "must_be" in cfg:
        return abs(value - float(cfg["must_be"])) < eps
    if "must_not_be" in cfg:
        return abs(value - float(cfg["must_not_be"])) >= eps
    if "must_be_greater_than" in cfg:
        return value > float(cfg["must_be_greater_than"])
    if "must_be_greater_than_or_equal" in cfg:
        return value >= float(cfg["must_be_greater_than_or_equal"])
    if "must_be_less_than" in cfg:
        return value < float(cfg["must_be_less_than"])
    if "must_be_less_than_or_equal" in cfg:
        return value <= float(cfg["must_be_less_than_or_equal"])
    if "must_be_between" in cfg:
        rng = cfg["must_be_between"]
        lo = rng.get("greater_than", rng.get("greater_than_or_equal"))
        hi = rng.get("less_than", rng.get("less_than_or_equal"))
        ok = True
        if lo is not None:
            ok &= value > float(lo) if "greater_than" in rng else value >= float(lo)
        if hi is not None:
            ok &= value < float(hi) if "less_than" in rng else value <= float(hi)
        return ok
    if "must_be_not_between" in cfg:
        return not _meets({"must_be_between": cfg["must_be_not_between"]}, value)
    return True


# ---------------------------------------------------------------------------
# 工具：引擎状态说明
# ---------------------------------------------------------------------------
def engine_status() -> dict:
    """返回当前校验引擎的状态，供 UI 展示。"""
    return {
        "soda_available": SODA_AVAILABLE,
        "soda_error": SODA_IMPORT_ERROR,
        "label": "Soda Core v4 (soda-duckdb)" if SODA_AVAILABLE else "内置 pandas 内核（降级）",
    }
