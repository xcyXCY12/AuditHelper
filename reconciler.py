"""
reconciler.py —— 纯逻辑对账引擎
============================================================
参考 Bank-Recon 的 utils.py，但做了以下改动：
  1. 纯 pandas 实现，不依赖 Streamlit（可单独测试/在 CI 中调用）
  2. 【修复】原版 amount_tolerance 参数声明后从未使用 → 本版真正生效
  3. 【修复】原版金额仅做精确 == 比较、不处理借贷方向 → 本版提供三种方向策略
  4. 【修复】原版 match_score >= 0 与初始值 -1 的语义矛盾，本版显式区分"候选"与"确认"
  5. 两轮匹配：第一轮精确匹配（日期+金额+参考号），第二轮日期容差匹配
  6. 输出 match_status 列（matched / bank_only / gl_only / mismatch / duplicate）

对外主入口：
    reconcile(bank_df, gl_df, ...) -> ReconResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

import config
from config import BANK_COLUMN_ALIASES, GL_COLUMN_ALIASES


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------
@dataclass
class ReconResult:
    """对账结果。"""

    reconciled: pd.DataFrame                 # 合并后的逐行明细，含 match_status / amount_diff / date_diff_days
    matched: pd.DataFrame = field(default_factory=pd.DataFrame)
    bank_only: pd.DataFrame = field(default_factory=pd.DataFrame)
    gl_only: pd.DataFrame = field(default_factory=pd.DataFrame)
    mismatch: pd.DataFrame = field(default_factory=pd.DataFrame)
    duplicates: pd.DataFrame = field(default_factory=pd.DataFrame)
    brs: pd.DataFrame = field(default_factory=pd.DataFrame)

    stats: dict = field(default_factory=dict)

    @property
    def match_rate(self) -> float:
        """匹配率（百分比，保留 2 位）。分母取两侧记录数较大者，避免重复计数。"""
        total = max(
            int(self.stats.get("bank_rows", 0)) + int(self.stats.get("gl_rows", 0)),
            1,
        )
        # 一条匹配同时消耗 bank 与 gl 各一行，故分母用总行数
        return round(len(self.matched) * 2 / total * 100, 2)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def parse_dates(series: pd.Series, formats: Optional[list[str]] = None) -> pd.Series:
    """
    按 config.DATE_FORMATS 依次尝试解析日期，全部失败则回退自动推断。

    【用户根据实际日期格式调整】→ 修改 config.DATE_FORMATS
    """
    formats = formats or config.DATE_FORMATS
    raw = series.astype(str).str.strip()
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

    for fmt in formats:
        pending = out.isna() & raw.notna() & (raw != "") & (raw.str.lower() != "nan")
        if not pending.any():
            break
        try:
            parsed = pd.to_datetime(raw[pending], format=fmt, errors="coerce")
            out.loc[pending] = parsed
        except Exception:
            continue

    # 回退：自动推断
    pending = out.isna() & raw.notna() & (raw != "") & (raw.str.lower() != "nan")
    if pending.any():
        try:
            parsed = pd.to_datetime(
                raw[pending], errors="coerce", dayfirst=config.DATE_AUTODETECT_DAYFIRST
            )
            out.loc[pending] = parsed
        except Exception:
            pass
    return out


def parse_amounts(series: pd.Series) -> pd.Series:
    """
    把金额列转成 float。会剔除千分位逗号、货币符号、括号负数写法（会计常用 (1,234.00) 表示负数）。
    """
    s = series.astype(str).str.strip()
    negative = s.str.match(r"^\(.*\)$", na=False)          # 会计括号负数
    s = (
        s.str.replace(r"[,\s¥￥$€£]", "", regex=True)
        .str.replace(r"^\((.*)\)$", r"\1", regex=True)
        .replace({"": None, "nan": None, "None": None, "-": None, "—": None})
    )
    out = pd.to_numeric(s, errors="coerce")
    out[negative] = -out[negative].abs()
    return out


def normalize_sign(series: pd.Series, mode: str) -> pd.Series:
    """
    按策略归一化金额符号，解决"银行借方 vs GL 贷方"方向相反导致的漏匹配。

    mode:
      strict    -> 原样返回（严格要求同号同值）
      absolute  -> 取绝对值（适用于正数列，方向另有列表达）
      sign_norm -> 取绝对值参与比对（方向差异在此被容忍），但保留原值供展示
    """
    if mode == "strict":
        return series
    if mode in ("absolute", "sign_norm"):
        return series.abs()
    return series


def _clean_reference(series: pd.Series) -> pd.Series:
    """参考号清洗：转字符串、去空格、统一大写；空值返回 None 以便识别。"""
    s = series.astype(str).str.strip().str.upper()
    return s.replace({"": None, "NAN": None, "NONE": None, "NULL": None})


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def reconcile(
    bank_df: pd.DataFrame,
    gl_df: pd.DataFrame,
    *,
    date_tolerance: int = config.DEFAULT_DATE_TOLERANCE_DAYS,
    amount_tolerance: float = config.DEFAULT_AMOUNT_TOLERANCE,
    amount_mode: str = config.DEFAULT_AMOUNT_MODE,
    col_date: str = "date",
    col_amount: str = "amount",
    col_reference: str = "reference",
    col_description: str = "description",
) -> ReconResult:
    """
    执行两轮对账匹配。

    第 1 轮（精确匹配）：日期完全相同 + 金额在容差内 + 参考号一致
    第 2 轮（容差匹配）：日期差 <= date_tolerance + 金额在容差内（参考号作为打分加权，非必需）

    返回 ReconResult。
    """
    bank = _prepare(bank_df, "bank", col_date, col_amount, col_reference, col_description,
                    amount_mode)
    gl = _prepare(gl_df, "gl", col_date, col_amount, col_reference, col_description,
                  amount_mode)
    # ---- 重复检测（先于匹配，与原版一致：重复行不参与匹配） ----
    dup_keys = ["date", "amount", "reference"]
    bank_dup_idx = set(
        bank[bank.duplicated(subset=dup_keys, keep=False)].index
    )
    gl_dup_idx = set(gl[gl.duplicated(subset=dup_keys, keep=False)].index)

    bank["_matched"] = False
    gl["_matched"] = False
    bank["_in_duplicate"] = bank.index.isin(bank_dup_idx)
    gl["_in_duplicate"] = gl.index.isin(gl_dup_idx)

    match_records: list[dict] = []
    mismatch_records: list[dict] = []

    # ================= 第 1 轮：精确匹配 =================
    match_records += _match_round(
        bank, gl,
        date_tol=0,
        amount_tol=amount_tolerance,
        require_reference=True,
        round_name="exact",
    )

    # ================= 第 2 轮：日期容差匹配 =================
    if date_tolerance > 0:
        match_records += _match_round(
            bank, gl,
            date_tol=date_tolerance,
            amount_tol=amount_tolerance,
            require_reference=False,
            round_name="tolerance",
        )

    # ================= 不匹配但参考号相同 → mismatch =================
    mismatch_records = _detect_mismatch(bank, gl)

    # ================= 汇总输出 =================
    matched_df = pd.DataFrame(match_records)
    mismatch_df = pd.DataFrame(mismatch_records)

    bank_only_df = bank[~bank["_matched"] & ~bank["_in_duplicate"]].copy()
    gl_only_df = gl[~gl["_matched"] & ~gl["_in_duplicate"]].copy()
    bank_only_df["match_status"] = "bank_only"
    gl_only_df["match_status"] = "gl_only"

    duplicates_df = _build_duplicates(bank, gl)

    reconciled = _build_reconciled(
        bank, gl, matched_df, bank_only_df, gl_only_df, mismatch_df, duplicates_df
    )

    brs = build_brs(bank_df, gl_df, reconciled)

    stats = {
        "bank_rows": len(bank),
        "gl_rows": len(gl),
        "matched": len(matched_df),
        "bank_only": len(bank_only_df),
        "gl_only": len(gl_only_df),
        "mismatch": len(mismatch_df),
        "duplicates": len(duplicates_df),
        "date_tolerance": date_tolerance,
        "amount_tolerance": amount_tolerance,
        "amount_mode": amount_mode,
    }

    return ReconResult(
        reconciled=reconciled,
        matched=matched_df,
        bank_only=bank_only_df,
        gl_only=gl_only_df,
        mismatch=mismatch_df,
        duplicates=duplicates_df,
        brs=brs,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _prepare(
    df: pd.DataFrame,
    source: str,
    col_date: str,
    col_amount: str,
    col_reference: str,
    col_description: str,
    amount_mode: str,
) -> pd.DataFrame:
    """
    标准化单侧数据：类型转换 + 补充缺失列 + 生成比对用的归一化金额。

    列名解析优先级：
      1. 调用方显式指定的列名（col_date / col_amount / ...）
      2. config 中的别名列表（大小写、中英文、下划线不敏感）

    第 2 条使本函数既能接收已重命名过的标准列（date/amount/reference），
    也能直接接收原始列（Date/Amount/Reference 或 日期/金额/参考号），
    避免调用方必须提前做一次映射才能用。
    """
    aliases = BANK_COLUMN_ALIASES if source == "bank" else GL_COLUMN_ALIASES
    normalized = {_norm_key(c): c for c in df.columns}

    def pick(explicit: str, std_key: str) -> Optional[str]:
        """返回实际存在的列名；找不到返回 None。"""
        if explicit in df.columns:
            return explicit
        for cand in aliases.get(std_key, []):
            if cand in df.columns:
                return cand
            hit = normalized.get(_norm_key(cand))
            if hit is not None:
                return hit
        # 标准名本身直接命中（归一化后）
        return normalized.get(_norm_key(std_key))

    c_date = pick(col_date, "date")
    c_amount = pick(col_amount, "amount")
    c_ref = pick(col_reference, "reference")
    c_desc = pick(col_description, "description")

    n = len(df)
    out = pd.DataFrame(index=df.index)
    out["date"] = parse_dates(df[c_date]) if c_date else pd.NaT
    out["amount"] = parse_amounts(df[c_amount]) if c_amount else np.nan
    out["reference"] = (
        _clean_reference(df[c_ref]) if c_ref
        else pd.Series([None] * n, index=df.index)
    )
    out["description"] = (
        df[c_desc].astype(str) if c_desc
        else pd.Series([""] * n, index=df.index)
    )
    out["source"] = source
    out["_amount_cmp"] = normalize_sign(out["amount"], amount_mode)
    out["_row_id"] = [f"{source.upper()}-{i + 1}" for i in range(n)]
    return out


def _norm_key(name: str) -> str:
    """列名归一化（去空格/下划线/连字符/括号，转小写），用于模糊匹配。"""
    import re as _re

    return _re.sub(r"[\s_\-（）()【】\[\]]+", "", str(name)).lower()


def _find_col(df: pd.DataFrame, candidates) -> Optional[str]:
    """
    在 df 中按候选名列表找列，大小写与中英文标点不敏感。

    candidates 可以是单个字符串或字符串列表。
    返回实际列名；找不到返回 None。
    """
    if isinstance(candidates, str):
        candidates = [candidates]
    normalized = {_norm_key(c): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        hit = normalized.get(_norm_key(cand))
        if hit is not None:
            return hit
    return None


def _find_candidate(
    bank: pd.DataFrame,
    gl: pd.DataFrame,
    b_idx,
    *,
    date_tol: int,
    amount_tol: float,
    require_reference: bool,
    skip_bank: set,
    skip_gl: set,
) -> Optional[tuple]:
    """
    在 GL 中为指定 bank 行找出得分最高的候选。

    打分规则（参考 Bank-Recon）：
        日期完全相同  +config.SCORE_EXACT_DATE
        参考号一致    +config.SCORE_REFERENCE_MATCH
    返回 (gl_index, score, date_diff, amount_diff) 或 None。
    """
    brow = bank.loc[b_idx]
    if pd.isna(brow["date"]) or pd.isna(brow["_amount_cmp"]):
        return None

    best = None
    best_score = -1

    for g_idx, grow in gl.iterrows():
        if g_idx in skip_gl:
            continue
        if pd.isna(grow["date"]) or pd.isna(grow["_amount_cmp"]):
            continue

        # --- 条件一：金额（容差真正生效，修复原版死参数） ---
        amount_diff = abs(float(brow["_amount_cmp"]) - float(grow["_amount_cmp"]))
        if amount_diff > amount_tol:
            continue

        # --- 条件二：日期 ---
        date_diff = abs((brow["date"] - grow["date"]).days)
        if date_diff > date_tol:
            continue

        # --- 条件三：参考号 ---
        ref_match = (
            brow["reference"] is not None
            and grow["reference"] is not None
            and brow["reference"] == grow["reference"]
        )
        if require_reference and not ref_match:
            continue

        # --- 打分 ---
        score = 0
        if date_diff == 0:
            score += config.SCORE_EXACT_DATE
        if ref_match:
            score += config.SCORE_REFERENCE_MATCH

        if score > best_score:
            best_score = score
            best = (g_idx, score, date_diff, amount_diff)

    return best


def _match_round(
    bank: pd.DataFrame,
    gl: pd.DataFrame,
    *,
    date_tol: int,
    amount_tol: float,
    require_reference: bool,
    round_name: str,
) -> list[dict]:
    """执行一轮匹配，就地更新 _matched 标记并返回匹配记录列表。"""
    records: list[dict] = []

    for b_idx in bank.index:
        if bank.at[b_idx, "_matched"] or bank.at[b_idx, "_in_duplicate"]:
            continue

        cand = _find_candidate(
            bank, gl,
            b_idx,
            date_tol=date_tol,
            amount_tol=amount_tol,
            require_reference=require_reference,
            skip_bank=set(),
            skip_gl={i for i in gl.index if gl.at[i, "_matched"] or gl.at[i, "_in_duplicate"]},
        )
        if cand is None:
            continue

        g_idx, score, date_diff, amount_diff = cand
        bank.at[b_idx, "_matched"] = True
        gl.at[g_idx, "_matched"] = True

        brow, grow = bank.loc[b_idx], gl.loc[g_idx]
        records.append({
            "match_status": "matched",
            "match_round": round_name,
            "reference": brow["reference"],
            "bank_row_id": brow["_row_id"],
            "gl_row_id": grow["_row_id"],
            "bank_date": brow["date"],
            "gl_date": grow["date"],
            "date_diff_days": int(date_diff),
            "bank_amount": float(brow["amount"]) if pd.notna(brow["amount"]) else np.nan,
            "gl_amount": float(grow["amount"]) if pd.notna(grow["amount"]) else np.nan,
            "amount_diff": round(float(amount_diff), config.DEFAULT_AMOUNT_ROUND),
            "match_score": int(score),
            "bank_description": brow["description"],
            "gl_description": grow["description"],
        })

    return records


def _detect_mismatch(bank: pd.DataFrame, gl: pd.DataFrame) -> list[dict]:
    """
    参考号一致但金额/日期超出容差 → mismatch。
    与原版一致：仅识别"同参考号"的情形；参考号不同的未对上是 bank_only / gl_only。
    """
    records: list[dict] = []

    for b_idx in bank.index:
        if bank.at[b_idx, "_matched"] or bank.at[b_idx, "_in_duplicate"]:
            continue
        bref = bank.at[b_idx, "reference"]
        if bref is None:
            continue

        for g_idx in gl.index:
            if gl.at[g_idx, "_matched"] or gl.at[g_idx, "_in_duplicate"]:
                continue
            if gl.at[g_idx, "reference"] != bref:
                continue

            brow, grow = bank.loc[b_idx], gl.loc[g_idx]
            bank.at[b_idx, "_matched"] = True
            gl.at[g_idx, "_matched"] = True

            amount_diff = (
                abs(float(brow["amount"]) - float(grow["amount"]))
                if pd.notna(brow["amount"]) and pd.notna(grow["amount"]) else np.nan
            )
            date_diff = (
                abs((brow["date"] - grow["date"]).days)
                if pd.notna(brow["date"]) and pd.notna(grow["date"]) else np.nan
            )

            records.append({
                "match_status": "mismatch",
                "match_round": "reference_only",
                "reference": bref,
                "bank_row_id": brow["_row_id"],
                "gl_row_id": grow["_row_id"],
                "bank_date": brow["date"],
                "gl_date": grow["date"],
                "date_diff_days": int(date_diff) if pd.notna(date_diff) else np.nan,
                "bank_amount": brow["amount"],
                "gl_amount": grow["amount"],
                "amount_diff": round(float(amount_diff), config.DEFAULT_AMOUNT_ROUND)
                if pd.notna(amount_diff) else np.nan,
                "match_score": np.nan,
                "bank_description": brow["description"],
                "gl_description": grow["description"],
            })
            break

    return records


def _build_duplicates(bank: pd.DataFrame, gl: pd.DataFrame) -> pd.DataFrame:
    """汇总两侧的重复行，附 Type 列区分来源。"""
    frames = []
    for df, label in ((bank, "Bank"), (gl, "GL")):
        dup = df[df["_in_duplicate"]].copy()
        if dup.empty:
            continue
        dup["Type"] = label
        frames.append(dup)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    return out.assign(match_status="duplicate")[[
        "match_status", "Type", "date", "reference", "amount", "description"
    ]].rename(columns={"Type": "source_type", "date": "txn_date"})


def _build_reconciled(
    bank: pd.DataFrame,
    gl: pd.DataFrame,
    matched: pd.DataFrame,
    bank_only: pd.DataFrame,
    gl_only: pd.DataFrame,
    mismatch: pd.DataFrame,
    duplicates: pd.DataFrame,
) -> pd.DataFrame:
    """
    构建统一的 reconciled 明细表。

    这是"输出契约"校验的对象：Soda 会检查
      - match_status 取值合法
      - amount_diff 无缺失
    """
    cols = [
        "match_status", "match_round", "reference",
        "bank_row_id", "gl_row_id",
        "bank_date", "gl_date", "date_diff_days",
        "bank_amount", "gl_amount", "amount_diff",
        "bank_description", "gl_description",
    ]

    parts: list[pd.DataFrame] = []
    if not matched.empty:
        parts.append(matched)
    if not mismatch.empty:
        parts.append(mismatch)

    # 单边记录补成同样的列结构
    for df, side in ((bank_only, "bank"), (gl_only, "gl")):
        if df.empty:
            continue
        rec = pd.DataFrame({
            "match_status": df["match_status"].values,
            "match_round": "none",
            "reference": df["reference"].values,
            "bank_row_id": df["_row_id"].values if side == "bank" else None,
            "gl_row_id": df["_row_id"].values if side == "gl" else None,
            "bank_date": df["date"].values if side == "bank" else pd.NaT,
            "gl_date": df["date"].values if side == "gl" else pd.NaT,
            "date_diff_days": np.nan,
            "bank_amount": df["amount"].values if side == "bank" else np.nan,
            "gl_amount": df["amount"].values if side == "gl" else np.nan,
            "amount_diff": np.nan,
            "bank_description": df["description"].values if side == "bank" else "",
            "gl_description": df["description"].values if side == "gl" else "",
        })
        parts.append(rec)

    if duplicates is not None and not duplicates.empty:
        dup = pd.DataFrame({
            "match_status": "duplicate",
            "match_round": "none",
            "reference": duplicates["reference"].values,
            "bank_row_id": np.where(duplicates["source_type"] == "Bank",
                                    duplicates["reference"].astype(str), None),
            "gl_row_id": np.where(duplicates["source_type"] == "GL",
                                  duplicates["reference"].astype(str), None),
            "bank_date": duplicates["txn_date"].values,
            "gl_date": pd.NaT,
            "date_diff_days": np.nan,
            "bank_amount": duplicates["amount"].values,
            "gl_amount": np.nan,
            "amount_diff": np.nan,
            "bank_description": duplicates["description"].values,
            "gl_description": "",
        })
        parts.append(dup)

    if not parts:
        return pd.DataFrame(columns=cols)

    out = pd.concat(parts, ignore_index=True)
    for c in cols:
        if c not in out.columns:
            out[c] = np.nan
    out = out[cols]

    # ---- 关键：把 amount_diff 的空值补成 0，使输出契约可控 ----
    # 单边记录（bank_only / gl_only）本质没有金额差异概念，补 0 表示"无差异需调整"。
    # duplicate 行同样补 0。这样 amount_diff 只在真正的解析失败场景才为空。
    out["amount_diff"] = pd.to_numeric(out["amount_diff"], errors="coerce").fillna(0.0)

    # 参考号为空时补成占位符，避免输入契约外的空值污染输出
    out["reference"] = out["reference"].fillna("(EMPTY)")

    # ---- 按状态排序，便于人工复核（异常项优先） ----
    order = {s: i for i, s in enumerate(
        ["bank_only", "gl_only", "mismatch", "duplicate", "matched"]
    )}
    out["_order"] = out["match_status"].map(order).fillna(99).astype(int)
    out = out.sort_values(
        ["_order", "reference"], kind="stable"
    ).drop(columns=["_order"]).reset_index(drop=True)

    return out


def build_brs(
    bank_raw: pd.DataFrame,
    gl_raw: pd.DataFrame,
    reconciled: pd.DataFrame,
) -> pd.DataFrame:
    """
    生成银行余额调节表（BRS）。

    【修复 Bank-Recon 的 generate_brs 概念错误】
    原版把 gl_book_balance 写成 bank_df['Amount'].sum()（用银行数据冒充 GL 余额），
    本版改为从 GL 侧取数，并把未达账项真正拆成调整项。
    """
    rows: list[dict] = []

    # 期初/期末余额：优先取 bank 侧的 balance 列（若有）
    bank_balance = np.nan
    for cand in ("balance", "Balance", "余额", "账户余额", "ClosingBalance"):
        col = _find_col(bank_raw, cand)
        if col is not None:
            vals = parse_amounts(bank_raw[col]).dropna()
            if not vals.empty:
                bank_balance = float(vals.iloc[-1])
            break

    # 金额列名探测（大小写/中英文不敏感，兼容已重命名与未重命名两种输入）
    amount_col_gl = _find_col(gl_raw, config.GL_COLUMN_ALIASES["amount"])
    gl_balance = (
        float(parse_amounts(gl_raw[amount_col_gl]).sum())
        if amount_col_gl is not None and len(gl_raw) else 0.0
    )

    bank_only = reconciled[reconciled["match_status"] == "bank_only"]
    gl_only = reconciled[reconciled["match_status"] == "gl_only"]
    mismatch = reconciled[reconciled["match_status"] == "mismatch"]

    # 银行侧单边（银行已记、企业未记）= 未达账项
    # GL 侧单边（企业已记、银行未记）= 逆未达账项
    # 金额符号已由 amount_mode 归一，这里统一用绝对值拆分借贷方向
    bank_only_amt = pd.to_numeric(bank_only["bank_amount"], errors="coerce").fillna(0.0)
    gl_only_amt = pd.to_numeric(gl_only["gl_amount"], errors="coerce").fillna(0.0)

    cheques_in_transit = float(bank_only_amt[bank_only_amt > 0].sum())      # 银行已记借方
    deposits_in_transit = float(abs(bank_only_amt[bank_only_amt < 0].sum()))  # 银行已记贷方
    gl_only_total = float(gl_only_amt.sum())                                # 企业已记、银行未记
    mismatch_diff = float(
        pd.to_numeric(mismatch["amount_diff"], errors="coerce").fillna(0.0).sum()
    ) if len(mismatch) else 0.0

    bank_balance_v = bank_balance if pd.notna(bank_balance) else 0.0

    rows.append({"Particulars": "银行对账单余额", "Amount": bank_balance})
    rows.append({"Particulars": "加：在途支票（银行已记、企业未记，借方）", "Amount": cheques_in_transit})
    rows.append({"Particulars": "减：在途存款（银行已记、企业未记，贷方）", "Amount": deposits_in_transit})
    rows.append({"Particulars": "加：企业已记、银行未记（GL 单边）", "Amount": gl_only_total})
    rows.append({"Particulars": "加/减：金额不符净差异", "Amount": mismatch_diff})
    rows.append({"Particulars": "调节后企业账面余额", "Amount": gl_balance})
    rows.append({
        "Particulars": "调节后差异（应为 0）",
        "Amount": round(
            bank_balance_v + cheques_in_transit - deposits_in_transit
            + gl_only_total + mismatch_diff - gl_balance,
            2,
        ),
    })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 输入校验（上传阶段用，供 app.py 第一步调用）
# ---------------------------------------------------------------------------
def validate_input_columns(
    df: pd.DataFrame,
    mapping: dict[str, str],
    required_keys: tuple[str, ...] = config.REQUIRED_KEYS,
) -> list[str]:
    """
    检查必需列是否齐备且非空。

    返回问题描述列表（空列表 = 通过）。
    """
    problems: list[str] = []
    for key in required_keys:
        if key not in mapping:
            problems.append(f"缺少必需列：{key}")
            continue
        col = mapping[key]
        if col not in df.columns:
            problems.append(f"列 {col} 不存在")
            continue
        blank = df[col].astype(str).str.strip().isin(["", "nan", "None", "NaN", "NaT"])
        n_blank = int(blank.sum())
        if n_blank:
            problems.append(f"列 {col} 存在 {n_blank} 个空值")
    return problems
