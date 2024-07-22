#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/22
@Python：python 3.9
"""
import time

from core.tools_core import send_request
from models.step_list import *
from models.step_info import *


class CaseMatchBasics(object):

    def __init__(self):
        # Initialize case-specific resources or state here
        self.step = 1
        self.step_id = 0
        print(f"Initialized CaseAnotherExample with step {self.step}")

    def build_step(self, message):
        StepInfo.create_step_info(str(self.step_id), '步骤 ' + str(self.step) + ' : ' + message)
        self.step += 1


    def case_run_another_example(self, args):
        # Assuming args is a list or dictionary
        print(f"Running case_run_another_example in CaseAnotherExample with args: {args}")
        # Example of logging a step
        self.step += 1
        print(f"Step updated to {self.step}")

    def another_method(self):
        print("Another method in CaseAnotherExample")

    def case_run_match_1to1(self, **kwargs):
        print(f"Running case_run_match_1to1 in CaseExample: {kwargs} ")
        step_id = StepList.create_step(str(kwargs['task_id']), kwargs['case_info']['case_key'])
        time.sleep(1)
        self.step_id = step_id
        self.build_step('初始化被匹配用户')
        time.sleep(1)
        self.build_step('初始化匹配用户')
        time.sleep(1)
        self.build_step('等待回调')
        time.sleep(1)
        self.build_step('验证结果')
        time.sleep(1)

        uri = ''

