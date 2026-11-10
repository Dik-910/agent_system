# 手册放置说明

把广立微 Scan Insertion 工具用户手册（PDF）放到本目录下。

## 放置方法

1. 将手册 PDF 文件复制到本目录（`scan_agent/submission/manual/`）。
2. 文件名不限，任意 `*.pdf` 均可，例如：
   ```
   scan_agent/submission/manual/Scan_Insertion_User_Guide.pdf
   ```
3. 可以放多个 PDF（如「命令参考」和「DRC 规则」分册），系统会自动合并建索引。

## 工作方式

- **构建/运行时自动解析**：程序首次运行时会把 PDF 转成纯文本，按「命令 / DRC 规则 / 错误码」切块并建立关键词倒排索引，缓存到 `.index_cache.json`。
- **检索时机**：在以下三处自动检索并注入 LLM 上下文：
  1. 生成 dofile 时 —— 根据 task_spec 里的需求关键词检索相关命令说明；
  2. 诊断 DRC 时 —— 根据违例规则号（如 DFTR1、DFTR10）检索规则说明与修复建议；
  3. 修复 dofile 时 —— 根据报错命令 / 错误码（如 CMD-0074、SCAN-1009）检索命令语法。
- **优雅降级**：如果手册 PDF 缺失或无法解析，RAG 自动返回空，不影响主流程（仅少一层手册参考，LLM 仍会凭自身知识 + golden.dofile 风格生成）。

## 注意事项

- 依赖：PDF 解析使用 `pypdf`（已在 requirements.txt 声明），纯 Python、无系统依赖。
- 若手册为扫描版（图片型 PDF），`pypdf` 无法提取文字，需用 OCR 版本或可复制文字的 PDF。
- 手册越大，首次建索引越慢（几十 MB 的 PDF 约数秒到数十秒），索引结果会缓存复用。
