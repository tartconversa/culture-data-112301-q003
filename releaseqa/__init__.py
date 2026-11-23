"""多语种版本缺陷外推、审核排程与上架签发应用。

模块划分：

* ``model``     读取合成资料与本地状态，计算版本指纹与证据适用性
* ``defects``   缺陷台账：开启、关闭、内容缺陷传播、失效证据
* ``estimation`` 仅基于概率抽检的 Horvitz-Thompson 暴露估计与自助法区间
* ``planning``  随机补样与定向核验计划、资质与工时约束、任务生命周期
* ``release``   渠道条款闸门、签发、回执事实与撤回查询
* ``store``     本地 JSONL 状态（追加写、不可改写历史）
* ``cli``       命令行入口
"""

SCHEMA_VERSION = "1.0"

SEVERITY_ORDER = {"none": 0, "minor": 1, "major": 2, "critical": 3}
PURPOSE_RANDOM = "random"
PURPOSE_COMPLAINT = "complaint"
PURPOSE_RECHECK = "recheck"
SCOPE_CONTENT = "content"
SCOPE_CONTEXT = "context"

# 单条非剧情组引用的默认审核工时（资料未给出逐条工时，仅组有整组工时）。
SOLO_AUDIT_HOURS = 1.0
# 风险层可外推所需的最少概率样本数。
MIN_EXTRAPOLABLE_SAMPLE = 2
# 自助法重复次数，固定种子保证结果可复现。
BOOTSTRAP_REPLICATES = 1000
