# 复检班次差额账

现有 `README.md` 说明了原测接纳、冻结复检批、独占占用、签发撤办、按号领取和实时核账。实时核账反映调用当时可确定的状态。班次账用于结班后按原班次核对期初、实际发生的变动和期末。

## 外部入口

四个新函数均从 `audit.shift` 导入，沿用 `archive.initialize.initialize(directory)` 返回的 `Stores`。批号和班次号沿用 V1 的非空 UTF-8 标签规则，非法标签抛出 `ValueError`。下列结果均为 Python 字典。

- `open_shift(stores, batch_id, shift_id)` 为一个已冻结批固定期初，返回 `status='open'`、`batch_id` 和 `shift_id`。同批可交叠多班。同号同批复投保留原期初，已结班则返回相同身份及 `status='closed'`。同号异批返回 `shift_conflict`。新班引用不存在的冻结批返回 `unknown_batch`。
- `close_shift(stores, shift_id)` 仅凭已登记班次及台账固定期末，返回 `status='closed'`、`batch_id` 和 `shift_id`。归档文件无法打开时仍可结班。重复调用返回原结班结果。未知班次返回 `unknown_shift`。
- `seal_shift(stores, shift_id)` 为已结班次出单，首次成功和此后成功复投均返回下述 `sealed` 结果。未知班次返回 `unknown_shift`，尚未结班返回 `not_closed`。
- `read_shift(stores, shift_id)` 返回已保存的 `sealed` 结果。已登记但未出单返回 `not_sealed`，未知班次返回 `unknown_shift`。

所需存储暂不可用或本次提交未成功时可返回 `pending`。未成功的首次出单无可领取的账或首执，可按原班次号续办。非 `sealed` 结果不交付账正文或首执。业务失败可只返回 `status`。

## 变动及出单口径

开班时已经取得的占用计入期初。仅收期开之后、结班之前实际提交的占用、正式核销和撤办退回，班次边界与台账变动按同一提交顺序裁决。一份整单在一次台账提交中发生的一种变动只列一笔。成品备料未核销时仍为占用。失效尝试、回滚、重复领取及没有释放凭据的撤办均不增加变动。升级前已存在的占用和核销按当前事实进入新班期初。

以冻结批全部原测修订的 `RevisionRef.key` 匹配证据，邻批发起的变动同样计算。逐笔只保留本批命中的格及证据，保持请求的原格顺序。无命中的整笔省略。区间内占用后退回的两笔均保留。分类余额沿用 `audit` 的六个计数键及口径。某修订虽然在本批不合格，若已被邻批占用或核销，仍计入 `held` 或 `used`。成功出单时边界已确认，`unconfirmed` 为零。

`sealed` 结果包含 `status='sealed'`、`shift_id`、`batch_id`、`opening_balance`、`closing_balance`、`events` 和 `receipt`。余额字典包含 `used`、`available`、`held`、`missing`、`ineligible`、`unconfirmed`。`events` 为实际提交顺序的列表，每项如下：

```python
{
    'kind': 'issue',
    'request_id': 'Y',
    'report_id': 'R-Y',
    'lines': [{'slot': 'U', 'evidence_id': '["P1","B",1]'}],
}
```

三种 `kind` 分别为 `hold`、`issue`、`release`。只有 `issue` 的 `report_id` 是原报告号，其余为 `None`。无变动时 `events` 为 `[]`。`receipt` 是含 `receipt_id`、`shift_id`、`batch_id` 的字典，其中 `receipt_id` 为首次出单时固定的非空标签。账和首执共同提交，签发后的复读使用该次保存的结果。晚到业务、原请求重投和重开不得改变已出班次账。原签发、撤办、历史报告、实时核账和旧库继续可用。

## 手算夹具和运行

`shift_cases.py` 的 `load_case(stores)` 使用原入口创建两个冻结批，返回 `(refs, x, y)`。`refs` 按 A、B、M、D、C 查原修订。`x` 用邻批 `neighbor` 的原格序 N2←D、N0←C、N1←A，`y` 用本批 `day` 的 U←B。A、B 在本批合格，M 缺测，D 在本批不合格，C 不在本批。沿用 `archive.publish` 的 `AFTER_PREPARE` 把签发停在备料后，沿用 `BEFORE_COMMIT` 验证核销回滚。

1. 给 `day` 开 S。备料 X 后再开同批 T。撤办 X，然后备料 Y。此时结 S。随后完成 Y 的正式核销，再结 T。S 依次有 X 占用、X 退回、Y 占用。T 依次有 X 退回、Y 占用、Y 核销。X 的每笔保留 N2←D、N1←A，Y 核销引用 R-Y。交换 X 退回和 Y 占用的提交次序，两班正文中这两笔的顺序也交换。
2. S 期初可用 2、缺测 1、不合格 1，期末可用 1、占用 1、缺测 1、不合格 1。T 期初可用 1、占用 2、缺测 1，期末已用 1、可用 1、缺测 1、不合格 1。未列类别均为零。
3. 结 S 时可用相同的 `ledger` 路径和无法打开的 `archive` 路径调用 `close_shift`。恢复归档后可另开归档写事务阻断首次出单，解除阻断后查不到新账。完成 Y 的晚到核销后重投结 S 和签发 S，期末仍有一笔占用。重开后按号取得相同账与首执。连续开结一班而不变动时也可出空账。

`python3 -B shift_cases.py` 运行 V1 夹具，显示当前库在上述业务时点的四组余额。沿用根目录 `.pointwise-runtime.json` 接入交付核验和演示。清单维持 `version` 为 1、`kind` 为 `cli`，`setup` 是准备命令的参数数组列表，`verify` 和 `demo` 各为一条命令的参数数组。`verify` 要执行原 `check_project.py` 覆盖的全部回归以及班次联动核验，失败时以非零状态退出。`demo` 展示归档不可用时结班、恢复后取得原账及重开复领的实际结果。
