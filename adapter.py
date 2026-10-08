"""
adapter.py —— 对账结果 → DuckDB 结果表 → 契约校验 的衔接层
============================================================
为什么需要这一层：
  Bank-Recon 输出的是"行级明细"（哪几笔没对上）
  Soda 输出的是"检查级判定"（是否达标）
  两者语义不通约。本模块把行级明细落成一张名为 reconciled 的结果表，
  再交给契约做阈值判定，从而在同一个应用里串起两条链路。

对外主入口：
    land_reconciled(con, result) -> pd.DataFrame   # 落表并返回结果表
    audit_reconciled(con, result, tolerance)       # 落表 + 生成/加载契约 + 校验
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import yaml

import config
import loader
import soda_runner

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
VIEW_RECONCILED = "reconciled"            # 结果表（视图）名，须与输出契约 dataset 末段一致
VIEW_BANK = "bank_statement"
VIEW_GL = "gl_ledger"

CONTRACT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "contracts", "output_reconciled.yml")
CONTRACT_INPUT_BANK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "contracts", "input_bank_statement.yml")
CONTRACT_INPUT_GL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "contracts", "input_gl_ledger.yml")


@dataclass
class Tolerance:
    """审计重要性水平配置 —— 决定输出契约的严格程度。"""

    max_bank_only: int = config.MAX_BANK_ONLY_ROWS
    max_gl_only: int = config.MAX_GL_ONLY_ROWS
    max_amount_diff_missing: int = config.MAX_AMOUNT_DIFF_MISSING
    min_match_rate_percent: float = config.MIN_MATCH_RATE_PERCENT

    def to_dict(self) -> dict:
        return {
            "max_bank_only": self.max_bank_only,
            "max_gl_only": self.max_gl_only,
            "max_amount_diff_missing": self.max_amount_diff_missing,
            "min_match_rate_percent": self.min_match_rate_percent,
        }


# ---------------------------------------------------------------------------
# 落表
# ---------------------------------------------------------------------------
def land_reconciled(con, reconciled: pd.DataFrame) -> pd.DataFrame:
    """
    把对账结果表注册为 DuckDB 视图 reconciled。

    先做输出侧的类型加固，保证契约可校验：
      - amount_diff 转数值并补 0（单边记录无差异概念）
      - date_diff_days 转数值（空值保留为空，由契约的 valid_min 约束非负）
      - match_status 转字符串
    """
    df = reconciled.copy()

    if "amount_diff" in df.columns:
        df["amount_diff"] = pd.to_numeric(df["amount_diff"], errors="coerce").fillna(0.0)
    if "date_diff_days" in df.columns:
        df["date_diff_days"] = pd.to_numeric(df["date_diff_days"], errors="coerce")
    if "match_status" in df.columns:
        df["match_status"] = df["match_status"].astype(str)

    # 日期列统一为字符串，避免 DuckDB/Soda 两侧类型推断不一致
    for col in ("bank_date", "gl_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")

    loader.register_dataframe_view(con, VIEW_RECONCILED, df)
    return df


# ---------------------------------------------------------------------------
# 输出契约的动态生成（把容差写进阈值）
# ---------------------------------------------------------------------------
def build_output_contract(tolerance: Tolerance) -> dict:
    """
    按容差参数生成输出契约。

    结构与 contracts/output_reconciled.yml 一致（dataset → columns → checks），
    区别是阈值由 tolerance 动态注入，便于 UI 上调节严格程度。
    """
    return {
        "dataset": f"audit_duckdb/memory/main/{VIEW_RECONCILED}",
        "checks": [
            {
                "row_count": {
                    "name": "对账结果不能为空",
                    "threshold": {"must_be_greater_than": 0},
                }
            },
            # 未达账项（企业未记）数量控制
            {
                "failed_rows": {
                    "name": f"bank_only 不超过 {tolerance.max_bank_only} 笔",
                    # Soda 要求：同类型检查出现多次时必须给出唯一 qualifier，
                    # 否则会报 Duplicate identity 导致整份契约校验失败。
                    "qualifier": "bank_only_count",
                    "expression": "match_status == 'bank_only'",
                    "threshold": {"must_be_less_than_or_equal": tolerance.max_bank_only},
                }
            },
            # 未达账项（银行未记）数量控制
            {
                "failed_rows": {
                    "name": f"gl_only 不超过 {tolerance.max_gl_only} 笔",
                    "qualifier": "gl_only_count",
                    "expression": "match_status == 'gl_only'",
                    "threshold": {"must_be_less_than_or_equal": tolerance.max_gl_only},
                }
            },
        ],
        "columns": [
            {
                "name": "match_status",
                "data_type": "varchar",
                "checks": [
                    {
                        "missing": {
                            "name": "对账状态不允许缺失",
                            "threshold": {"must_be": 0},
                        }
                    },
                    {
                        "invalid": {
                            "name": "对账状态取值必须合法",
                            "valid_values": config.VALID_MATCH_STATUSES,
                        }
                    },
                ],
            },
            {
                "name": "amount_diff",
                "checks": [
                    {
                        "missing": {
                            "name": f"差异金额缺失不超过 {tolerance.max_amount_diff_missing} 行",
                            "threshold": {
                                "must_be_less_than_or_equal": tolerance.max_amount_diff_missing
                            },
                        }
                    }
                ],
            },
            {
                "name": "date_diff_days",
                "checks": [
                    {
                        "invalid": {
                            "name": "日期差必须非负",
                            "valid_min": 0,
                        }
                    }
                ],
            },
            {
                "name": "reference",
                "checks": [
                    {
                        "missing": {
                            "name": "参考号不允许缺失",
                            "threshold": {"must_be": 0},
                        }
                    }
                ],
            },
        ],
    }


def write_output_contract(contract: dict, path: Optional[str] = None) -> str:
    """把动态契约写到磁盘（Soda 需要文件路径）。"""
    path = path or os.path.join(
        os.path.dirname(CONTRACT_OUTPUT), f"output_reconciled_dynamic.yml"
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(contract, fh, allow_unicode=True, sort_keys=False)
    return path


# ---------------------------------------------------------------------------
# 一键：落表 + 生成契约 + 校验
# ---------------------------------------------------------------------------
@dataclass
class AuditOutcome:
    """对账结果的审计结论。"""

    reconciled: pd.DataFrame = field(default_factory=pd.DataFrame)
    result: Optional[soda_runner.ContractRunResult] = None
    contract_path: str = ""
    tolerance: Tolerance = field(default_factory=Tolerance)

    @property
    def is_ok(self) -> bool:
        return bool(self.result and self.result.is_ok)


def audit_reconciled(
    con,
    reconciled: pd.DataFrame,
    tolerance: Optional[Tolerance] = None,
    *,
    use_dynamic_contract: bool = True,
) -> AuditOutcome:
    """
    对账结果落表并执行输出契约校验。

    use_dynamic_contract=True  → 按 tolerance 生成契约（UI 可调）
    use_dynamic_contract=False → 使用 contracts/output_reconciled.yml（固定契约）
    """
    tolerance = tolerance or Tolerance()
    df = land_reconciled(con, reconciled)

    if use_dynamic_contract:
        contract = build_output_contract(tolerance)
        path = write_output_contract(contract)
    else:
        path = CONTRACT_OUTPUT

    result = soda_runner.run_contract(
        con, path, views=[VIEW_RECONCILED, VIEW_BANK, VIEW_GL]
    )
    return AuditOutcome(reconciled=df, result=result, contract_path=path, tolerance=tolerance)


# ---------------------------------------------------------------------------
# 输入契约校验（上传阶段）
# ---------------------------------------------------------------------------
def audit_inputs(con) -> dict[str, soda_runner.ContractRunResult]:
    """
    校验两张输入表是否满足输入契约（date/amount/reference 无缺失）。

    返回 {"bank_statement": result, "gl_ledger": result}
    """
    return {
        VIEW_BANK: soda_runner.run_contract(
            con, CONTRACT_INPUT_BANK, views=[VIEW_BANK, VIEW_GL]
        ),
        VIEW_GL: soda_runner.run_contract(
            con, CONTRACT_INPUT_GL, views=[VIEW_BANK, VIEW_GL]
        ),
    }


# ---------------------------------------------------------------------------
# 汇总：把对账统计 + 契约判定合成审计结论
# ---------------------------------------------------------------------------
def summarize(result_recon, audit: Optional[AuditOutcome]) -> pd.DataFrame:
    """
    生成审计结论汇总表，供 UI 展示与导出。

    注意：所有值统一转为字符串，避免同一列混入 '80.0%' 与数字导致
          Streamlit/Arrow 序列化失败（ArrowInvalid: tried to convert to int64）。
    """
    stats = result_recon.stats

    def s(v) -> str:
        return "" if v is None else str(v)

    rows = [
        ("对账单记录数", s(stats.get("bank_rows", 0))),
        ("总账记录数", s(stats.get("gl_rows", 0))),
        ("✓ 匹配成功", s(stats.get("matched", 0))),
        ("🏦 仅银行有（未达账项）", s(stats.get("bank_only", 0))),
        ("📘 仅总账有（未达账项）", s(stats.get("gl_only", 0))),
        ("⚠️ 金额/日期不符", s(stats.get("mismatch", 0))),
        ("🔁 重复记录", s(stats.get("duplicates", 0))),
        ("匹配率", f"{result_recon.match_rate}%"),
        ("日期容差（天）", s(stats.get("date_tolerance", 0))),
        ("金额容差", s(stats.get("amount_tolerance", 0))),
        ("方向策略", s(stats.get("amount_mode", ""))),
    ]
    if audit and audit.result:
        counts = audit.result.counts
        rows += [
            ("契约校验引擎", audit.result.engine),
            ("契约通过", s(counts["pass"])),
            ("契约告警", s(counts["warn"])),
            ("契约失败", s(counts["fail"])),
            ("审计结论", "通过" if audit.is_ok else "需人工复核"),
        ]
    return pd.DataFrame(rows, columns=["项目", "值"])
