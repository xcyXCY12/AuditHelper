"""
审计助手 - 全局配置
============================================================
【用户需要调整的位置】
如果实际业务系统的列名与此处不同，只需修改本文件，无需改动其他模块。
"""

# ---------------------------------------------------------------------------
# 1. 列名映射 —— 【用户根据实际列名调整】
# ---------------------------------------------------------------------------
# 左侧 key 是程序内部使用的标准列名（不要改）
# 右侧 value 是实际文件中的列名（可按需替换成中文列名，如 "日期" / "金额" / "摘要"）
# 值为 None 表示"该列必需但没有别名"，程序会自动做模糊匹配（大小写、下划线、空格不敏感）
BANK_COLUMN_ALIASES = {
    "date":      ["Date", "交易日期", "日期", "记账日期", "TransactionDate", "TxnDate"],
    "amount":    ["Amount", "金额", "交易金额", "发生额", "TransactionAmount"],
    "reference": ["Reference", "参考号", "凭证号", "流水号", "支票号", "Ref", "TxnID"],
    # 以下为可选列
    "balance":   ["Balance", "余额", "账户余额", "ClosingBalance"],
    "description": ["Description", "摘要", "备注", "说明", "Narrative"],
}

GL_COLUMN_ALIASES = {
    "date":      ["Date", "记账日期", "日期", "凭证日期", "PostingDate"],
    "amount":    ["Amount", "金额", "发生额", "借方金额", "DebitAmount"],
    "reference": ["Reference", "参考号", "凭证号", "凭证编号", "VoucherNo", "DocNo"],
    "description": ["Description", "摘要", "备注", "说明", "Narrative"],
}

# 必需列（缺失则直接判定输入不合格，不进入对账环节）
REQUIRED_KEYS = ("date", "amount", "reference")

# ---------------------------------------------------------------------------
# 2. 日期解析 —— 【用户根据实际日期格式调整】
# ---------------------------------------------------------------------------
# 按顺序尝试；全部失败则回退到 pandas 自动推断（dayfirst=True 更适合中文/欧洲格式）
DATE_FORMATS = ["%d-%m-%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%Y%m%d"]
DATE_AUTODETECT_DAYFIRST = True

# ---------------------------------------------------------------------------
# 3. 对账参数（与 Bank-Recon 的 date_tolerance / amount_tolerance 对应）
# ---------------------------------------------------------------------------
DEFAULT_DATE_TOLERANCE_DAYS = 3      # 日期容差（天），对应 Bank-Recon 的 date_tolerance
DEFAULT_AMOUNT_TOLERANCE = 0.01      # 金额容差（绝对值），修复 Bank-Recon 中 amount_tolerance 死参数
DEFAULT_AMOUNT_ROUND = 2             # 金额比对前保留的小数位（消除浮点误差）

# 借贷方向处理策略：
#   "strict"   —— 金额必须同号同值（最严格，等同 Bank-Recon 原行为）
#   "absolute" —— 先取绝对值再比对（适合金额列已是正数、方向由另一列表达的台账）
#   "sign_norm"—— 先归一化符号方向再比对（适合借贷方用正负号表达的台账）
DEFAULT_AMOUNT_MODE = "sign_norm"

# 匹配打分权重（参考 Bank-Recon：同日 +2，参考号一致 +1）
SCORE_EXACT_DATE = 2
SCORE_REFERENCE_MATCH = 1

# ---------------------------------------------------------------------------
# 4. DuckDB / 文件读取
# ---------------------------------------------------------------------------
CSV_READ_FUNCTION = "read_csv_auto"          # CSV 读取函数
EXCEL_READ_FUNCTION = "read_xlsx"            # Excel 读取函数
SHEET_NAME = None                            # Excel 工作表名；None = 第一个工作表

# ---------------------------------------------------------------------------
# 5. 审计阈值 —— 输出契约的默认容差（可按项目重要性调整）
# ---------------------------------------------------------------------------
# match_status 为 bank_only 的行数上限（0 表示必须完全对上，实践中通常允许少量未达账项）
MAX_BANK_ONLY_ROWS = 0
MAX_GL_ONLY_ROWS = 0
# amount_diff 缺失的行数上限（0 表示不允许缺失）
MAX_AMOUNT_DIFF_MISSING = 0
# 允许的匹配率下限（百分比）
MIN_MATCH_RATE_PERCENT = 0.0

# 合法状态集合（同时用于输出契约的 valid_values）
VALID_MATCH_STATUSES = ["matched", "bank_only", "gl_only", "mismatch", "duplicate"]

# ---------------------------------------------------------------------------
# 6. 导出中文化 —— CSV / Excel 导出时的列名与取值显示
# ---------------------------------------------------------------------------
# 【导出专用】程序内部仍使用英文标准列名与英文状态值（对账匹配、契约校验依赖它们），
# 仅在导出 CSV / Excel 文件时转换成下面的中文显示。
# 想调整措辞或补充列名，只需修改本节，无需改动其他模块。

# 列名：内部名 → 导出显示名
EXPORT_COLUMN_NAMES_CN = {
    "match_status":     "匹配状态",
    "match_round":      "匹配轮次",
    "match_score":      "匹配得分",
    "reference":        "参考号",
    "bank_row_id":      "对账单行号",
    "gl_row_id":        "总账行号",
    "bank_date":        "对账单日期",
    "gl_date":          "总账日期",
    "date_diff_days":   "日期差（天）",
    "bank_amount":      "对账单金额",
    "gl_amount":        "总账金额",
    "amount_diff":      "金额差异",
    "bank_description": "对账单摘要",
    "gl_description":   "总账摘要",
    # 单侧导出（matched / bank_only / gl_only / duplicates）使用的列
    "date":             "日期",
    "txn_date":         "交易日期",
    "amount":           "金额",
    "description":      "摘要",
    "balance":          "余额",
    "source_type":      "来源",
    # 余额调节表（BRS）
    "Particulars":      "项目",
    "Amount":           "金额",
}

# 取值：列名 → {内部值: 显示值}
EXPORT_VALUE_MAPS_CN = {
    "match_status": {
        "matched":   "匹配成功",
        "bank_only": "仅银行有",
        "gl_only":   "仅总账有",
        "mismatch":  "金额日期不符",
        "duplicate": "重复记录",
    },
    "match_round": {
        "exact":          "精确匹配",
        "tolerance":      "容差匹配",
        "reference_only": "仅参考号相同",
        "none":           "不适用",
    },
    "source_type": {
        "Bank": "银行对账单",
        "GL":   "总账",
    },
}

# 契约校验结果等级（Excel「契约校验」工作表的"结果"列）
EXPORT_LEVEL_CN = {
    "pass": "通过",
    "warn": "警告",
    "fail": "未通过",
}
