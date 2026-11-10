# 本轮改动说明（2026-10-23 第五轮，build r17-refalign）

## R17 关键修正：恢复 base_url / model 默认值（**推翻我前一轮的判断**）

逐项对比 `submission_old_2485s.zip`（r4，那次 LLM 真的在跑）与官方参考实现后，发现我把
「合规」理解过头了：

官方 `reference_submission02/submission/scan_agent.py` 原文就是：

```python
"api_key":  os.environ.get("LLM_API_KEY",  "sk-your-api-key-here"),
"base_url": os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
"model":    os.environ.get("LLM_MODEL",    "deepseek-v4-pro"),
```

配合参考 README 那句「**参赛队可以只在 .env 中填写 LLM_API_KEY**」可知：
**地址与模型名本来就是主办方固定的，允许作为代码默认值；参赛队只提供 key。**

我上一轮把这三种默认值全删了，还要求三者齐全，后果很严重：
一旦平台**只注入 `LLM_API_KEY`**（这正是参考用法），我们会把 base_url/model 判空 →
`llm_available()` 为假 → 整条 LLM 能力**静默失效**，日志只留一句「缺少环境变量」。

**改法**（`config.py`）：
- 恢复 `_DEFAULT_BASE_URL` / `_DEFAULT_MODEL`，取值与官方参考实现逐字一致。
- `llm_available()` 回到「**只看 key**」：有 key 就建客户端（地址/模型有默认值兜底）。
- key 依然**没有默认值**（占位符也视为空），所以「无公网乱连公网地址」的风险不成立：
  没有 key 就不构造客户端，一个请求都不会发。

对比结论（与 r4 逐项）：

| 项 | r4（2485s） | r17 | 说明 |
|---|---|---|---|
| `.env` 位置 | 包根 | 包根 | 一致 |
| `.env` 内容 | 有 key（已过期） | `LLM_API_KEY=` 留空 | 由平台注入，符合要求 |
| requirements | openai/httpx/requests/pypdf | 同 | 完全一致 |
| Dockerfile | 无 make | 补 `make` | r4 的 LEC 会失败，这是修好的地方 |
| base_url/model 默认 | 有（dashscope/deepseek-v4-pro） | **恢复为同值** | 上一轮误删，已纠正 |
| 入口脚本 | `python3 scan_agent.py` | `python3 -m scan_agent` | 等价，`-m` 更稳 |
| LLM 可用判据 | 有 key 即可 | **同** | 已对齐 |

验证（7 种组合，接口桩）：

```
LLM_* 根地址        -> SDK base_url=…/v1, model=注入值        ✓
LLM_* 已带 /v1      -> 不重复拼接                              ✓
LLM_* /v1/ 尾斜杠   -> 归一化                                  ✓
OPENAI_* 别名       -> 接住                                    ✓
只注入 key          -> base_url=dashscope…/v1, model=deepseek-v4-pro  ✓（与参考一致）
占位符 key / 全缺   -> 判不可用，且不构造客户端                 ✓
```

## 回归（r17）

编译 OK；case6 冒烟 PASS；marker e2e PASS；泛化门槛 PASS；预算压测 PASS；DRC 熔断 PASS；
LLM 接线 PASS（7 场景）。

---

# 上一轮（2026-10-23 第四轮，build r16-sdkcompat）

## R16-1 目录结构按官方参考示例对齐（`reference_submission02.zip`）

官方参考包的**真正提交体**是扁平结构（外面那层 `reference_submission/` 只是分发包装）：

```
.env            ← 与 Dockerfile 同级，在包根目录
Dockerfile
README.md
submission/     ← 只有代码
```

据此修正（上一轮我把 `.env` 搬进 `submission/` 是错的）：
- `.env` 回到**包根目录**；`submission/.env` 删除。
- `.env` 形态与示例一致：保留一行**空赋值** `LLM_API_KEY=`（本地调试可直接填），
  地址与模型名保持注释。空值不等于凭据，符合 8.1「不得写死」。
- 打包脚本的第 3 项检查改为：允许空赋值，**只拦截有非空取值**的项。
- 其余顶层文件：`Dockerfile`、`README.md`、`.dockerignore`、`CHANGES.md`。

> 入口脚本只与示例差一处：我们用 `python3 -m scan_agent`（示例是 `python3 scan_agent.py`），
> `-m` 会把工作目录加入 `sys.path`，对包内相对导入更稳，故保留。

## R16-2 依赖版本风险：平台装到 openai 3.37.0，参考示例 pin 1.12.0

从平台 `build.log` 查到实际解析结果：**`openai 3.37.0`**、httpx 0.28.1、pypdf 6.21.2。
而组委会参考示例 pin 的是 `openai==1.12.0` —— 跨了大版本，且测试环境没注入 key，
**LLM 路径从未被真正执行过**。

**为什么不直接改 pin**：构建成功是硬前提（build 失败 = 全盘 0 分），
而本地连不上 PyPI（代理 502），无法验证某个 pin 组合能否构建。所以不动依赖，改为两层自保：

1. **可观测**：启动横幅打印 `openai SDK <版本>`，出问题时一眼分清是「没注入」还是「SDK 变了」。
2. **参数名回退**：`chat()` 若因 `max_tokens` 抛 `TypeError`，自动改用
   `max_completion_tokens` 重试一次（新版 SDK 的重命名）。

验证（用桩模拟 3.37.0 的拒绝行为）：

```
第一次调用参数: max_tokens, messages, model, temperature, timeout
→ TypeError（SDK 不认 max_tokens）
第二次调用参数: max_completion_tokens, messages, model, temperature, timeout
→ 返回正文 OK-FALLBACK，调用计数 2，未退化到规则路径
```

## 回归（r16）

编译 OK；case6 冒烟 PASS；LLM 接线 PASS（6 场景）；`max_tokens` 回退 PASS。

---

# 上一轮（2026-10-23 第三轮，build r15-llmenv）

## R15 复盘：为什么旧包 2485s，新包 943s

对比 `dist/submission_old_2485s.zip`（build `2026-10-12-r4`）与本轮包，差异在 **LLM 有没有真的跑起来**：

| | 旧包 r4（2485s） | 本轮（943s） |
|---|---|---|
| API Key 来源 | `.env` 里有一把 一把 `sk-***` 开头的 key（**已过期，实际不可用**） | 无，全靠环境变量注入 |
| LLM 实际来源 | **平台注入**（自带 key 已失效，所以只能是注入生效） | 测试环境**未注入** → 降级规则路径 |
| 单轮工具上限 | `_TOOL_TIMEOUT_CAP=3600` | 900 |
| 预算解析失败 | 不设上限 | 1200s 兜底 + 5400s 绝对看门狗 |

结论修正（最初判断有误，记录在此以免再错）：
- 2485s **不是**「自带 key 在跑」，那把 key 早已过期；**是平台注入在起作用**，
  说明真实评测确实会注入 `LLM_*`。LLM 每轮调模型（单次 48~148s）+ 工具单轮可跑一小时，
  叠出了 2485s。
- 943s 是因为「测试4」那次环境**没有注入** `LLM_*`，全程走确定性规则路径。
  两个数不是同一套条件，不能直接比。

> `submission_old_2485s.zip` 里那把 `sk-***` 虽已过期，仍是明文凭据且已提交过平台，
> 建议确认已在服务端吊销 —— 与 `.env` 注释里记的历史泄露是同一类问题。

## R15 改动：LLM 环境变量接受通用别名（**绝不覆盖注入值**）

**前提**：真实评测会注入 `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`，
而这份代码的第一原则是**不去干扰注入**：
- `.env` 加载只用 `os.environ.setdefault` —— 已存在的键**永不覆盖**，注入值优先；
- 全仓库没有任何 `os.environ[...] =` 或 `putenv` 去改写 `LLM_*`；
- 不构造任何默认凭据/地址/模型；拿不到就降级，绝不朝猜测的地址发包。

**动机**：不同脚手架也常用 `OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL`。
变量名对不上 → 静默降级为纯规则路径 → 丢掉 LLM 那部分分，而且日志里只写
「缺少环境变量」，很难当场判断是没注入还是名字不符。

**改法**（`config.py`）：
- `_env_first()` 按序取第一个非空**且非占位符**的值：
  - key：`LLM_API_KEY` → `OPENAI_API_KEY`
  - base：`LLM_BASE_URL` → `OPENAI_BASE_URL` → `LLM_API_BASE`
  - model：`LLM_MODEL` → `OPENAI_MODEL`
  （`LLM_*` 始终优先，别名只是兜底）
- 新增 `_PLACEHOLDER_VALUES` / `_is_placeholder()`：`sk-your-api-key-here` 这类占位符
  在 `.env` 加载时就**不写入** `os.environ`（否则它会通过 `setdefault` 抢占，
  把真正注入的 `OPENAI_*` 顶掉 —— 这个坑是写测试时实际撞出来的）。
- 启动日志会打印归一化后的 `base_url` 与 `model`（不含 key），便于当场核对。

**接线验证**（`tools/_llm_wire_test.py`，用接口桩替换 SDK，本机装不上 openai）：

```
LLM_*  根地址     -> SDK 收到 base_url=…/v1, model=注入值, max_retries=0  ✓
LLM_*  已带 /v1   -> 不重复拼接                                          ✓
LLM_*  /v1/ 尾斜杠 -> 归一化为 /v1                                       ✓
OPENAI_* 别名     -> 同样被接住                                          ✓
占位符 key        -> 判不可用，且**不构造客户端**（不发任何请求）          ✓
全缺              -> 判不可用，且不构造客户端                             ✓
```

## 回归（r15）

编译 OK；case6 冒烟 PASS；marker e2e PASS；泛化门槛 PASS；预算压测 PASS；
DRC 熔断 PASS；LLM 接线 PASS。

---

# 上一轮（2026-10-23 第二轮，build r14-drcfeed）

> 依据：平台「测试4」实测结果（11 个 case 格式/运行全通过，total_time 942.98s）。
> 但**通过 ≠ 做对** —— 逐 case 看 DRC，有两个真失败。本节针对这两个失败。

## R14-1 task_1/case5：DFTR1×69022（最大问题）

**现象**：openC906（37325 模块 / 650074 时序单元）首轮报 `DFTR1×69022`，
耗时 355s，然后**直接收工**，没有第二轮。

**根因 1（时钟域未识别）**：生成的 dofile 里写着
`set_scan_cfg -max_length 500 -mix_clocks false -internal_clocks none`。
手册 p16/p28：`none` = **不识别内部时钟域**。openC906 内部有大量 ICG/分频产生的
时钟域，不识别 → 这些域里的 FF 全被判「时钟不可控」→ DRC 直接上万条，
此时改任何 `set_scan_signal` 都不可能收敛。

这个 `none` 来自 `_apply_pseudo_pi` 里的一句 `setdefault`，是照抄某个 case 的
golden 写法 —— 典型的「只对现有用例有效」。

**改法**：新增 `dofile_generator.repair_internal_clocks_for_drc()`，用 **DRC 反馈**
决定档位（不再预设立场）：
| 轮次 | 触发条件 | 改动 |
|---|---|---|
| 第 1 次 | DFTR1 ≥ 100 且当前为 `none` | → `multi`（识别任意组合门后的内部时钟域） |
| 第 2 次 | 仍 ≥ 100 | 追加 `-mix_internal_clocks true` |
| 之后 | — | 不再尝试，交回原流程（避免无谓烧预算） |

阈值 100：个位数属于单个 FF 的问题，交给常规修复；上百条说明是整体时钟域没识别。

**根因 2（确定性修复被 LLM 挡死）**：旧代码给这个修复分支加了
`has_useful_time(llm)` 门槛，而它在 **LLM 不可用**时返回 False。本次平台测试
恰好没注入 `LLM_API_KEY`，于是 69k 条违例之后一次修复都没试就 break。
`repair_internal_clocks_for_drc` 与 `ensure_dofile_integrity` 都是纯确定性计算，
只该受**时间预算**约束 —— 门槛已改为只判 `time_is_up(task)`。

## R14-2 task_2/case2：DFTR10×21 被判「未完全成功」

`drc_residual_rules()` 只做**行内**匹配（允许词与规则号必须在同一行且在规则号之前）。
任务书里若是表格形态（`| DFTR10 | … | 允许 |`）或跨行列举
（「其余固有违例：\nDFTR10、DFTR11」），就漏判 → 残留违例被算作失败 →
summary 写「最终未完全成功」，还多烧一轮修复。

**改法**：改为**段落级**匹配（空行分隔），并加否定保护：

```
允许遗留 DFTR10 的违例，无需处理        -> ['DFTR10']   ✓
| DFTR10 | clock 接 data 端 | 允许 |    -> ['DFTR10']   ✓
其余固有违例 \n DFTR11 \n 可忽略不计     -> ['DFTR11']   ✓
禁止允许 DFTR1 残留                     -> []            ✓（否定句不采信）
允许遗留 DFTR10 \n 禁止残留 DFTR1        -> ['DFTR10']   ✓（逐行判否定）
```

## 回归结果（build r14）

| 测试 | 结果 |
|---|---|
| 全量编译 | 通过 |
| 新版 case6 冒烟 | PASS |
| marker 协议端到端 | PASS |
| 泛化门槛三情形 | PASS |
| 内部时钟档位阶梯（none→multi→mix→停） | PASS（含小规模不触发、无该选项时追加） |
| 允许残留 6 种写法 | PASS（含否定句） |
| 预算压测 / DRC 熔断 | PASS |

> 注意：case5 的 `internal_clocks multi` 是否真能消掉 69022 条 DFTR1，
> **只能下一次平台跑才知道**（本机无 dftexp_scan）。若不奏效，第二级
> `-mix_internal_clocks true` 会接着试，仍不行则停止并保留产物。

---

# 上一轮（2026-10-23 第一轮，build r13-marker）

> 以下按「日期倒序」记录。**最新一轮在最前**。

## R13：对齐《赛题评测相关补充说明(10.22)》+ 为 5 个新 case 做泛化

### 0. 最关键：补上完成标志 `.case_complete_marker`（很可能就是「2 小时超时」的真因）

补充说明第 2 节定义：评测系统**在 output 根目录检测到 `.case_complete_marker`
才记 t2（完成时刻）并归档/停容器**。

旧代码**完全没有写这个文件** —— 它只等过就绪标志，跑完就 `return`。后果是：
agent 明明几分钟就跑完了，评测系统却永远等不到完成信号，只能一直等到 case 的
最大时限，然后判「超时 fail」。这与平台观测到的「跑满两小时 fail」完全吻合，
比之前怀疑的「工具单轮上限 3600s」更直接。

**改法**（`scan_agent.py`）：
- `main()` 用 `try / except / finally`，完成标志在 **`finally`** 里写 ——
  正常返回、异常兜底（`_emergency_finalize`）、`SystemExit` 三种出口都会写。
- 写之前不再做任何耗时操作；写完立即 `os.sync()`，避免归档早于落盘。
- 就绪标志同步更新：评测用 `case_upload_marker`（原 `.case_ready` 保留为兼容别名）。

### 1. 合规：不再在镜像里写死 LLM 凭据 / 地址 / 模型名

- `config.LLM_CONFIG` 三项**全部**只从环境变量取，**删除默认值**
  （原默认 `https://dashscope.aliyuncs.com/...` + `deepseek-v4-pro`）。
  容器无公网，默认值一旦生效就是「连一个不可达地址 → 反复超时」。
- `.env` 按**示例包约定**放在**包根目录**（与 Dockerfile 同级，不在 `submission/` 内），
  内容全部注释、无任何生效赋值。Dockerfile 只 COPY `submission/`，所以它**不进镜像**，
  只是仓库级开发兜底；镜像内运行时完全靠评测系统注入。
- `llm_available()` 要求三变量齐全，缺任一即降级到确定性路径，并在启动日志
  逐项列出缺哪个，便于事后区分「没注入」还是「注入格式不对」。

### 2. 修复：`LLM_BASE_URL` 缺 `/v1` 会导致 LLM 全废

补充说明第 4 节：`LLM_BASE_URL` 是**网关根地址**，OpenAI 兼容客户端必须请求
`${LLM_BASE_URL}/v1/chat/completions`。旧代码把根地址原样交给 openai SDK，
实际会打 `/chat/completions` → 404。新增 `_normalize_base_url()` 幂等补 `/v1`。

### 3. 泛化：ECO 触发判据从「spec 措辞」改为「措辞 OR 行为」

比赛会给 5 个全新 case，spec 措辞不可预知。旧门槛只认
「网表缺陷 / ECO / disconnect_net…」这类词，新 case 若写成
「DFF 时钟不可控」就永远进不了 ECO 路径（表现为怎么修都不降）。

新判据（`_eco_inject`）：
1. spec 明确提网表 → 立即 ECO（强信号，快路径）；
2. spec 没提 → **先让 dofile 层修复试一次**，仍不降才转 ECO（行为判据）。

`dofile_fix_attempts` 计数在 `_run_task2_impl` 循环里维护。这样既不抢跑
（当初加硬门槛是为了保护 case3），也能覆盖措辞不同的新 case。

### 4. 泛化：时钟 / 模式端口改为「spec 优先，网表反推兜底」

- `discover_clock_port()`：spec 没写扫描时钟时，从顶层 input 端口里找
  「真的被 FF 时钟脚引用」的那个（再退化为名字像 clk 的）。
  旧实现 `if not clock_port: return None` 会让 DFTR1 的**整条 ECO 路径静默失效**。
- `_top_input_ports()` 同时支持 **ANSI 与非 ANSI** 两种 Verilog 端口写法。
  旧实现只认 ANSI，而 case6 恰是非 ANSI（`module led_drv ( clk, ... );` +
  体内 `input clk;`），实测返回空列表。
- 模式端口：spec 点名 → 否则取「无负载且名字像测试模式
  （scan_mode/test_mode/scan_en/se/tm…）」的端口。

### 5. 官方 LEC 参考链的启示（case6 (10.23) 新增 yosys/ 目录）

新版 case6 输入文件与旧版**逐字节一致**，新增的是 `yosys/` 参考验证链：
`yosys miter → write_btor → add_constraint.py → btormc`，命题是
「功能态（scan_mode=0）下 `netlist_fixed.v` 与原网表顶层等价」。

这印证了两点：① `netlist_fixed.v` 是 dftexp_scan 执行 dofile 中 ECO 命令后的
**产物**，所以「把 ECO 命令写进 dofile」正是命题方预期形态（不是让 LLM 重写网表）；
② 等价性只要求功能态，与我们的 MUX 接法（A0=原逻辑、S=模式端口）一致。

> 本机无 yosys / btormc，无法跑这条链；已将其作为平台侧的最终判据。

## 验证状态

| 项目 | 结果 |
|---|---|
| 全量编译 | 通过 |
| 新版 case6（10.23）冒烟 | PASS：28 违例 → ECO 2 处 → 次轮 0 违例 |
| marker 协议端到端（走真实 `main()`） | PASS：正常路径 rc=0 且写 marker；异常路径 rc=1 **仍写** marker；就绪标志真等待 |
| 泛化门槛三情形（无提示/已试过/有提示） | PASS |
| 时钟端口反推 | PASS：case6 反推为 `clk`（非功能时钟 `clk_a`） |
| 预算压测 / DRC 熔断 | PASS（挂死 40.5s 被切；恒 28 第 3 轮熔断） |
| 真实 dftexp_scan + 官方 yosys LEC | **未做**——本机 Docker、yosys、btormc 均不可用 |

---

# 上一轮（2026-10-21）改动说明

基线：`workpack/submission`（DeepSeek 评审后的 P0 版本）。
本轮针对**新增的 task2/case6（网表 ECO 修复型用例）**做适配。

## 验证状态（先说清楚）

| 项目 | 状态 |
|---|---|
| 全量语法编译（`compileall`） | 通过 |
| case6 离线冒烟（假工具 + 假 LLM，跑真实 `run_task2`） | PASS：28 违例 → 注入 ECO → 次轮 0 违例 → 收工 |
| ECO 命令与官方 `golden.dofile` 逐条对照 | 16/16 拓扑一致（差异仅新实例/新 net 的命名） |
| task2 case1~case4 时钟声明 + ECO 门槛回归 | 时钟均为真实顶层端口；ECO 门槛全部拦截（不误触发） |
| **真实 dftexp_scan 运行** | **未做**——本机 Docker 守护进程不可用 |
| case5 | 本地 12GB 内存下 `extract_facts` 被 OOM kill，未验证 |

**结论：逻辑与结构层面已尽可能验证，但「工具是否接受这些 ECO 命令」「DRC 是否真的清零」
只能由平台日志回答。** 若平台报错，按文末「排查指引」定位。

---

## 1. 会从第一行崩掉：内部信号被当成扫描时钟

**现象**：case6 的 `task_spec` 写「`u_pwm` 组 FF 的时钟经 ICG 门控（`GCLK`）」，
时钟表格抽取把 `GCLK` 抓成时钟端口，dofile 写出：

```tcl
set_scan_signal -type clock -port GCLK -off_state 0
```

工具直接 `[ERROR] SCAN-1315 Port 'GCLK' does not exist in the design`，后续命令级联失败。
（这是上一轮真跑实际观测到的错误。）

**根因**：`GCLK` 是 ICG 单元的**输出引脚名**，不是顶层端口。

**改法**（`dofile_generator.py`）：
- `_drop_non_port_clocks()`：在 `_rule_extract_understanding` 返回前统一过滤
  （此时 4 条时钟来源已合并完毕，一次拦全）。
- `sanitize_clock_ports()`：在 `normalize_dofile_layout` **最后**再清一遍——
  因为 `spec_fixer` 会按 spec 原文把 GCLK 重新补回来，只过滤 understanding 拦不住（冒烟实测）。

## 2. 功能时钟被当成扫描时钟（与任务书禁止项正面冲突）

**现象**：`set_scan_signal -type clock -port clk_a`。

case6 的 spec 把「把 clk_a 追加声明为扫描时钟」明确列入**禁止修复方式**
（"实测无效仍 28 条"），官方 golden 也只声明 `clk`。旧抽取把 spec 里
「`clk_a` 是 u_stat 组的功能时钟」也当成扫描时钟。

**改法**：新增 `drop_functional_clock_ports()`，按**子句**（不是整行）判断：
某端口所在子句含「功能时钟」且另有端口被称「扫描时钟」时剔除。
必须按子句，因为 case6 的同一行里两者并存（按整行会让两个端口同时命中）。
`sanitize_clock_ports()` 里再做一次同样的最终清洗。

## 3. ECO 命令永远轮不到执行（控制流顺序错误）

**现象**：冒烟首轮日志显示 `↻ 已针对性修复 dofile，重试` —— 走的是 dofile 补丁，
新加的 ECO 注入根本没执行。

**根因**：ECO 代码被放在了 `dofile_fixer.targeted_fix` **之后**。只要 dofile
补丁还能产出任何改动就会 `continue`，ECO 永远轮不到。这正是 case6 一直
「反复改声明、DRC 恒 28」的机制。

**改法**（`scan_agent.py`）：把确定性 ECO 提到 dofile 修补**之前**（标为 ①，dofile 修补为 ②）。

## 4. ECO 路径用模块名而非实例路径（会全部找不到对象）

**现象**：生成 `stat_unit/nclk_a`，但网表里 `module stat_unit` 的实例名是 `u_stat`，
FF 的真实层次路径是 `u_stat/status_reg[0]`，golden 也写 `u_stat/nclk_a`。

**改法**：新增 `_hierarchy_prefixes()`（模块 → 顶层实例前缀，BFS 自顶向下），
所有 ECO 命令统一用 `_fp(prefix, name)` 拼接。同一模块被多处实例化时判定有歧义，
该组直接跳过（不猜）。

## 5. 新增：确定性网表 ECO 推导

**为什么**：此前 task2 改网表只有一条路——让 LLM **重写整份网表**再跑 LEC。
case6 真跑证明它会把顶层 `scan_mode` 端口声明漏掉（随后 `connect_pin` 报 SCAN-1315），
改动面不可控。而官方 golden 的修法就是工具自带的 ECO 命令，写在 dofile 里。

**分工**（LLM 并没有被拿掉）：

| 环节 | 谁做 |
|---|---|
| 判断根因、选修法 | **LLM**（`diagnose_with_llm`，仍是主诊断路径） |
| 查准 pin/net/驱动关系 | 确定性代码（网表事实） |
| MUX 的 A0/A1/S 接法 | 规则（任务书已固定） |
| 结构识别不出时 | **LLM**（原有 `_try_netlist_fix` 兜底保留） |

**新增函数**（`lec_verifier.py`）：`derive_clock_path_eco()`（DFTR1，时钟穿组合门）、
`derive_icg_test_enable_eco()`（DFTR9，ICG 的 TE/SCE 被功能链占据）、`derive_netlist_eco()`。

接法严格按任务书「MUX 接法约定 A/B 统一」：
`A0=原连接（功能态 scan_mode=0 透传）`、`A1=测试逻辑（clk / tie1）`、
`S=测试模式端口（scan_mode）`、`X→原 net`，**被修引脚连接一律不动**。

**硬门槛**（重要，防误伤）：`_eco_inject` 要求 `_needs_netlist_eco(spec)` 为真，
即 spec 明确写「缺陷位于网表」「必须用网表编辑（ECO）命令」。
原因：**task2/case3 的 DRC 同样是 DFTR1**，且 spec 也提到一个无负载端口
`scan_cg_en`——只看「规则号 + 有模式端口」会把 case3 判成需要 ECO，
抢在它原本正确的 dofile 修复之前动手，把能过的 case 改坏。
实测门槛：case1/2/3/4 全 False，仅 case6 True。

与 golden 逐条对照：16 条命令**拓扑完全一致**，仅新实例/新 net 的命名不同
（`eco_clkmux` vs `U912`、`nclk_a_src` vs `nclk_src`）；两条 `disconnect_net`
因引用已有对象而逐字相同。

## 6. 修正：`set_scan_drc_rule_handling {DFTR-TIE1 DFTR-TIE0} Ignore` 被误判违规

**现象**：每轮都报「不得通过 DRC 豁免掩盖违例」，永远不收敛。

**根因**：case6 的 spec 写「不得用 DRC 规则豁免（`set_scan_drc_rule_handling` 对
**DFTR1/DFTR9**）消除违例」，同时又要求保留 original.dofile 里的 TIE 豁免（工具口径，
与违例无关）。旧判据见到同一行里既有「豁免」又有「DFTR」就判断任务要求豁免，
但该行后半句有「（如 `add_pseudo_pi`…）」触发了转义，于是反转成「全禁」。

**改法**（`spec_verifier.py`）：新增 `_spec_forbidden_ignore_rules()`，
精确解析「禁止对其设豁免的规则号」，断言改为
`不得对 DFTR1、DFTR9 设 DRC 豁免`（只禁这两个规则，不误伤 TIE）。
`_spec_asks_drc_ignore()` 同时排除禁止句。
**验证**：golden（含 TIE 豁免）→ 0 违规；人工注入 `{DFTR1 DFTR9} Ignore` → 命中。

## 7. 修正：缺 `dofile_path` 时抛 `IsADirectoryError`

`Path(run.get("dofile_path", ""))` 在缺键时等于 `Path(".")`，`.exists()` 对目录也为 True，
随后 `read_text` 抛 `IsADirectoryError`。`output_manager.py` / `decision_logger.py`
共 5 处，统一改为 `_dofile_file()`（`is_file()` 守卫）。冒烟时实际踩到。

## 8. ECO 命令手册真正进 RAG

`manual/md/*.md`（20 份）此前虽进索引但整篇当一个「页」，标题是 `### JsonConfig`，
命令名只在正文，检索时被 PDF 小节压过。改法：
- 按 `## 命令名` 切块；`### Syntax` 等子标题保留在正文；
- `_COMMAND_RE` 补 `connect|disconnect|rewire|get` 前缀
  （`connect_pin` 之前只被拆成 connect + pin，标题加成永远落空）；
- 复合标识符整体入倒排索引；md 定义节 ×2 加权；同源同节只取一段。

**验证**：`connect_pin`/`add_inst`/`rewire`/`disconnect_net` 均命中 md 定义节
（改前 `add_inst` 命中的是 PDF 的「2.2.4 对多个设计进行 Scan 插入」）。

## 9. ECO 场景的 RAG 查询

case6 的 spec 只写「修复其中位于网表的缺陷」，**一个命令名都没有**，原查询构造
查不到 ECO 命令。新增 `_needs_netlist_eco()` 命中特征时把
`disconnect_net connect_net add_net add_inst connect_pin` 加进查询。

---


## 10. 修正：决策记录声称「未修改网表」（自相矛盾）

**现象**：做了 ECO 之后，`decision_log.json` 的 summary 仍写
「本轮未修改网表，无需 LEC」。

**根因**：summary 只认 `netlist_changes`（LLM 重写网表那条路），不认 ECO 路径。

**改法**（`decision_logger.py`）：从 `fix_records[*]["eco"]` 识别 ECO 修改，
summary 如实写成「以工具网表编辑命令 ECO 修改 N 处，未单独运行 LEC」——
明确 LEC 未执行，不冒充做过等价性校验（docx 5.4.2 不得伪造结果）。

---

## 未做 / 已知风险

1. **P0-6（LEC 改用官方 `run.sh`/yosys/eqy）未做**：case6 官方验证链是
   `yosys miter → BTOR → btormc`，与现有 `run_yosys_lec`（`equiv_make`/`equiv_induct`）
   是两条不同的链；还需处理 ICG 降维、`clk2fflogic`、`setundef -undriven -zero` 的顺序坑。
   本轮无法验证，动它风险高于收益。**现有 LEC 仅服务于「LLM 重写网表」兜底路径。**
2. **`spec_verifier.build_assertions` 对 task2 断言偏少**（case6=1 条，case2=0 条）——
   这是基线既有状况，非本轮引入；意味着「DFT 配置符合任务要求」这一项检查偏弱。
3. **case5 未验证**（本机 OOM）。建议优先在平台跑 case5。
4. `add_net` 用层次写法 `u_stat/nclk_a_src`，与 golden 的 `u_stat/nclk_src` 同形，
   但新 net 名带层次前缀是否被工具接受，需平台日志确认（见排查指引 ①）。

---

## 排查指引（平台报错时按此定位）

| 平台现象 | 可能原因 | 改哪里 |
|---|---|---|
| ① `add_net` 报错/找不到层次对象 | 工具要求裸 net 名而非层次路径 | `lec_verifier._fp()` 或 `derive_*_eco()` 里 `n_src`/`n_tsel` 的拼法 |
| ② `add_inst` 报单元格不存在 | 库里 MUX/conb 命名不同 | `_MUX_NAME_HINTS` / `_TIEHI_NAME_HINTS` |
| ③ `connect_pin -from scan_mode` 报 SCAN-1315 | 顶层 scan_mode 端口声明丢失 | ECO 块必须紧跟 `present_design`，检查是否被布局重排移走 |
| ④ DRC 仍 28 条 | MUX 未生效（A0/A1 接反或 S 未接） | 对照 golden：A0=原逻辑、A1=clk/tie1、S=scan_mode |
| ⑤ 每轮「配置不符合任务要求」 | spec 断言误判 | 打印 `_check_spec_compliance` 返回的 requirement，按第 6 节方式修判据 |
