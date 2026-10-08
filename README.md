# 审计助手 · 银行对账工具

把 **Bank-Recon 的交易匹配能力** 与 **Soda Core v4 的数据质量契约校验** 整合到同一个 Streamlit 应用中。

核心思路：银行对账的**行级匹配**是 Soda 的盲区（它只做声明式检查，不做两表勾对），
而数据质量的**契约与阈值分级**是 Bank-Recon 的空白。两者互补而非重叠——
本应用用一张 `reconciled` 结果表把它们串起来。

```
上传 → 输入校验 → 对账匹配 → 输出校验 → 下载
```

---

## 一、快速开始

```bash
# 1. 创建环境（Python 3.11）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
streamlit run app.py
# 浏览器打开 http://localhost:8501
```

界面里点「**使用示例对账单**」「**使用示例总账**」即可跑通全流程，无需自备数据。

命令行自测（不依赖界面）：

```bash
python test_pipeline.py
```

---

## 二、架构与技术栈

| 层 | 文件 | 职责 |
|---|---|---|
| 配置 | `config.py` | 列名映射、日期格式、对账容差、审计阈值（**唯一需要改的地方**） |
| 加载 | `loader.py` | DuckDB 内存连接；CSV → `read_csv_auto`，Excel → `read_xlsx`，注册为视图 |
| 对账 | `reconciler.py` | 纯 pandas 逻辑，两轮匹配，输出 `match_status` 列；**不依赖 Streamlit** |
| 契约 | `soda_runner.py` | 封装 `verify_contract_locally()`，复用同一 DuckDB cursor；含降级内核 |
| 衔接 | `adapter.py` | 对账结果落表 → 动态生成输出契约 → 校验 → 汇总结论 |
| 界面 | `app.py` | 五步流程 UI |
| 契约 | `contracts/*.yml` | 输入契约 ×2、输出契约 ×1 |

**数据流**：

```
CSV/Excel ──loader──> DuckDB 视图(bank_statement / gl_ledger)
                          │
                    reconciler（纯 pandas，两轮匹配）
                          │
                    六类明细 + match_status
                          │
                    adapter.land_reconciled
                          │
                  DuckDB 视图(reconciled)
                          │
              soda_runner ──> Soda Core v4 契约校验
                          │
                    审计结论 is_ok
```

---

## 三、对账逻辑（两轮匹配）

第一轮**精确匹配**（日期完全相同 + 金额在容差内 + 参考号一致）；
第二轮**日期容差匹配**（日期差 ≤ 容差 + 金额在容差内，参考号转为打分加权）。

打分规则参考 Bank-Recon：同日 `+2`，参考号一致 `+1`，取最高分候选。

### 相对 Bank-Recon 的修复

| # | 原版问题 | 本版处理 |
|---|---|---|
| 1 | `amount_tolerance` 参数**声明后从未使用**，金额只做精确 `==` | 容差真正生效，`1000.00` vs `1000.01` 在 `0.01` 容差下可匹配 |
| 2 | 不处理**借贷方向**，银行借方 vs 总账贷方永不相遇 | 新增 `amount_mode`：`strict` / `absolute` / `sign_norm`（默认） |
| 3 | 打分初值 `-1` 但判定用 `>= 0`，README 的"最低分 0"与代码矛盾 | 显式区分「候选」与「确认」，语义清晰 |
| 4 | `generate_brs` 用 `bank_df['Amount'].sum()` **冒充总账余额** | 改为从总账侧取数；`gl_only`/`mismatch` 真正拆成调整项 |
| 5 | `app.py` 按**列位置**赋名（`df.columns = [...]`） | 按列名语义映射，支持中英文别名与模糊匹配 |
| 6 | 日期格式写死 `%d-%m-%Y` | 按格式列表依次尝试 + 自动推断回退 |
| 7 | `gl_only`/`mismatch` 传入 `generate_brs` 后未使用 | 已用于调整项计算 |

### 输出分类

| `match_status` | 含义 | 业务对应 |
|---|---|---|
| `matched` | 双向勾对成功 | 正常 |
| `bank_only` | 银行已记、企业未记 | 未达账项（在途支票） |
| `gl_only` | 企业已记、银行未记 | 未达账项（应计、冲销） |
| `mismatch` | 同参考号但金额/日期不符 | 需追查的错账 |
| `duplicate` | 单侧重复录入 | 内控缺陷 |

---

## 四、Soda 契约

### 输入契约

- `contracts/input_bank_statement.yml` → dataset: `bank_statement`
- `contracts/input_gl_ledger.yml` → dataset: `gl_ledger`

校验 `date` / `amount` / `reference` 三列**无缺失**，并对日期、金额做格式合法性检查。

### 输出契约

- `contracts/output_reconciled.yml`（固定模板）
- `contracts/output_reconciled_dynamic.yml`（按界面阈值**动态生成**）

校验：

1. `match_status` **取值合法**（只能是上述 5 类）
2. `amount_diff` **无缺失**
3. `date_diff_days` 非负
4. `bank_only` / `gl_only` 笔数在重要性水平内

### Python API 调用

```python
from soda_core.contracts import verify_contract_locally

result = verify_contract_locally(
    data_source_file_path="ds_config.yml",
    contract_file_path="contracts/output_reconciled.yml",
    publish=False,
)
if not result.is_ok:          # 注意：v4 是属性，不是方法
    print(result.get_errors_str())
```

Soda v4 结果对象结构（已在 4.26 实测）：

```
SessionResult
  .is_ok / .is_passed / .is_failed / .is_warned / .has_errors   ← 属性
  .contract_verification_results[]
      .check_results[]
          .outcome                  → CheckOutcome 枚举（PASSED/WARNED/FAILED）
          .threshold_value
          .diagnostic_metric_values → {'missing_count': 0, ...}
          .check.name / .check.type / .check.column_name
```

---

## 五、需要适配的接口差异

| # | 差异 | 适配方案 |
|---|---|---|
| 1 | **Soda 不能直接读 CSV/Excel** | `loader` 先注册成 DuckDB 视图（CSV 用 `read_csv_auto`，Excel 用 `read_xlsx`） |
| 2 | **DuckDB 无独立"库名"层** | 契约 dataset 路径为 `数据源名/catalog/schema/表名`；物化到文件库时 `catalog` 段须改写为文件名，由 `_rewrite_dataset_catalog()` 自动处理 |
| 3 | **DuckDB 无容差概念** | 日期容差留在 pandas 层（`reconciler`），Soda 只校验结果表 |
| 4 | **同类型检查需唯一 qualifier** | 两个 `failed_rows` 分别标 `bank_only_count` / `gl_only_count`，否则报 `Duplicate identity` |
| 5 | **`is_ok` 是属性而非方法** | 适配层按属性读取（网上部分示例写成 `result.is_ok()` 会报 `'bool' object is not callable`） |
| 6 | **Soda 需要文件路径** | `materialize_duckdb()` 把内存库物化为临时 `.duckdb` 文件 |
| 7 | 校验引擎可能不可用 | `soda_runner` 内置 pandas 降级内核，语义对齐（未装 Soda 也能跑） |

---

## 六、定制指南

**唯一需要改的地方是 `config.py`**：

```python
# 1. 列名映射（支持中文别名与模糊匹配）
BANK_COLUMN_ALIASES = {
    "date":      ["Date", "交易日期", "日期"],
    "amount":    ["Amount", "金额", "发生额"],
    "reference": ["Reference", "参考号", "凭证号"],
}

# 2. 日期格式
DATE_FORMATS = ["%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d"]

# 3. 对账容差
DEFAULT_DATE_TOLERANCE_DAYS = 3
DEFAULT_AMOUNT_TOLERANCE    = 0.01
DEFAULT_AMOUNT_MODE         = "sign_norm"
```

契约 YAML 中标注了 `【调整点】` 的列名同理。审计重要性水平可直接在界面左侧面板调节，无需改代码。

---

## 七、目录结构

```
├── app.py                     # Streamlit 入口
├── config.py                  # 【配置中心】
├── loader.py                  # DuckDB 加载层
├── reconciler.py              # 对账逻辑（纯 pandas）
├── soda_runner.py             # Soda v4 封装 + 降级内核
├── adapter.py                 # 对账结果 ↔ 契约 衔接层
├── test_pipeline.py           # 端到端自测
├── requirements.txt
├── contracts/
│   ├── input_bank_statement.yml
│   ├── input_gl_ledger.yml
│   └── output_reconciled.yml
└── sample_data/
    ├── sample_bank_statement.csv
    └── sample_gl_register.csv
```

---

## 八、已知限制

- **输出契约模板的列名与示例文件一致**（`Date`/`Amount`/`Reference`）。若实际列名不同，需同步修改契约 YAML 和 `config.py`。
- 降级内核的 `schema` 检查只做列存在性提示；完整的类型/顺序比对需真实 Soda 引擎。
- `materialize_duckdb` 会在系统临时目录留下 `.duckdb` 文件（Soda 需要文件路径）；如需清理可在流程结束后删除。
- Excel 读取依赖 DuckDB 的 `excel` 扩展，首次需联网执行 `INSTALL excel`；离线环境请先转成 CSV。
