"""
app.py —— 审计助手（银行对账 + 数据质量契约校验）
============================================================
流程：上传 → 输入校验 → 对账 → 输出校验 → 下载 CSV

运行：
    streamlit run app.py

【用户需要调整的位置】
  - 列名映射、日期格式、对账容差 → config.py
  - 契约阈值 → 界面上可直接调，或用 config.py 的默认值
"""
from __future__ import annotations

import io
import os

import pandas as pd
import streamlit as st

import adapter
import config
import loader
import reconciler
import soda_runner

st.set_page_config(page_title="审计助手 · 银行对账", layout="wide")

SAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")


# ---------------------------------------------------------------------------
# 会话状态
# ---------------------------------------------------------------------------
def init_state() -> None:
    defaults = {
        "con": None,
        "bank_df": None,
        "gl_df": None,
        "bank_map": None,
        "gl_map": None,
        "recon_result": None,
        "audit": None,
        "input_results": None,
    }
    for key, val in defaults.items():
        st.session_state.setdefault(key, val)


def get_connection():
    """惰性创建 DuckDB 连接（整个会话复用同一个连接，Soda 也用它的 cursor）。"""
    if st.session_state["con"] is None:
        st.session_state["con"] = loader.create_connection()
    return st.session_state["con"]


# ---------------------------------------------------------------------------
# 侧边栏：全局配置
# ---------------------------------------------------------------------------
def sidebar() -> dict:
    st.sidebar.title("⚙️ 参数配置")

    st.sidebar.subheader("对账参数")
    date_tol = st.sidebar.number_input(
        "日期容差（天）", min_value=0, max_value=30,
        value=config.DEFAULT_DATE_TOLERANCE_DAYS, step=1,
        help="对应 Bank-Recon 的 date_tolerance。0 表示只做精确日期匹配。",
    )
    amount_tol = st.sidebar.number_input(
        "金额容差", min_value=0.0, max_value=1000.0,
        value=float(config.DEFAULT_AMOUNT_TOLERANCE), step=0.01, format="%.2f",
        help="修复了 Bank-Recon 中 amount_tolerance 声明后未生效的问题。",
    )
    amount_mode = st.sidebar.selectbox(
        "借贷方向策略",
        options=["sign_norm", "absolute", "strict"],
        index=["sign_norm", "absolute", "strict"].index(config.DEFAULT_AMOUNT_MODE),
        help=(
            "sign_norm：归一化符号（推荐，容忍银行借方 vs 总账贷方）；"
            "absolute：取绝对值比对；"
            "strict：严格要求同号同值（等同 Bank-Recon 原行为）"
        ),
    )

    st.sidebar.subheader("审计重要性水平（输出契约阈值）")
    max_bank_only = st.sidebar.number_input(
        "bank_only 最大容许笔数", min_value=0, value=config.MAX_BANK_ONLY_ROWS, step=1,
        help="超过该笔数则输出契约判定失败。",
    )
    max_gl_only = st.sidebar.number_input(
        "gl_only 最大容许笔数", min_value=0, value=config.MAX_GL_ONLY_ROWS, step=1,
    )
    max_diff_missing = st.sidebar.number_input(
        "amount_diff 最大容许缺失行数", min_value=0,
        value=config.MAX_AMOUNT_DIFF_MISSING, step=1,
    )

    st.sidebar.divider()
    status = soda_runner.engine_status()
    if status["soda_available"]:
        st.sidebar.success(f"校验引擎：{status['label']}")
    else:
        st.sidebar.warning(
            f"校验引擎：{status['label']}\n\n"
            "安装 Soda：`pip install soda-duckdb`"
        )

    return {
        "date_tolerance": int(date_tol),
        "amount_tolerance": float(amount_tol),
        "amount_mode": amount_mode,
        "tolerance": adapter.Tolerance(
            max_bank_only=int(max_bank_only),
            max_gl_only=int(max_gl_only),
            max_amount_diff_missing=int(max_diff_missing),
        ),
    }


# ---------------------------------------------------------------------------
# 步骤 1：上传
# ---------------------------------------------------------------------------
def step_upload() -> tuple[object, object]:
    st.subheader("① 上传数据文件")

    col1, col2 = st.columns(2)
    with col1:
        bank_file = st.file_uploader(
            "银行对账单（CSV / Excel）", type=["csv", "xlsx", "xls"], key="bank_file"
        )
        if st.button("使用示例对账单", key="use_sample_bank"):
            st.session_state["bank_df"] = _read_sample("sample_bank_statement.csv")
            st.rerun()
    with col2:
        gl_file = st.file_uploader(
            "总账明细账（CSV / Excel）", type=["csv", "xlsx", "xls"], key="gl_file"
        )
        if st.button("使用示例总账", key="use_sample_gl"):
            st.session_state["gl_df"] = _read_sample("sample_gl_register.csv")
            st.rerun()

    # 上传的文件优先
    if bank_file is not None:
        try:
            raw, mapping, missing = _load_upload(bank_file, config.BANK_COLUMN_ALIASES)
            st.session_state["bank_df"] = raw
            st.session_state["bank_map"] = mapping
            if missing:
                st.error(f"银行对账单缺少必需列：{missing}（请在 config.py 中补充别名）")
        except loader.LoadError as exc:
            st.error(str(exc))

    if gl_file is not None:
        try:
            raw, mapping, missing = _load_upload(gl_file, config.GL_COLUMN_ALIASES)
            st.session_state["gl_df"] = raw
            st.session_state["gl_map"] = mapping
            if missing:
                st.error(f"总账明细账缺少必需列：{missing}（请在 config.py 中补充别名）")
        except loader.LoadError as exc:
            st.error(str(exc))

    bank_df = st.session_state["bank_df"]
    gl_df = st.session_state["gl_df"]

    if bank_df is not None or gl_df is not None:
        c1, c2 = st.columns(2)
        with c1:
            if bank_df is not None:
                st.caption(f"对账单：{len(bank_df)} 行 × {len(bank_df.columns)} 列")
                st.dataframe(bank_df.head(5), width='stretch', hide_index=True)
        with c2:
            if gl_df is not None:
                st.caption(f"总账：{len(gl_df)} 行 × {len(gl_df.columns)} 列")
                st.dataframe(gl_df.head(5), width='stretch', hide_index=True)

    con = get_connection()
    ready = bank_df is not None and gl_df is not None
    if ready:
        _register_inputs(con, bank_df, gl_df)

    return con, ready


def _load_upload(uploaded, aliases: dict) -> tuple[pd.DataFrame, dict, list]:
    """读取上传文件 + 列名映射，返回 (标准化后的 DF, 映射表, 缺失必需列)。

    注意：这里返回的 DataFrame 已经重命名为标准列名（date/amount/reference），
          同时保留一份原始列名映射供契约使用。
    """
    raw_bytes = uploaded.getvalue()
    df = loader.read_dataframe(raw_bytes, uploaded.name)
    _, mapping, missing = loader.resolve_columns(df, aliases)
    return df, mapping, missing


def _read_sample(name: str) -> pd.DataFrame:
    path = os.path.join(SAMPLES_DIR, name)
    if not os.path.exists(path):
        st.error(f"示例文件不存在：{path}")
        return pd.DataFrame()
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def _register_inputs(con, bank_df: pd.DataFrame, gl_df: pd.DataFrame) -> None:
    """
    把上传的原始表注册为以【原始列名】命名的 DuckDB 视图。

    为什么保持原始列名：
      输入契约 YAML 里的 column name 对应实际列名，
      Soda 需要按实际列名去查，所以这里不做重命名。
      reconciler 内部再做标准化映射。
    """
    loader.register_dataframe_view(con, adapter.VIEW_BANK, bank_df)
    loader.register_dataframe_view(con, adapter.VIEW_GL, gl_df)


# ---------------------------------------------------------------------------
# 步骤 2：输入校验
# ---------------------------------------------------------------------------
def step_input_check(con) -> bool:
    st.subheader("② 输入数据质量校验")

    bank_df = st.session_state["bank_df"]
    gl_df = st.session_state["gl_df"]
    if bank_df is None or gl_df is None:
        st.info("请先上传两个文件。")
        return False

    # ---- 2.1 结构校验（本地快速检查） ----
    problems: list[str] = []
    for label, df, aliases in (
        ("银行对账单", bank_df, config.BANK_COLUMN_ALIASES),
        ("总账明细账", gl_df, config.GL_COLUMN_ALIASES),
    ):
        _, mapping, missing = loader.resolve_columns(df, aliases)
        if missing:
            problems.append(f"{label}：缺少列 {missing}")
        else:
            for p in reconciler.validate_input_columns(df, mapping):
                problems.append(f"{label}：{p}")

    if problems:
        st.error("结构校验未通过：\n\n" + "\n".join(f"- {p}" for p in problems))
        with st.expander("查看实际列名，便于修正 config.py"):
            st.write("对账单列：", list(bank_df.columns))
            st.write("总账列：", list(gl_df.columns))
        return False

    st.success("结构校验通过：必需列齐备。")

    # ---- 2.2 契约校验（Soda / 降级内核） ----
    with st.spinner("正在执行输入契约校验…"):
        results = adapter.audit_inputs(con)
    st.session_state["input_results"] = results

    for view, res in results.items():
        label = "银行对账单" if view == adapter.VIEW_BANK else "总账明细账"
        _render_contract_result(label, res)

    return all(r.is_ok for r in results.values())


def _render_contract_result(label: str, res: soda_runner.ContractRunResult) -> None:
    """渲染一次契约校验结果。"""
    counts = res.counts
    badge = "✅ 通过" if res.is_ok else "❌ 未通过"
    with st.expander(f"{label} · {badge} — 通过 {counts['pass']} / 告警 {counts['warn']} / 失败 {counts['fail']}",
                     expanded=not res.is_ok):
        if res.errors:
            for err in res.errors:
                st.caption(f"ℹ️ {err}")
        if res.outcomes:
            st.dataframe(
                pd.DataFrame([{
                    "结果": o.icon,
                    "检查": o.name,
                    "类型": o.check_type,
                    "列": o.column or "",
                    "度量": o.metric,
                    "说明": o.message,
                } for o in res.outcomes]),
                width='stretch', hide_index=True,
            )
        st.caption(f"引擎：{res.engine}")


# ---------------------------------------------------------------------------
# 步骤 3：对账
# ---------------------------------------------------------------------------
def step_reconcile(cfg: dict) -> bool:
    st.subheader("③ 执行对账匹配")

    bank_df = st.session_state["bank_df"]
    gl_df = st.session_state["gl_df"]
    if bank_df is None or gl_df is None:
        st.info("请先完成上传与输入校验。")
        return False

    # 把实际列名映射为标准列名后交给纯逻辑层
    bank_std, _, _ = loader.resolve_columns(bank_df, config.BANK_COLUMN_ALIASES)
    gl_std, _, _ = loader.resolve_columns(gl_df, config.GL_COLUMN_ALIASES)

    with st.spinner("正在执行两轮匹配…"):
        result = reconciler.reconcile(
            bank_std, gl_std,
            date_tolerance=cfg["date_tolerance"],
            amount_tolerance=cfg["amount_tolerance"],
            amount_mode=cfg["amount_mode"],
        )
    st.session_state["recon_result"] = result

    # ---- 指标卡 ----
    m = st.columns(5)
    m[0].metric("对账单记录", result.stats["bank_rows"])
    m[1].metric("总账记录", result.stats["gl_rows"])
    m[2].metric("✓ 匹配成功", result.stats["matched"])
    m[3].metric("未达账项", result.stats["bank_only"] + result.stats["gl_only"])
    m[4].metric("匹配率", f"{result.match_rate}%")

    # ---- 明细标签页 ----
    tabs = st.tabs(["✓ 匹配", "🏦 仅银行有", "📘 仅总账有", "⚠️ 金额/日期不符", "🔁 重复", "BRS 调节表"])
    with tabs[0]:
        _show_df(result.matched, "尚无匹配记录")
    with tabs[1]:
        _show_df(result.bank_only, "无仅银行有的记录")
    with tabs[2]:
        _show_df(result.gl_only, "无仅总账有的记录")
    with tabs[3]:
        _show_df(result.mismatch, "无金额/日期不符记录")
    with tabs[4]:
        _show_df(result.duplicates, "无重复记录")
    with tabs[5]:
        st.dataframe(result.brs, width='stretch', hide_index=True)

    return True


def _show_df(df: pd.DataFrame, empty_msg: str) -> None:
    if df is None or df.empty:
        st.info(empty_msg)
    else:
        show = df.drop(columns=[c for c in df.columns if c.startswith("_")], errors="ignore")
        st.dataframe(show, width='stretch', hide_index=True)


# ---------------------------------------------------------------------------
# 步骤 4：输出校验
# ---------------------------------------------------------------------------
def step_output_check(con, cfg: dict) -> None:
    st.subheader("④ 对账结果契约校验")

    result = st.session_state["recon_result"]
    if result is None:
        st.info("请先执行对账。")
        return

    with st.spinner("正在校验输出契约…"):
        audit = adapter.audit_reconciled(
            con, result.reconciled, tolerance=cfg["tolerance"]
        )
    st.session_state["audit"] = audit

    if audit.result:
        _render_contract_result("对账结果（reconciled）", audit.result)

    with st.expander("查看生效的输出契约（阈值来自左侧面板）"):
        st.code(audit.contract_path, language="text")
        with open(audit.contract_path, "r", encoding="utf-8") as fh:
            st.code(fh.read(), language="yaml")

    # ---- 审计结论 ----
    summary = adapter.summarize(result, audit)
    st.markdown("**审计结论汇总**")
    st.dataframe(summary, width='stretch', hide_index=True)

    if audit.is_ok:
        st.success("✅ 审计结论：对账结果通过全部契约校验。")
    else:
        st.error("❌ 审计结论：存在未通过项，需人工复核后再出具结论。")


# ---------------------------------------------------------------------------
# 步骤 5：下载
# ---------------------------------------------------------------------------
def _cn_display(df: pd.DataFrame) -> pd.DataFrame:
    """导出专用：内部英文列名/状态值 → 中文显示（不影响内部计算与契约校验）。"""
    if df is None or df.empty:
        return df
    out = df.drop(
        columns=[c for c in df.columns if str(c).startswith("_")], errors="ignore"
    ).copy()
    for col, vmap in config.EXPORT_VALUE_MAPS_CN.items():
        if col in out.columns:
            out[col] = out[col].map(vmap).fillna(out[col])
    return out.rename(columns=config.EXPORT_COLUMN_NAMES_CN)


def step_download() -> None:
    st.subheader("⑤ 导出结果")

    result = st.session_state["recon_result"]
    if result is None:
        st.info("请先执行对账。")
        return

    # ---- 主结果 CSV ----
    st.download_button(
        "下载对账明细 reconciled.csv",
        data=_cn_display(result.reconciled).to_csv(index=False).encode("utf-8-sig"),
        file_name="reconciled.csv",
        mime="text/csv",
        type="primary",
    )

    cols = st.columns(3)
    for i, (name, df) in enumerate([
        ("matched.csv", result.matched),
        ("bank_only.csv", result.bank_only),
        ("gl_only.csv", result.gl_only),
        ("mismatch.csv", result.mismatch),
        ("duplicates.csv", result.duplicates),
        ("brs.csv", result.brs),
    ]):
        with cols[i % 3]:
            if df is not None and not df.empty:
                clean = _cn_display(df)
                st.download_button(
                    f"下载 {name}",
                    data=clean.to_csv(index=False).encode("utf-8-sig"),
                    file_name=name,
                    mime="text/csv",
                    key=f"dl_{name}",
                )

    # ---- Excel 汇总 ----
    audit = st.session_state.get("audit")
    with st.expander("打包下载 Excel 汇总（含审计结论）"):
        if st.button("生成 Excel"):
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                _cn_display(result.reconciled).to_excel(
                    writer, sheet_name="对账明细", index=False
                )
                for df_sheet, sheet_name in (
                    (result.matched, "匹配成功"),
                    (result.bank_only, "仅银行有"),
                    (result.gl_only, "仅总账有"),
                    (result.mismatch, "金额日期不符"),
                    (result.duplicates, "重复记录"),
                ):
                    if not df_sheet.empty:
                        _cn_display(df_sheet).to_excel(
                            writer, sheet_name=sheet_name, index=False
                        )
                _cn_display(result.brs).to_excel(
                    writer, sheet_name="余额调节表", index=False
                )
                adapter.summarize(result, audit).to_excel(
                    writer, sheet_name="审计结论", index=False
                )
                if audit and audit.result and audit.result.outcomes:
                    pd.DataFrame([{
                        "结果": config.EXPORT_LEVEL_CN.get(o.level, o.level),
                        "检查": o.name, "类型": o.check_type,
                        "列": o.column or "", "度量": o.metric, "说明": o.message,
                    } for o in audit.result.outcomes]).to_excel(
                        writer, sheet_name="契约校验", index=False
                    )
            buffer.seek(0)
            st.download_button(
                "下载 审计助手_对账报告.xlsx",
                data=buffer.getvalue(),
                file_name="审计助手_对账报告.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    init_state()

    st.title("🔍 审计助手 · 银行对账")
    st.caption(
        "交易匹配引擎（参考 Bank-Recon 两轮匹配） + 数据质量契约校验（Soda Core v4 / DuckDB）"
    )

    cfg = sidebar()
    con, ready = step_upload()

    st.divider()
    input_ok = step_input_check(con) if ready else False

    st.divider()
    recon_ok = False
    if ready:
        recon_ok = step_reconcile(cfg)

    st.divider()
    if recon_ok:
        step_output_check(con, cfg)

    st.divider()
    if recon_ok:
        step_download()


if __name__ == "__main__":
    main()
