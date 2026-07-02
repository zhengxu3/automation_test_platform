"""AI 任务路由注册表"""
from ai_worker.tasks.requirement_analysis_task import RequirementAnalysisTask
from ai_worker.tasks.branch_review_task import BranchReviewTask
from ai_worker.tasks.script_gen_task import ScriptGenTask
from ai_worker.tasks.goal_capability_task import GoalCapabilityTask
from ai_worker.tasks.repo_vectorize_task import RepoVectorizeTask

# task_type → TaskClass
TASK_CLASS_ROUTER = {
    2: BranchReviewTask,          # 代码分析
    10: RepoVectorizeTask,        # 仓库向量化
    20: RequirementAnalysisTask,  # 需求分析 / 通用智能体执行
    30: ScriptGenTask,            # 脚本生成（UI 自动化）
    40: GoalCapabilityTask,       # Goal step 统一能力执行器
}
