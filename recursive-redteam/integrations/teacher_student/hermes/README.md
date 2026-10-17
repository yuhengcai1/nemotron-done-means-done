# Hermes：未知提交状态与恢复规则教学单元

**供主线程整合，本文和 evidence/ 为宿主材料，不作为学生提示。**
学生只接收 `suite.describe(split)`、`suite.lesson()` 和自身 probe 的结果。
不应将 suite 源码、教师探针、测试源码、验证报告或 holdout 实现映射送入学生上下文。

入口：`integrations.teacher_student.hermes.suite:build_suite()`。
实现了统一协议 `describe / lesson / teacher_probes / evaluate / control / novelty`，
没有改核心 runtime、主 README、既有 Hermes 集成或其他方向目录。

## 已冻结的真实问题

`evidence/frozen-native.json` 固定一个历史原生问题：非幂等创建已提交但返回
`commit_status=unknown`，携带旧恢复记忆的原生 Hermes 再次创建，产生两个副作用。
含两条历史失败／复现记录、两条匹配空记忆成功对照和两条当前正确记忆成功对照。

不变量是单个逻辑任务至多产生一个已提交副作用；完成还要求恰好一个完成的导出以及
最后创建后的状态核验。两条失败均有独立模型调用与服务提交，原始调用顺序被冻结。
包内保存请求模型、原生 runtime commit、源码哈希、各原始文件哈希和离线重放结果。

该证据属于 **historical native observation**：原生 Hermes + Crusoe/Nemotron 推理在之前发生，
当前做的是模型消息到独立服务账本的离线重放和哈希核验。
模型权重哈希、实际 served checkpoint 没有提供，不能由请求模型别名推断。
本轮核验了 40 个文件引用，未发起原生运行或新的 API 调用。

## 教学与探针

`lesson.json` 只教通用前提、探索步骤、oracle 和非案例：区分请求与副作用；检查记忆的
适用条件；确认查询结果何时进入决策及是否足够新；区分同一逻辑操作与新的操作身份。
它不含 holdout 目标 ID、私有策略名称、预期结果或现成探针答案。

学生提交受校验的 JSON：选择公开的 target handle、故障窗口、真实的回执语义、
读可见性和可选模拟传输故障。学生不能直接指定任意工具动作、源码、路径、密钥、
模型设置或 oracle。每次 evaluate 都是 fresh 本地状态，最多 8 次服务操作。

当前被测对象是明确标注的**确定性本地策略程序**，用于让学生选择实验并发现规则边界。
它们不代表新发生的原生 Hermes 或模型行为。
训练集提供简单的旧恢复规则与条件式恢复；holdout 改变决策内结果交付时序、查询新鲜度、
以及幂等性约束的操作身份，包含正确恢复负对照，不是仅给训练案例改名。
`novelty()` 在宿主侧检查这些实质条件是否被触发。

`evaluate()` 返回 `status`、稳定 `signature`、原始回执、提交的 effect IDs、事件序号和
决策前已收到的响应集合。判定来自实际效果，不来自私有策略标签或最后复盘文字。
重复请求但同键只提交一次是负例；无异常且任务完成才是 passed；传输／协议／未完成是 error。
同批查询与重试不被当作先看查询结果后再决定。

`control()` 保留策略、读契约和其他参数，只去掉服务超时故障。
主 runner 负责模型预算、生成学生探针、独立复现、负对照、来源身份及长期 ledger。
不得把“学生说出了正确原则”当作发现，也不得把一个 passed probe 当作目标普遍安全。

## 本轮验证

- 16 个测试通过，覆盖历史证据、接口、探针校验、时序、查询新鲜度、幂等负例、novelty、
  oracle 不依赖目标标签、错误不算发现及 lesson 不泄漏 holdout 映射。
- 教师训练探针：一个本地策略反例，初始执行后两次 fresh 复现，匹配控制通过。
- 离线脚本探针：3 个 holdout 策略反例、5 个条件内的负观察、1 个 inconclusive。
  这些是接口／宿主校验测试，**不是学生 Nemotron 的真实发现或学习效果**。
- 主线程已反馈独立通过训练探针的两次复现和负对照；本目录不替主线程生成 live 结果。

证据保存于 `evidence/local-validation.json`。教师角色为 Codex 编写和选择本地探针，
当前精确 checkpoint 不可获取；没有另行调用 Astra API。学生 live 尚未运行于本目录。

从仓库根目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_teaching_hermes -v
PYTHONDONTWRITEBYTECODE=1 python3 -m integrations.teacher_student.hermes.freeze verify \
  --packet integrations/teacher_student/hermes/evidence/frozen-native.json
PYTHONDONTWRITEBYTECODE=1 python3 -m integrations.teacher_student.hermes.local_validation \
  --out integrations/teacher_student/hermes/evidence/local-validation-new.json
```

验证产物用排他创建，不覆盖冻结证据。可选原生复现命令在冻结包内，标为本轮未执行；
新的原生或学生推理请求统一由主线程安排。

## 交付边界

`STATUS.json` 为主线程读取的就绪状态和源码哈希。置为 `ready_for_live=true` 后停止源码修改，
避免 live 运行中途身份变化。后续实测应分别报告：教师历史／本地证据、学生自己选择的
新条件、宿主复现与控制、未成功和 inconclusive 结果。只有完成这些，才讨论学生迁移表现。
