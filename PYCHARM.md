# 在 PyCharm 中打开本项目

项目路径：`C:\AuditHelper`　解释器：`C:\AuditHelper\.venv`（Python 3.11.4）

---

## 一、打开项目

PyCharm → **File → Open** → 选 `C:\AuditHelper` → OK。

如果 PyCharm 提示 “Trust Project”，选 **Trust**。

打开后右下角状态栏应显示解释器为 `Python 3.11 (AuditHelper)`。
如果显示 “Invalid SDK” 或红字，按下面「三、解释器没认出来怎么办」处理。

---

## 二、运行

### 方式 1：直接用右上角运行配置（推荐）

PyCharm 右上角的下拉框里已经预置了两个配置：

| 配置名 | 作用 | 结果 |
|---|---|---|
| **启动 审计助手 (Streamlit)** | 启动网页界面 | 终端输出本地地址，浏览器自动打开 |
| **端到端自测 (test_pipeline)** | 跑全流程自测 | 终端输出对账 + 契约校验结果 |

选中后点绿色 ▶ 即可。

**启动后地址**：<http://localhost:8501>

### 方式 2：终端手敲

在 PyCharm 底部的 **Terminal** 里：

```bash
# 启动界面
python -m streamlit run app.py

# 跑自测
python test_pipeline.py
```

确认终端提示符前面有 `(.venv)`；没有的话先执行 `.venv\Scripts\activate`。

### 方式 3：直接运行 app.py

右键 `app.py` → **Run 'app'** 会报 `missing ScriptRunContext` 之类的警告并空跑，
因为 Streamlit 脚本必须经 `streamlit run` 启动。**请用方式 1 或 2。**

---

## 三、解释器没认出来怎么办

若出现 `Invalid Python SDK` / 解释器变红，手工指定一次即可：

1. **File → Settings**（或 `Ctrl+Alt+S`）
2. 左侧 **Project: AuditHelper → Python Interpreter**
3. 右上齿轮 → **Add...**
4. 选 **Existing**（已有环境）→ 类型选 **Virtualenv** 或直接选 **Python**
5. 解释器路径填：`C:\AuditHelper\.venv\Scripts\python.exe`
6. OK

之后 `Project Interpreter` 列表里应能看到 `duckdb`、`pandas`、`streamlit`、
`soda-core`、`openpyxl`、`pyyaml` 等包。

---

## 四、界面怎么用（五步）

1. **上传** — 左侧「使用示例对账单」「使用示例总账」可一键载入 `sample_data/` 里的样例；
   也可上传自己的 CSV / Excel。列名不必统一，程序会自动识别中文/英文别名。
2. **输入校验** — Soda v4 检查日期/金额/参考号三列有无缺失、日期能否解析、金额是否为数值。
   有问题会在这一步被拦住。
3. **对账** — 两轮匹配：① 同日 + 同额 + 同参考号；② 日期容差内 + 金额容差内。
   产出 `match_status`（matched / bank_only / gl_only / mismatch / duplicate）。
4. **输出校验** — 校验 `match_status` 取值合法、`amount_diff` 无缺失，
   并检查未达账项笔数是否超过阈值。
5. **下载** — 导出对账明细 CSV 与银行存款余额调节表（BRS）。

---

## 五、换成自己的数据要改哪里

**只改一个文件：`config.py`**。其余文件不需要动。

| 配置项 | 用途 |
|---|---|
| `BANK_COLUMN_ALIASES` | 对账单的列名别名（中英文都列上） |
| `GL_COLUMN_ALIASES` | 总账的列名别名 |
| `DEFAULT_DATE_TOLERANCE_DAYS` | 日期容差天数（默认 3） |
| `DEFAULT_AMOUNT_TOLERANCE` | 金额容差（默认 0.01） |
| `DEFAULT_AMOUNT_MODE` | 借贷方向策略：`strict` / `absolute` / `sign_norm` |
| `MAX_BANK_ONLY_ROWS` / `MAX_GL_ONLY_ROWS` | 未达账项告警阈值（0 = 只要有一笔就告警） |

契约 YAML 里的**列名**要和你的实际表头一致。若表头是中文，
可同步改 `adapter.py` 里 `build_output_contract()` 的列名参数，
或直接把 `contracts/*.yml` 里的列名改成你的表头。

**导出文件中文化**：导出 CSV / Excel 里的列名和状态值已默认转为中文
（匹配成功 / 仅银行有 / 仅总账有 / 金额日期不符 / 重复记录），措辞在
`config.py` 第 6 节「导出中文化」配置。程序内部仍是英文标准名，
对账匹配与契约校验不受影响。

---

## 六、目录说明

```
C:\AuditHelper\
├── app.py                      Streamlit 入口（五步流程）
├── loader.py                   DuckDB 连接 + CSV/Excel 注册为视图
├── reconciler.py               纯 pandas 两轮对账匹配
├── soda_runner.py              封装 Soda v4 verify_contract_locally + 降级内核
├── adapter.py                  落表 → 生成契约 → 校验 → 汇总
├── config.py                   ★ 配置中心，改这里
├── test_pipeline.py            端到端自测
├── contracts/                  三份契约 YAML
│   ├── input_bank_statement.yml
│   ├── input_gl_ledger.yml
│   └── output_reconciled.yml
├── sample_data/                样例数据
├── requirements.txt
├── README.md                   项目技术说明
└── PYCHARM.md                  本文件
```

---

## 七、常见问题

**Q：`ModuleNotFoundError: No module named 'xxx'`**
A：解释器选错了。按「三」重新指定 `C:\AuditHelper\.venv\Scripts\python.exe`。

**Q：终端里 `python` 找不到**
A：用完整路径 `C:\AuditHelper\.venv\Scripts\python.exe`，或先激活 `.venv\Scripts\activate`。

**Q：端口 8501 被占用**
A：`python -m streamlit run app.py --server.port 8502`。

**Q：输出校验里出现红色 `❌ bank_only 不超过 N 笔`**
A：这是**正常**的。样例数据本身就设计了 3 笔未达账项，超过默认阈值，
用来演示「契约能拦下异常数据」。想让样例全绿，把 `config.py` 里
`MAX_BANK_ONLY_ROWS`、`MAX_GL_ONLY_ROWS` 调大即可。

**Q：`soda-duckdb` 装不上 / Soda 报错**
A：程序内置了纯 pandas 降级内核，会自动接管校验，功能等价只是提示信息略简。
`test_pipeline.py` 会打印当前用的是哪个引擎（`engine=soda` 或 `engine=fallback`）。

---

## 八、网页打不开？按这张表排查

启动后浏览器没弹出来、或页面打不开，**先看 PyCharm 底部运行窗口的输出**。

**第 1 步：找 `Local URL: http://localhost:xxxx` 这一行**

- 找得到 → 服务其实已经起来了，把这个地址复制到浏览器打开即可（端口不一定是 8501）。
- 找不到 → 服务没启动成功，对照下表看报错。

**第 2 步：报错对照表**

| 控制台现象 | 原因 | 处理 |
|---|---|---|
| `No module named streamlit` | 运行用的不是项目 venv | 按「三」把解释器指到 `C:\AuditHelper\.venv\Scripts\python.exe` |
| `can't open file ... 'run'` 或参数拼错 | 旧版运行配置写法不兼容 | 运行配置已改为模块模式；**完全退出 PyCharm 再重开项目**后重试 |
| `Invalid Python SDK` | 解释器丢失 | 同「三」 |
| `Port 8501 is already in use` | 端口被占（如已开着一个实例） | Streamlit 会自动换 8502，以控制台 `Local URL` 显示的端口为准 |
| 输出一闪就结束、没有 URL | 跑的是「端到端自测」配置 | 右上角切换到「启动 审计助手 (Streamlit)」再点 ▶ |
| 页面一直空白/转圈 | 前端资源还在加载 | 等 10 秒后按 Ctrl+F5 强制刷新 |

**第 3 步：兜底方案（不依赖 PyCharm）**

双击项目目录里的 **`启动审计助手.bat`**，等浏览器自动打开，或手动访问 <http://localhost:8501>。
关不掉就关掉那个黑窗口；想换端口就右键编辑该文件，把 `run app.py` 改成 `run app.py --server.port 8502`。
