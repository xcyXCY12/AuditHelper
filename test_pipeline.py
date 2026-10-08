"""
test_pipeline.py —— 端到端自测脚本（不依赖 Streamlit）
============================================================
用途：验证 loader → reconciler → adapter → soda_runner 全链路可用。

运行：
    python test_pipeline.py

【用户根据实际列名调整】
  若使用自己的数据，请修改下面的 DATA_DIR / FILE 名称，或直接用 app.py 上传。
"""
from __future__ import annotations

import os
import sys

import pandas as pd

import adapter
import config
import loader
import reconciler
import soda_runner

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")


def _line(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


# ---------------------------------------------------------------------------
# 1. 加载
# ---------------------------------------------------------------------------
def test_load() -> tuple:
    _line("1. 加载与列名映射")

    bank = pd.read_csv(os.path.join(SAMPLES, "sample_bank_statement.csv"),
                       dtype=str, encoding="utf-8-sig")
    gl = pd.read_csv(os.path.join(SAMPLES, "sample_gl_register.csv"),
                     dtype=str, encoding="utf-8-sig")

    bank_std, bank_map, bank_missing = loader.resolve_columns(bank, config.BANK_COLUMN_ALIASES)
    gl_std, gl_map, gl_missing = loader.resolve_columns(gl, config.GL_COLUMN_ALIASES)

    print(f"对账单：{len(bank)} 行，列映射 {bank_map}，缺失 {bank_missing}")
    print(f"总账：  {len(gl)} 行，列映射 {gl_map}，缺失 {gl_missing}")

    assert not bank_missing, f"对账单缺少必需列：{bank_missing}"
    assert not gl_missing, f"总账缺少必需列：{gl_missing}"
    assert "date" in bank_std.columns and "amount" in bank_std.columns

    # 结构校验
    p1 = reconciler.validate_input_columns(bank, bank_map)
    p2 = reconciler.validate_input_columns(gl, gl_map)
    print(f"结构校验：对账单问题 {p1 or '无'}；总账问题 {p2 or '无'}")

    return bank, gl


# ---------------------------------------------------------------------------
# 2. 对账
# ---------------------------------------------------------------------------
def test_reconcile(bank: pd.DataFrame, gl: pd.DataFrame) -> reconciler.ReconResult:
    _line("2. 两轮对账匹配")

    bank_std, _, _ = loader.resolve_columns(bank, config.BANK_COLUMN_ALIASES)
    gl_std, _, _ = loader.resolve_columns(gl, config.GL_COLUMN_ALIASES)

    result = reconciler.reconcile(
        bank_std, gl_std,
        date_tolerance=3,
        amount_tolerance=0.01,
        amount_mode="sign_norm",
    )

    s = result.stats
    print(f"对账单 {s['bank_rows']} 行 / 总账 {s['gl_rows']} 行")
    print(f"  matched    : {s['matched']}")
    print(f"  bank_only  : {s['bank_only']}")
    print(f"  gl_only    : {s['gl_only']}")
    print(f"  mismatch   : {s['mismatch']}")
    print(f"  duplicates : {s['duplicates']}")
    print(f"  匹配率     : {result.match_rate}%")

    assert s["matched"] > 0, "应至少有一笔匹配成功"

    # 输出契约的三个硬要求
    assert "match_status" in result.reconciled.columns, "缺少 match_status 列"
    assert "amount_diff" in result.reconciled.columns, "缺少 amount_diff 列"
    bad = set(result.reconciled["match_status"].unique()) - set(config.VALID_MATCH_STATUSES)
    assert not bad, f"match_status 出现非法取值：{bad}"
    assert result.reconciled["amount_diff"].isna().sum() == 0, "amount_diff 存在缺失"
    print("输出结构检查：match_status 取值合法、amount_diff 无缺失 ✓")

    return result


# ---------------------------------------------------------------------------
# 3. 契约校验
# ---------------------------------------------------------------------------
def test_contracts(bank: pd.DataFrame, gl: pd.DataFrame,
                   result: reconciler.ReconResult) -> None:
    _line("3. 契约校验（Soda Core v4 / 降级内核）")

    status = soda_runner.engine_status()
    print(f"校验引擎：{status['label']}")
    if status["soda_error"]:
        print(f"  （Soda 不可用原因：{status['soda_error']}）")

    con = loader.create_connection()
    loader.register_dataframe_view(con, adapter.VIEW_BANK, bank)
    loader.register_dataframe_view(con, adapter.VIEW_GL, gl)

    # ---- 输入契约 ----
    print("\n--- 3.1 输入契约 ---")
    input_results = adapter.audit_inputs(con)
    for view, res in input_results.items():
        c = res.counts
        print(f"[{view}] engine={res.engine} pass={c['pass']} warn={c['warn']} fail={c['fail']} ok={res.is_ok}")
        for o in res.outcomes:
            print(f"   {o.icon} {o.name} | 度量={o.metric} | {o.message}")
        for e in res.errors:
            print(f"   ℹ️ {e}")

    # ---- 输出契约 ----
    print("\n--- 3.2 输出契约 ---")
    audit = adapter.audit_reconciled(
        con, result.reconciled,
        tolerance=adapter.Tolerance(max_bank_only=2, max_gl_only=3),
    )
    c = audit.result.counts
    print(f"engine={audit.result.engine} pass={c['pass']} warn={c['warn']} fail={c['fail']} ok={audit.is_ok}")
    for o in audit.result.outcomes:
        print(f"   {o.icon} {o.name} | 度量={o.metric} | {o.message}")
    for e in audit.result.errors:
        print(f"   ℹ️ {e}")
    print(f"契约文件：{audit.contract_path}")

    # ---- 汇总 ----
    print("\n--- 3.3 审计结论 ---")
    print(adapter.summarize(result, audit).to_string(index=False))


# ---------------------------------------------------------------------------
# 4. 修复点验证
# ---------------------------------------------------------------------------
def test_fixes() -> None:
    _line("4. 对 Bank-Recon 缺陷的修复验证")

    # --- 4.1 amount_tolerance 真正生效 ---
    # 用典型的"分位差异"（0.01）构造用例：银行 1000.00 vs 总账 1000.01
    bank = pd.DataFrame({"Date": ["01-09-2024"], "Reference": ["A1"],
                         "Amount": ["1000.00"], "Balance": ["5000"]})
    gl = pd.DataFrame({"Date": ["01-09-2024"], "Reference": ["A1"],
                       "Amount": ["1000.01"], "Description": ["x"]})

    r_strict = reconciler.reconcile(bank, gl, amount_tolerance=0.0)
    r_loose = reconciler.reconcile(bank, gl, amount_tolerance=0.01)
    print(f"amount_tolerance=0.00 → matched={r_strict.stats['matched']}（期望 0）")
    print(f"amount_tolerance=0.01 → matched={r_loose.stats['matched']}（期望 1）")
    assert r_strict.stats["matched"] == 0
    assert r_loose.stats["matched"] == 1
    print("✓ amount_tolerance 已生效（原版该参数声明后从未使用）")

    # --- 4.2 借贷方向归一 ---
    bank2 = pd.DataFrame({"Date": ["01-09-2024"], "Reference": ["B1"],
                          "Amount": ["3000"], "Balance": ["5000"]})
    gl2 = pd.DataFrame({"Date": ["01-09-2024"], "Reference": ["B1"],
                        "Amount": ["-3000"], "Description": ["contra"]})
    r_strict2 = reconciler.reconcile(bank2, gl2, amount_mode="strict")
    r_sign2 = reconciler.reconcile(bank2, gl2, amount_mode="sign_norm")
    print(f"strict    → matched={r_strict2.stats['matched']}（期望 0，方向相反）")
    print(f"sign_norm → matched={r_sign2.stats['matched']}（期望 1，方向已归一）")
    assert r_strict2.stats["matched"] == 0
    assert r_sign2.stats["matched"] == 1
    print("✓ 借贷方向归一已生效（原版仅做精确 ==，方向相反必漏匹配）")

    # --- 4.3 两轮匹配：容差匹配确实在第二轮发生 ---
    bank3 = pd.DataFrame({"Date": ["01-09-2024"], "Reference": ["C1"],
                          "Amount": ["800"], "Balance": ["5000"]})
    gl3 = pd.DataFrame({"Date": ["04-09-2024"], "Reference": ["C1"],
                        "Amount": ["800"], "Description": ["timing"]})
    r3 = reconciler.reconcile(bank3, gl3, date_tolerance=3)
    assert r3.stats["matched"] == 1, "3 天内的日期差应能匹配"
    round_used = r3.matched.iloc[0]["match_round"]
    print(f"日期差 3 天 → matched=1，命中轮次={round_used}（期望 tolerance）")
    assert round_used == "tolerance"
    print("✓ 第二轮日期容差匹配生效")

    # --- 4.4 BRS 不再用银行数据冒充总账余额 ---
    bank4 = pd.read_csv(os.path.join(SAMPLES, "sample_bank_statement.csv"), dtype=str)
    gl4 = pd.read_csv(os.path.join(SAMPLES, "sample_gl_register.csv"), dtype=str)
    b_std, _, _ = loader.resolve_columns(bank4, config.BANK_COLUMN_ALIASES)
    g_std, _, _ = loader.resolve_columns(gl4, config.GL_COLUMN_ALIASES)
    r4 = reconciler.reconcile(b_std, g_std)
    print(r4.brs.to_string(index=False))

    gl_sum = reconciler.parse_amounts(gl4["Amount"]).sum()
    bank_sum = reconciler.parse_amounts(bank4["Amount"]).sum()
    assert abs(gl_sum - bank_sum) > 0.01, "示例数据中两侧合计应不同，否则无法验证"

    # 第 6 行"调节后企业账面余额"必须等于总账合计（原版误写成银行合计）
    gl_row = r4.brs[r4.brs["Particulars"].str.contains("企业账面余额")]["Amount"].iloc[0]
    assert abs(float(gl_row) - float(gl_sum)) < 0.01, "BRS 总账余额应等于总账金额合计"
    assert abs(float(gl_row) - float(bank_sum)) > 0.01, "不应等于银行金额合计"
    print(f"✓ BRS 企业账面余额 = {gl_row}（总账合计），而非银行合计 {bank_sum}")


# ---------------------------------------------------------------------------
def main() -> int:
    print("审计助手 · 端到端自测")
    bank, gl = test_load()
    result = test_reconcile(bank, gl)
    test_contracts(bank, gl, result)
    test_fixes()

    _line("全部自测通过 ✅")
    print("提示：可视化界面请运行  streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
