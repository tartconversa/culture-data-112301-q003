# 多语种游戏版本的缺陷外推与上架签发

用于本地业务开发的合成经营资料，所有参与方均使用虚构标识。应用把三类工作连成一条
可审计的链路：**抽检证据的风险外推 → 下一轮审核排程与任务生命周期 → 渠道内容集合的
签发、回执与撤回查询**。

## 目录

```
releaseqa/          应用包（仅使用 Python 标准库）
  model.py          资料装载、版本指纹与证据适用性
  defects.py        缺陷台账、内容缺陷传播、失效证据与复查关闭
  estimation.py     概率抽检的 Horvitz-Thompson 暴露估计与自助法置信区间
  planning.py       随机补样/定向核验计划、资质工时约束、任务失效说明
  release.py        渠道闸门、可发布子集、回执事实与撤回影响面
  store.py          追加式本地状态（审核、任务、签发、回执）
  cli.py            命令行入口
tools/
  check_materials.py  资料指纹/字段/外键检查（只读，不作业务计算）
  releaseqa_app.py    应用入口脚本
tests/              unittest 测试，含一次共享素材变化的端到端连锁测试
data/               合成资料（UTF-8 JSONL + catalog.json，只读）
docs/
  资料契约.md         资料口径与字段
  应用说明.md         业务规则、命令用法与状态文件说明
state/              应用本地状态（Git 已忽略，自动创建）
outputs/            导出资料包目录（Git 已忽略）
```

## 快速开始

Python 需要 3.11 至 3.14。应用本身只用标准库；`requirements.txt` 固定的是资料分析
环境的可选依赖，运行应用无需安装。

```sh
# 资料自检（指纹、字段类型、直接外键）
python3 -I tools/check_materials.py

# 运行全部测试
python3 -I -m unittest discover -s tests -v

# 分析德语 v2 各风险层的缺陷暴露与不确定性
python3 -I tools/releaseqa_app.py analyze --language de --version 2 --channel pc

# 生成并提交下一周期审核计划
python3 -I tools/releaseqa_app.py plan --language de --version 2 --channel pc --commit --on 2026-10-01

# 渠道闸门评估
python3 -I tools/releaseqa_app.py gate --content-set V2-de-pc
```

全局参数 `--root`（项目根，默认本仓库）与 `--state`（状态目录，默认 `./state`）
必须放在子命令之前。建议始终以 `python3 -I` 运行，避免从数据目录加载同名模块。

## 核心口径（摘要）

* 只有带纳入概率的 `random` 抽检可用于整层发生率外推；`complaint` 与 `recheck`
  是定向来源，单独列示，**投诉量不能冒充发生率**。
* 复查合格不抹掉原抽检记录的抽样身份；样本基数保留，缺陷状态由台账另行关闭。
* 历史证据仅在「内容指纹未变且适用条件一致」时可沿用；条目编号不变不构成继承。
  换版后产生的新审核针对新条件，不会被旧失效名单连坐。
* 内容缺陷沿同指纹条目传播到全部引用；截断、时序、上下文错误只属于对应引用。
* 渠道放行要求风险估计合格、阻断级缺陷关闭、必要风险层证据覆盖**同时成立**，
  不可用平均质量掩盖关键分支缺证。
* 签发事件追加后不可变；更正后的统计以新事件登记，过去的签发记录保持原样。
* 实际上架状态以渠道接收回执为准，签发本身只是内部决策，不等于已上架。

完整规则、命令清单与示例见 `docs/应用说明.md`，资料字段见 `docs/资料契约.md`。
