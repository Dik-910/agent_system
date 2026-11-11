# agent_system

基于大语言模型的 EDA Scan Insertion 智能体，用于 2026 年中国研究生创"芯"大赛 · EDA 精英挑战赛赛题二。

读取任务说明书与门级网表，自主生成、运行并修复 `dftexp_scan` 的 TCL dofile，完成从「插链配置」到「DRC 收敛 + 产物交付」的全流程。

## 功能

### 任务一：Dofile 生成

- 从 `task_spec.md` + 门级网表 + Liberty + 手册检索结果，规划完整的插链流程
- 网表事实抽取：DFF / SFF 类型分布、顶层时钟端口、已存在 SCE 端口、移位链（沿 Q→D 追到链尾）、ICG 与悬空时钟根、附加模块的端口语义
- LLM 输出结构化决策，由代码按白名单渲染成 TCL：命令合法性与实例/端口存在性先校验再落盘，杜绝幻觉命令
- 多候选择优：静态评分（基础命令齐备、需求覆盖、路径写法、工作流阶段顺序）后择优进入运行
- 多轮修复：错误按签名归并，签名重复或违例数不降即止损，避免「改了等于没改」的无效重跑

### 任务二：问题定位与修复

赛题把注入了故障的 `original.dofile` 分成三类：执行问题、DRC 问题、DFT 配置不符合任务要求。三类都覆盖：

- **LLM 诊断主路径**：一次调用同时产出结构化问题清单（现象 / 对象 / 行号 / 根因 / 修复动作 / 违反的任务要求）与修复后的完整 dofile
- **确定性守卫**：LLM 的 dofile 必须通过「无幻觉命令 + 输入路径绝对化 + 基础命令齐备 + task_spec 静态断言」才被采用，否则回退 `spec_fixer`
- **规格驱动修复**：断言只取 task_spec 中明确写出的数值、端口名与命名格式，专治「日志不报 ERROR 的配置类故障」
- **网表级修复 + 等价性校验**：备份 → 修改 → `yosys` LEC → 通过采用 / 失败回滚；形态库未覆盖时可让 LLM 在代码扫出的候选点里选择，不生成
- **证据链闭合**：issue 的行号与原文片段必须能在 `original.dofile` 中定位，定位失败留空，不编造

### 手册检索（RAG）

把手册 PDF 按编号章节切块，建 IDF 加权的关键词倒排索引，中文走 n-gram 并按文档频率自动剪枝。

三处自动注入 LLM 上下文：生成 dofile 时按需求关键词、诊断 DRC 时按规则号（DFTR1、DFTR10）、修复时按错误码（CMD-0074、SCAN-1009）。检索片段带出处，例如 `【手册 · 2.1.5.4 输出 SCANDEF 文件 (p64)】`。

手册缺失或无文字层时自动降级为空，不影响主流程。

### 预算与时限

赛题规定超时或超工具调用次数即当前 case 0 分，因此时限是流程的一等公民：

- 内部截止 = wall time × 0.90，留给产物整理；解析不到预算时按保守默认值兜底而不是「无上限」
- 预运行窗口 = min(预算 × 0.92, 上限, 预算 × 0.90 − 工具保留量)，工具保留量按网表体积分档（15 / 60 / 90 s）
- 每轮结束增量落盘 + 交付断言，异常路径也保证有可评分产物
- `.case_complete_marker` 放在 `finally` 中无条件写出：不写会被判超时，写早了要能被交付断言发现缺件

## 架构

```
入口层    agent_system        参数校验、目录挂载、启动主程序
编排层    scan_agent          任务分流、预算计时、多轮循环、落盘、完成标志
决策层    dofile_generator    网表事实 → LLM 决策 → TCL 渲染，规则模板兜底
          task2_diagnoser     任务二 LLM 诊断 + 修复
          spec_fixer          依 task_spec 原文的确定性修复器
能力层    netlist_facts       网表与 Liberty 事实抽取
          manual_rag          手册章节索引与检索
          spec_verifier       task_spec 断言抽取与 LLM 复核
          lec_verifier        网表 ECO 与等价性校验
          llm_eco             候选点选择式 ECO 兜底
工具层    tool_runner         dftexp_scan / yosys / eqy / btormc 调用与日志
          report_parser       日志、DRC 违例、报告解析
交付层    output_manager      runs/、final_results/、diffs/ 组织
          decision_logger     decision_log.json
```

决策层与工具层之间不共享全局状态：所有工具调用都走 `tool_runner.run_dofile`，所有判断都来自 `report_parser` 的结构化输出，便于在无 EDA 工具的环境下用桩替换回放日志。

## 目录

```
submission/
  agent_system         入口脚本（容器内 /submission 下启动）
  scan_agent.py        主流程：预算控制、多轮循环、交付
  dofile_generator.py  dofile 生成
  dofile_fixer.py      按错误行的局部补丁修复
  task2_diagnoser.py   任务二诊断 + 修复
  spec_fixer.py        规格驱动确定性修复
  spec_verifier.py     task_spec 断言与复核
  netlist_facts.py     网表事实抽取
  netlist_analyzer.py  网表结构统计
  lec_verifier.py      LEC 与网表 ECO
  llm_eco.py           ECO 候选点选择
  manual_rag.py        手册 RAG
  candidate_selector.py 候选方案评分
  input_parser.py      输入扫描与任务理解
  tool_runner.py       工具调用
  report_parser.py     日志与报告解析
  output_manager.py    输出目录组织
  decision_logger.py   决策记录
  llm_client.py        LLM 客户端（超时、截止时间、流式守护）
  config.py            配置与环境变量
  manual/              手册 PDF、命令参考、索引缓存
```

## 输入输出

### 输入

```
/input
  task_spec.md            任务说明书
  limitations.md          本 case 的 wall time 与工具调用次数上限
  netlist/*.v             门级网表（pre-scan）
  lib/*.lib               Liberty
  *.ctl                   可选，测试协议
  original.dofile         仅任务二：被注入故障的脚本
  case_upload_marker      就绪哨兵，出现后才开始计预算
```

### 输出

```
<output>/
  decision_log.json
  runs/R1..R3/
    R{n}.log
    deliverables/R{n}.dofile
    output/               工具导出的网表 / CTL / SCANDEF
    reports/              DRC 与各类报告
  final_results/
    final.log
    deliverables/         final.dofile、post_scan.v、pre_scan_final.v
    reports/              drc.rpt、lec_report.rpt 等
  diffs/                  dofile_orig_to_R1.diff、dofile_R1_to_R2.diff …
  .case_complete_marker
```

`decision_log.json` 的字段按赛题 schema 分任务对齐：

| 字段 | 内容 |
| --- | --- |
| `case_id` / `task` / `final_run` / `summary` | 基本标识与结论摘要 |
| `requirement_mapping` | 任务一：需求项 → 实际命令 → `final.dofile` 真实行号 `locator` |
| `issue_resolutions` | 问题现象、证据出处、修复动作、是否闭环 |
| `tool_runs` | 每轮工具调用、日志路径、退出状态 |
| `file_changes` | 改动路径与 diff；任务二额外带 `type` 与 `lec_ref` |

原则是**不伪造**：工具没有导出的文件不补、改名不合法产物，缺件由交付断言报出并记为 incomplete。

## 配置

LLM 凭据一律由环境变量注入，镜像内不写死 key：

| 变量 | 说明 |
| --- | --- |
| `LLM_API_KEY` | 必填，未注入则全程走确定性规则路径并在启动横幅明确告警 |
| `LLM_BASE_URL` | 网关根地址，内部自动补 `/v1`（幂等） |
| `LLM_MODEL` | 模型名 |
| `LLM_ENABLE_THINKING` | 默认 `0`，关闭思考模式；置 `1` 回到旧行为 |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | SDK 级超时与重试，默认 240 s / 0 次 |
| `SCANINSERTION_LICENSE_SERVER` | 缺少时 `dftexp_scan` 的 DFT 命令会全部报 SCAN-0620，启动时提前告警 |
| `DFTEXP_SCAN` / `YOSYS` / `EQY` / `BTORMC` | 工具路径 |
| `MAX_RUNS` | 最大工具轮数，默认 3 |
| `TOOL_TIMEOUT` | 单轮工具超时，另受 900 s 硬上限约束 |

预算类参数（`SCAN_AGENT_PRE_RUN_FRACTION`、`SCAN_AGENT_TOOL_RESERVE_S`、`SCAN_AGENT_ABSOLUTE_CAP_S` 等）均有实测标定的默认值，仅在复现实验时覆盖。

## 构建与运行

```bash
cd submission
pip3 install -r requirements.txt

export SCANINSERTION_LICENSE_SERVER=8273@<license-server>
export LLM_API_KEY=<key>

# 容器内（入口脚本会切到 /submission）
./agent_system -input /input -output /output

# 本地裸机调试（input 目录需就绪哨兵，本地可关掉等待）
SCAN_AGENT_WAIT_READY=0 python3 -m scan_agent \
    --input ./case/task_1/case1 --output ./out
```

依赖 Python 3、`openai`、`pypdf`；外部工具为 `dftexp_scan`（广立微 Scan 引擎）与 oss-cad-suite 中的 `yosys` / `eqy` / `btormc`。

## 关键取舍

以下数值都来自平台实测，不是估计值：

| 项 | 结论 | 依据 |
| --- | --- | --- |
| 思考模式 | 默认关闭 | 同一 prompt 开思考 260 s、reasoning 16451 tokens；关闭后降到秒级 |
| 预运行窗口 | 120 s（150 s 预算） | 单次生成调用实测 48 / 80 / 107 s，平台侧 78~148 s |
| 工具保留量 | 按网表体积 15 / 60 / 90 s | 0.33 MB→2.3 s，232 MB→334 s，259 MB→718 s |
| 最大轮数 | 6 → 3 | 90% 的收敛发生在前两轮，小网表 40~60 s/轮，大网表 300~400 s/轮 |
| 参考答案 | 不读 `golden.dofile` / `preset_issues.json` | hidden case 不提供，依赖它等于评测时失效 |
| 网表改动 | 必须过 LEC，否则回滚 | 破坏功能等价性的代价高于放弃一次修复 |

## 评测记录

平台第 20 次测试：12 / 12 case 通过，总运行时长 1114.9 s（目标 ≤ 1200 s）。

## 相关仓库

- [can-node](https://github.com/Dik-910/can-node) —— 车载 CAN 节点固件，STM32 上的 CAN 收发、UDS 诊断与双区 Bootloader
- [smartgateway](https://github.com/Dik-910/smartgateway) —— Linux 网关端
