import json
import threading
import time
from models.task_m import TaskM
from case_script.caseLoader import CaseManager
import logging
from app import create_app
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 创建 Flask 应用
app = create_app()

def process_task(task):
    # 处理单个任务的逻辑
    print(f"Starting {task.id}")

    case_dir = './case_script'
    # method_key = input("Enter the method key (e.g., 'case_run_example'): ")
    # kwargs_json = input("Enter arguments as a JSON object (e.g., {\"param1\": 1, \"param2\": 2}): ")
    #
    # try:
    #     kwargs = json.loads(kwargs_json)
    # except json.JSONDecodeError as e:
    #     logger.error(f"Failed to parse arguments: {e}")
    #     return
    # Instantiate CaseManager
    case_manager = CaseManager(case_dir)
    for case in json.loads(task['case_list']):
        data = {
            "task_id": task.id,
            "task_name": task.task_name,
            "task_type": task.task_type,
            "status": task.status,
            # "run_case_key": case['case_key'],
            "case_info": case,
        }
        method, case_instance = case_manager.invoke_case_method(case['case_key'], **data)
        if method:
            try:
                # logger.info(f"Invoking method: {method_key} with kwargs: {kwargs}")
                method(**data)  # Unpack the kwargs dictionary into the method call
            except Exception as e:
                logger.error(f"Error during method execution: {e}")
    TaskM.mark_task_completed(task.id)


def main():
    while True:
        # 获取待处理任务
        pending_tasks = TaskM.get_pending_tasks()

        if not pending_tasks:
            print('No pending tasks.')
        else:
            # 为每个任务创建一个线程来处理
            for task in pending_tasks:
                thread = threading.Thread(target=process_task, args=(task,))
                thread.start()
                # 可以选择是否等待线程完成（通常在实际应用中会需要）
                thread.join()

        # 短暂休眠，避免CPU占用过高
        time.sleep(2)


if __name__ == "__main__":
    main()