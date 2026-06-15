"""设备任务路由"""
import os
import glob
import importlib

from device_worker.tasks.external_script_task import ExternalScriptTask

TASK_CLASS_ROUTER = {
    3: ExternalScriptTask,
}


def scan_device_handlers():
    tasks_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
    for filepath in glob.glob(os.path.join(tasks_dir, "*_task.py")):
        module_name = f"device_worker.tasks.{os.path.basename(filepath)[:-3]}"
        try:
            mod = importlib.import_module(module_name)
            meta = getattr(mod, 'HANDLER_META', None)
            if meta and meta.get('task_type') and meta['task_type'] not in TASK_CLASS_ROUTER:
                task_classes = [name for name in dir(mod) if name.endswith('Task') and name != 'BaseDeviceTask']
                if task_classes:
                    TASK_CLASS_ROUTER[meta['task_type']] = getattr(mod, task_classes[0])
        except Exception:
            pass


scan_device_handlers()


def register_handlers_to_db(db):
    tasks_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks")
    for filepath in glob.glob(os.path.join(tasks_dir, "*_task.py")):
        module_name = f"device_worker.tasks.{os.path.basename(filepath)[:-3]}"
        try:
            mod = importlib.import_module(module_name)
            meta = getattr(mod, 'HANDLER_META', None)
            if meta and meta.get('key'):
                doc = {"key": meta["key"], "label": meta.get("label", ""), "description": meta.get("description", ""), "inputs": meta.get("inputs", []), "task_type": meta.get("task_type", 0)}
                db["device_handlers"].update_one({"key": meta["key"]}, {"$set": doc}, upsert=True)
        except Exception:
            pass
