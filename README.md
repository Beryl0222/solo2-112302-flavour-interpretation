# 跨产区咖啡词义复核与评委对照样分配

面向“杯测短评、词义对照、分样单和复评席名录”的送复评流程引擎。项目仅依赖
Python 标准库，语料为本地合成演练文本；同一条流水线可用于下一场，只需换语料文件。

## 业务规则落点

| 规则 | 实现位置 |
| --- | --- |
| 否定属性、对照比较、主审引用均定位到原句、句内字符跨度与杯位 | `review_flow/notes.py`，断言对象 `models.Assertion` |
| 统一大类只用于检索，不能把两个具体义项上的意见直接抵消 | `review_flow/terminology.py`、`disputes.py` |
| 上下位与条件适用词（如 winey 仅洁净杯语境） | `models.TermSense.conditional`、`terminology.Glossary` |
| 同小批改盲号仍是同一来源；两次转记的相同意见合并，不构成独立分歧 | `review_flow/provenance.py`、`disputes.py` |
| 同来源留样不得当跨产区对照；未登记杯位转疑点 | `provenance.ProvenanceRegistry.eligible_controls`、`disputes.py` |
| 缺区分证据的词先留疑点，保留原语言片段供秘书核查 | `disputes.Doubt.raw_fragment` |
| 重新指定词义产生新版本，复评目的、评委资质、对照杯联动重算 | `Glossary.redesignate`、`review.ReviewPlanner` |
| 见过同来源宣传稿的评委改派；无适格人或对照不足则退回 | `review.ReviewPlanner.plan` |
| 已送达锁定且仍合格的场次不换；复核说明禁止暴露新盲号对应 | `sheets.deliver`、`instruction_safe_for_frozen_delivery` |
| 盲审单只含盲号与脱敏材料；产区负责人凭封存密钥回收结论与来源证据 | `sheets.SheetBuilder`、`SealedKey`、`assert_no_identity_leak` |
| 不同冲注是不同判断，不得借修订记录重复登记分封 | `notes.detect_pour_seq`、`pipeline.apply_revision_records` |

## 目录

- `review_flow/`：领域引擎（术语表、来源分封、短评解析、分歧认定、派单、盲审单、流水线）
- `data/corpus.json`：第一场合成语料（酒香/发酵缺陷之争、改号杯 H17/X52、宣传稿接触人）
- `data/session_two.json`：第二场合成语料（改号转记重复、同来源留样 S88、缺证词 muddled finish）
- `materials.py`：原始语料读取入口（保持不变）
- `run_review.py`：命令行入口
- `tests/`：34 个 unittest 用例，覆盖每条规则与两场端到端流程

## 运行

```bash
# 第一场 / 第二场
python3 run_review.py data/corpus.json
python3 run_review.py data/session_two.json

# 重新指定词义：把裸“发酵”指到缺陷义项，目的与派单按 v2 重算
python3 run_review.py data/session_two.json --redesignate 发酵 ferment_defect v2

# 测试
python3 -m unittest discover -s tests -v
```

读取外部目录中的语料时，把路径作为参数传入并用隔离模式运行，解释器不会从
语料目录加载模块：

```bash
python3 -I run_review.py /path/to/外部语料.json
```

## 输出物

- **复评单 `ReviewSheet`**：盲号、固定角色（待复评/对照杯）、脱敏原句与开放疑点；
  行序洗牌，不含批号、产区、评委姓名。
- **封存密钥 `SealedKey`**：按产区隔离，原产区负责人可从盲号收回批号、产区与
  分封/分样证据记录编号；盲审单本身无法反推同案其他杯身份。
- **送达记录 `DeliveryRecord`**：送达即冻结盲号集合，后续说明出现未冻结新盲号
  会直接抛出 `PermissionError`。
- **疑点 `Doubt`**：未登记词、裸用大类词、来源不明对照杯一律先留疑点再复评。
