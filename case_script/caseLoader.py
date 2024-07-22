import importlib
import os
import logging

logger = logging.getLogger(__name__)

class CaseManager:
    def __init__(self, case_dir):
        self.case_dir = case_dir
        self.cases = self.load_cases()

    def load_cases(self):
        cases = {}
        for filename in os.listdir(self.case_dir):
            if filename.startswith("case_") and filename.endswith(".py"):
                module_name = filename[:-3]
                module_path = f"case_script.{module_name}"
                try:
                    module = importlib.import_module(module_path)
                    for attr in dir(module):
                        if attr.startswith('Case') and hasattr(module, attr):
                            case_class = getattr(module, attr)
                            if isinstance(case_class, type) and issubclass(case_class, object):
                                cases[attr] = case_class
                                logger.info(f"Loaded case class: {attr}")
                except Exception as e:
                    logger.error(f"Failed to import module {module_path}: {e}")
        return cases

    def get_case_instance(self, case_key):
        case_class = self.cases.get(case_key)
        if case_class:
            return case_class()
        return None

    def invoke_case_method(self, method_key, **keyword_args):
        for case_class in self.cases.values():
            case_instance = case_class()
            if hasattr(case_instance, method_key):
                method = getattr(case_instance, method_key)
                if callable(method):
                    return method, case_instance
        return None, None