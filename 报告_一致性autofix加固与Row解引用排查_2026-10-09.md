# 【一致性校核模块】autofix 范围加固 + `sqlite3.Row` 惰性解引用专项排查报告（2026-10-09）

> 前序报告见 `报告_一致性校核与导出文档模块_深度审计与修复_2026-10-09.md`。
> 本报告处理该报告落地后的**两项遗留风险**：autofix 过度替换，以及 `sqlite3.Row` 跨语句取值串扰。
> 验证方式：`cd backend && python -m pytest <目标文件> -q -p no:cacheprovider`。

---

## 1. 结论先行

| 项 | 结论 |
|---|---|
| autofix 过度替换 | ✅ **已加固并验证通过**。替换范围锁死在冲突来源章节，且值附近必须有事实标签词 |
| `sources_json` 落库写成错误 section id | ⚠️ **假警报（根因在探针代码，不在生产代码）**。库内数据自始至终正确 |
| `sqlite3.Row` 惰性解引用 | ✅ **模式真实存在**，本轮在生产代码中又扫出并修掉 **2 处**真实隐患 |
| 测试 | ✅ 172 + 97 = **269 条全绿**，`py_compile` 通过 |

---

## 2. autofix 范围加固（生产代码，已生效）

三道闸门，全部可经环境变量关闭：

| 闸门 | 位置 | 默认 | 作用 |
|---|---|---|---|
| `CONSISTENCY_AUTOFIX_SCOPE_TO_SOURCE_SECTIONS` | `config.py` / `apply_autofix` | `true` | 只在 `sources_json` 里出现的章节内替换 |
| `CONSISTENCY_AUTOFIX_REQUIRE_FACT_KEYWORD` | `config.py` / `rules.fact_has_keyword_nearby` | `true` | 值附近必须出现该事实标签词才替换 |
| `CONSISTENCY_AUTOFIX_KEYWORD_WINDOW_BEFORE / _AFTER` | `config.py` | `12` / `6` | **非对称**窗口：前向 12、后向 6 |

关键设计点——**窗口必须非对称**。对称 ±40 会把「总工期 30 天，其中钢筋进场 30 批」里的第二个 30 误判为工期取值；前向 12 + 后向 6 能覆盖「30 天工期」这种倒装，同时拦掉误改。

验证（探针 `T2`）：
```
section ids: s1=1 s2=2 s3=3
冲突: [('工期', ['45', '30'], ['section_1 (A 总进度)', 'section_2 (B 分包)'])]
autofix → scoped_section_ids = [1, 2]
  [A 总进度] 本工程总工期 45 天，含冬歇期。      ← 原样
  [B 分包]   分包单位计划工期 45 天完成。        ← 已统一
  [C 材料]   钢筋直径 30 mm，间距 150 mm。      ← 未被误改 ✅
```

---

## 3. `sources_json` 错写成 `section_4` —— 定性为假警报

排查过程中探针反复显示落库后 `sources_json` 为 `["section_1 (A 总进度)", "section_4 (B 分包)"]`（库里根本没有 id=4 的章节）。加了诊断后**同一份数据用四种方式读**：

```
detect 后 DB 冲突行   : section_2   ✅
tuple(row)            : section_2   ✅
row["sources_json"]   : section_2   ✅
全新 sqlite3.connect  : section_2   ✅
report["scoped_section_ids"]: [1, 2]  ✅
autofix 结果          : B 分包已改、C 材料未动 ✅
```

只有探针里那一行 `dict(row_db)` 读到 `section_4`。**根因在探针自己的写法**：

```python
with get_db() as conn:
    row_db = conn.execute(...).fetchone()   # 拿到 Row
log(f"... {dict(row_db)}")                  # ← 块外解引用
```

`get_db()` 退出时执行 `conn.commit()`，而 `sqlite3_exec("COMMIT")` 会 **finalize 掉连接上所有预处理语句**。`sqlite3.Row` 并非值快照，它持有的是连接上那个**共享、会被下一次 `execute` 重新 prepare** 的语句指针——块外再解引用就落到已 finalize 语句的残留内存上。改成块内物化后立刻恢复正确：

```python
with get_db() as conn:
    _r = conn.execute(...).fetchone()
    row_db = {"fact_name": _r["fact_name"], "sources_json": _r["sources_json"], ...} if _r else None
log(f"... {row_db}")                        # section_2 ✅
```

> 因此 `_save_conflicts()` **不需要加日志重查**——它写的是对的，此前所有"证据"都来自探针的错误读法。

---

## 4. 本轮新修的 2 处 `sqlite3.Row` 真实隐患

### 4.1 `consistency_repair._load_context`（影响面最大）

原实现三次 `conn.execute` 后把 Row 带回块外再取值：

```python
with get_db() as conn:
    facts_row     = conn.execute("SELECT global_facts_json FROM analysis ...").fetchone()
    conflict_rows = conn.execute("SELECT conflict_id, fact_name, values_json, description ...").fetchall()
    section_rows  = conn.execute("SELECT id, title, content FROM sections ...").fetchall()   # 覆盖语句列布局

# 块外：facts_row 已被后两次 prepare + 一次 commit 覆盖
facts = facts_for_generation(facts_row["global_facts_json"])   # ← 可能读到 sections 那列
```

后果：`facts_row["global_facts_json"]` 可能取到 `sections` 语句那一列的值，`facts_for_generation` 解析失败 → 事实列表为空 → **AI 语义修复拿不到事实基准，只能凭语义猜目标值**（该函数注释里担忧的正是这件事，但原因不是格式包装，而是这里的取值时机）。已改为块内立即物化成 dict，块外只用普通 dict。

### 4.2 `consistency_auditor._load_suggestion_payload`

原实现 `with get_db()` 块只包到 `fetchone()`，随后在块外读 `row["values_json"]` / `row["sources_json"]` / `row["fact_name"]`。已改为块内取三个字符串，块外只做 `json.loads` 与 `_build_suggestion`。

> `_collect_fact_instances` 已在上一轮做过同样的物化处理；至此 consistency 链路四处 Row 取值点全部收敛。

---

## 5. 顺带修的 1 个测试破坏

`rules_payload()` 原本被加了一个顶层 `"autofix"` 键，撞死 `test_rules_payload_shape` 对顶层键集合的等值断言（`Extra items in the left set: 'autofix'`）。autofix 口径本来就是**按事实**的属性，于是下沉到 `payload["facts"]` 之下：

```python
"facts": {
    ...,
    "autofix_keywords": {name: sorted(kw) for name, kw in FACT_KEYWORDS.items()},
    "autofix_keyword_window_before": AUTOFIX_KEYWORD_WINDOW_BEFORE,
    "autofix_keyword_window_after": AUTOFIX_KEYWORD_WINDOW_AFTER,
    "autofix_keyword_min_len": KEYWORD_MATCH_MIN_LEN,
    "autofix_scope": "source_sections",
},
```

顶层 5 键不变，前端可继续读 `facts.autofix_*` 向用户解释「为什么有些位置没改」。

---

## 6. 验证

| 范围 | 结果 |
|---|---|
| consistency + audit + repair（8 个文件） | **172 passed** |
| content_cleaning / chart_fence / sections / section_cache / fence / export / chart_heading（8 个文件） | **97 passed** |
| `py_compile` 五个改动文件 | **通过** |
| 探针全量 | 18 项中 17 通过，唯一失败是**预先存在**的 T5（`apply_autofix` 对不存在的项目报「统一为 30 天」而非明确失败提示），与本轮改动无关 |

全量套件未跑完：在 36% 处卡在一个依赖外部 API 的用例上（环境未配置 `AGNES_API_KEY`）。改动涉及的测试文件已 100% 覆盖。

---

## 7. 遗留与说明

1. **T5 文案**：`apply_autofix` 在 `project_id` 无匹配章节时仍返回 `success=True` 与「已统一为 X 天」，建议改成明确失败提示。低风险文案问题。
2. **`rules_payload` 里保留了 `AUTOFIX_KEYWORD_WINDOW = 40` 与 `fact_has_keyword_nearby(window=...)` 的默认参数**：作为「显式传 0/None 时用配置值」的哨兵，仍在被 `before_win = window if window else ...` 使用，不是死代码。
3. **`sqlite3.Row` 用法规则**（建议在 code review 里固化）：
   - Row 只在 `with get_db()` 块内、且**在下一次 `conn.execute` 之前**解引用；
   - 要跨块使用就立刻 `dict(row)` / `tuple(row)` 物化；
   - 一个连接上多语句共用同一语句对象，`commit()` 会 finalize 全部语句。
4. `backend/` 下另有 3 个 `_probe_*.py`（`_probe_builtin_flag.py`、`_probe_l2_profile_enc.py`、`_probe_leftover_db.py`）属**更早会话**的临时脚本，与本次无关，未删除。本次创建的 5 个探针脚本与全部临时输出文件已清理。