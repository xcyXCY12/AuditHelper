"""
loader.py —— DuckDB 内存连接与数据加载层
============================================================
职责：
  1. 创建 DuckDB 内存连接（Soda 与 pandas 共用同一个 cursor）
  2. 将上传的 CSV / Excel 注册为 DuckDB 视图
     - CSV   → read_csv_auto()
     - Excel → read_xlsx()（需要 DuckDB 的 excel 扩展）
  3. 将 pandas DataFrame 注册为视图（供 reconciler 写回中间结果）

【Soda 不能直接读 CSV/Excel】
Soda Core 只认"数据源 + 表/视图"。因此本模块先把文件注册成 DuckDB 视图，
再由 soda_runner 生成 ds_config.yml 指向 DuckDB，从而让 Soda 校验这些文件内容。

参考 Soda 官方 data source 配置结构：
    name: duckdb_local
    type: duckdb
    connection:
      database: :memory:
"""
from __future__ import annotations

import io
import os
import re
from typing import Optional

import duckdb
import pandas as pd

import config


class LoadError(Exception):
    """文件加载失败（格式错误、缺列、编码问题等）。"""


# ---------------------------------------------------------------------------
# 列名归一化工具
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    """把列名归一化，用于模糊匹配：去空格/下划线/连字符，转小写。"""
    return re.sub(r"[\s_\-（）()【】\[\]]+", "", str(name)).lower()


def resolve_columns(
    df: pd.DataFrame,
    aliases: dict[str, list[str]],
    required_keys: tuple[str, ...] = config.REQUIRED_KEYS,
) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    """
    把实际列名映射为程序内部标准列名。

    返回: (重命名后的 DataFrame, {标准列名: 实际列名}, 缺失的必需列列表)

    【用户根据实际列名调整】的位置在 config.py 的 *_COLUMN_ALIASES。
    """
    mapping: dict[str, str] = {}      # 标准名 -> 实际列名
    rename: dict[str, str] = {}       # 实际列名 -> 标准名
    normalized = {_norm(c): c for c in df.columns}

    for std_name, candidates in aliases.items():
        found: Optional[str] = None
        # 第一轮：精确匹配别名列表
        for cand in candidates:
            if cand in df.columns:
                found = cand
                break
        # 第二轮：归一化模糊匹配
        if found is None:
            for cand in candidates:
                key = _norm(cand)
                if key in normalized:
                    found = normalized[key]
                    break
        # 第三轮：标准名本身直接出现
        if found is None and std_name in df.columns:
            found = std_name

        if found is not None and found not in rename:
            mapping[std_name] = found
            rename[found] = std_name

    missing = [k for k in required_keys if k not in mapping]
    out = df.rename(columns=rename)
    return out, mapping, missing


# ---------------------------------------------------------------------------
# DuckDB 连接
# ---------------------------------------------------------------------------
def create_connection() -> duckdb.DuckDBPyConnection:
    """
    创建 DuckDB 内存连接。

    注意：使用 :memory: 时，同一个 connection 对象内的 cursor() 会共享同一份内存库，
    因此 Soda 与我们的视图注册可以复用同一个连接（这是"复用同一个 DuckDB cursor"的关键）。
    """
    con = duckdb.connect(database=":memory:")
    _ensure_extensions(con)
    return con


def _ensure_extensions(con: duckdb.DuckDBPyConnection) -> dict[str, bool]:
    """
    尝试加载 excel 扩展（read_xlsx 需要）。

    返回 {"excel": True/False}。若安装失败不抛异常，交由调用方在读取 Excel 时给出友好提示。
    """
    status = {"excel": False}
    try:
        con.execute("INSTALL excel;")
        con.execute("LOAD excel;")
        status["excel"] = True
    except Exception:
        # 可能已在某些发行版内置，或离线环境无法 INSTALL，尝试直接 LOAD
        try:
            con.execute("LOAD excel;")
            status["excel"] = True
        except Exception:
            status["excel"] = False
    return status


# ---------------------------------------------------------------------------
# 文件 → DuckDB 视图
# ---------------------------------------------------------------------------
def register_csv_view(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    file_bytes: bytes,
    *,
    all_varchar: bool = True,
    filename_override: Optional[str] = None,
) -> None:
    """
    将内存中的 CSV 字节注册为 DuckDB 视图。

    做法：把字节写成临时文件，再用 read_csv_auto() 建视图。
    （DuckDB 的 read_csv_auto 支持从本地路径读取；内存缓冲需要额外的 httpfs/pyarrow 支持，
     为最大兼容性这里落一个临时文件，用完由调用方清理。）

    all_varchar=True：强制按字符串读入，避免 DuckDB 自动推断把 "01-09-2024" 解析坏，
                      也避免金额前导零丢失。类型转换统一由 pandas 层负责。
    """
    path = _write_temp(file_bytes, filename_override or f"{view_name}.csv", ".csv")
    sql = (
        f'CREATE OR REPLACE VIEW "{view_name}" AS '
        f"SELECT * FROM {config.CSV_READ_FUNCTION}('{_escape_path(path)}'"
        f"{', ALL_VARCHAR=true' if all_varchar else ''});"
    )
    try:
        con.execute(sql)
    except Exception as exc:
        raise LoadError(f"CSV 读取失败（视图 {view_name}）：{exc}") from exc


def register_excel_view(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    file_bytes: bytes,
    *,
    sheet_name: Optional[str] = None,
) -> None:
    """
    将内存中的 Excel 字节注册为 DuckDB 视图。

    使用 read_xlsx()，需要 duckdb 的 excel 扩展。
    all_varchar=true：同样为了避免日期被 Excel 序列号/类型推断破坏。
    """
    path = _write_temp(file_bytes, f"{view_name}.xlsx", ".xlsx")
    sheet = sheet_name or config.SHEET_NAME
    opts = ["header=true", "all_varchar=true"]
    if sheet:
        opts.append(f"sheet='{sheet}'")

    sql = (
        f'CREATE OR REPLACE VIEW "{view_name}" AS '
        f"SELECT * FROM {config.EXCEL_READ_FUNCTION}('{_escape_path(path)}', {', '.join(opts)});"
    )
    try:
        con.execute(sql)
    except Exception as exc:
        raise LoadError(
            f"Excel 读取失败（视图 {view_name}）：{exc}\n"
            "提示：read_xlsx 需要 DuckDB excel 扩展，请在联网环境执行 INSTALL excel; 后重试，"
            "或把文件另存为 CSV 再上传。"
        ) from exc


def register_file_view(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    file_bytes: bytes,
    filename: str,
) -> None:
    """按扩展名自动选择 CSV / Excel 注册方式。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".csv", ".txt", ".tsv"):
        register_csv_view(con, view_name, file_bytes, filename_override=filename)
    elif ext in (".xlsx", ".xls"):
        register_excel_view(con, view_name, file_bytes)
    else:
        raise LoadError(f"不支持的文件类型：{ext}（仅支持 .csv / .xlsx / .xls）")


def register_dataframe_view(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
    df: pd.DataFrame,
) -> None:
    """
    将 pandas DataFrame 注册为 DuckDB 视图。

    实现方式：把 DataFrame 注册进 DuckDB 的 replacement scan（临时表），
    再建视图包一层，使视图名在连接内稳定可查（Soda 需要一个真实的表名/视图名）。
    """
    tmp = f"__tmp_{view_name}"
    try:
        con.register(tmp, df)
        con.execute(f'CREATE OR REPLACE VIEW "{view_name}" AS SELECT * FROM "{tmp}";')
    except Exception as exc:
        raise LoadError(f"DataFrame 注册失败（视图 {view_name}）：{exc}") from exc


def fetch_dataframe(
    con: duckdb.DuckDBPyConnection,
    view_name: str,
) -> pd.DataFrame:
    """从 DuckDB 视图读回 DataFrame。"""
    try:
        return con.execute(f'SELECT * FROM "{view_name}"').df()
    except Exception as exc:
        raise LoadError(f"读取视图 {view_name} 失败：{exc}") from exc


def view_exists(con: duckdb.DuckDBPyConnection, view_name: str) -> bool:
    """判断视图/表是否存在。"""
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [view_name],
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def list_view_columns(con: duckdb.DuckDBPyConnection, view_name: str) -> list[str]:
    """列出视图的列名（用于诊断列名映射问题）。"""
    try:
        rows = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [view_name],
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _write_temp(file_bytes: bytes, filename: str, suffix: str) -> str:
    """把字节写入临时文件并返回绝对路径。"""
    import tempfile

    tmpdir = tempfile.gettempdir()
    safe = re.sub(r"[^\w.\-]+", "_", filename)
    path = os.path.join(tmpdir, f"audit_helper_{os.getpid()}_{safe}")
    if not path.lower().endswith(suffix):
        path += suffix
    with open(path, "wb") as fh:
        fh.write(file_bytes)
    return path


def _escape_path(path: str) -> str:
    """转义 SQL 字符串中的单引号。"""
    return path.replace("\\", "/").replace("'", "''")


def detect_encoding(file_bytes: bytes) -> str:
    """粗略探测编码，优先 UTF-8-SIG（Excel 导出的 CSV 常见），回退 GBK（中文环境常见）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            file_bytes.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def read_dataframe(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """
    直接用 pandas 读取文件（不走 DuckDB）。

    用途：上传阶段的"输入校验"需要快速拿到列名与缺失情况，
          用 pandas 读更轻量，也避免为一个只读的检查动作去装 excel 扩展。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".xlsx", ".xls"):
        try:
            return pd.read_excel(io.BytesIO(file_bytes), dtype=str)
        except Exception as exc:
            raise LoadError(f"Excel 解析失败：{exc}") from exc
    enc = detect_encoding(file_bytes)
    try:
        return pd.read_csv(io.BytesIO(file_bytes), dtype=str, encoding=enc)
    except Exception as exc:
        raise LoadError(f"CSV 解析失败（编码 {enc}）：{exc}") from exc
