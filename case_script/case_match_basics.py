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
from models.callback import *


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

        send_data = {
        "token": "bc64045d65cae0c54ba9532129302cd6",
        "app_version": "3.11.0",
        "match_options": {
            "preferred_region": "",
            "gender": ""
        }
        }
        headers = {
            'Content-Type': 'application/json'
        }

        print(f"Running case_run_match_1to1 in CaseExample: {kwargs} ")
        step_id = StepList.create_step(str(kwargs['task_id']), kwargs['case_info']['case_key'])
        time.sleep(1)
        self.step_id = step_id
        self.build_step('初始化被匹配用户')

        #临时设置uri
        holla_host_str = 'http://testv2.holla.world/'
        user_verify_list = []

        # 获取当前时间
        since_time = datetime.datetime.now()

        user_passivity_list = kwargs['case_info']['user_passivity']
        for user_passivity_info in user_passivity_list:
            # 检测渠道
            if user_passivity_info.get('user_channel') == 'holla':
                holla_host_url = holla_host_str + 'api/MatchRequest/cancel'
            # 检测是否自定义json
            if len(user_passivity_info.get('case_json')) > 0:
                send_data = user_passivity_info.get('case_json')
            # 替换token
            send_data['token'] = user_passivity_info.get('user_token')
            r = send_request(holla_host_url, send_data, headers)
            if r.status_code == 200:  # 此处临时验证
                self.build_step('初始化匹配用户初始化成功' + str(user_passivity_info.get('user_token')))
            else:
                self.build_step('初始化匹配用户初始化失败' + str(r.status_code) + str(r.text))

            if user_passivity_info.get('user_verify') == 'true':
                user_verify_list.append({'uid': '57389721'})  # mock

            # 进行匹配
            holla_host_url = holla_host_str + 'api/MatchRequest/Online/create'
            r = send_request(holla_host_url, send_data, headers)
            if r.status_code == 200:  # 此处临时验证
                self.build_step('初始化匹配用户初始化成功' + str(user_passivity_info.get('user_token')))
            else:
                self.build_step('初始化匹配用户初始化失败' + str(r.status_code) + str(r.text))

        self.build_step('等待5秒 入被选池')
        time.sleep(5)

        user_main_info_list = kwargs['case_info']['user_main']
        for user_main_info in user_main_info_list:
            # 检测渠道
            if user_main_info.get('user_channel') == 'holla':
                holla_host_url = holla_host_str + 'api/MatchRequest/cancel'
            # 检测是否自定义json
            if len(user_main_info.get('case_json')) > 0:
                send_data = user_main_info.get('case_json')
            # 替换token
            send_data['token'] = user_main_info.get('user_token')
            r = send_request(holla_host_url, send_data, headers)
            if r.status_code == 200:  # 此处临时验证
                self.build_step('初始化主匹配用户初始化成功' + str(user_main_info.get('user_token')))
            else:
                self.build_step('初始化主匹配用户初始化失败' + str(r.status_code))

            # 如果是重要用户就保存uid
            if user_main_info.get('user_verify') == 'true':
                user_verify_list.append({'uid': '5740564'})  # mock

            # 进行匹配
            holla_host_url = holla_host_str + 'api/MatchRequest/Online/create'
            r = send_request(holla_host_url, send_data, headers)
            if r.status_code == 200:  # 此处临时验证
                self.build_step('初始化主匹配用户 匹配成功' + str(user_main_info.get('user_token')))
            else:
                self.build_step('初始化匹配用户 匹配失败' + str(r.status_code) + str(r.text))

        self.build_step('验证结果')
        time.sleep(3)
        for user_verify in user_verify_list:
            c_info = Callback.get_callbacks_by_union_uid(user_verify['uid'], since_time)
            if len(c_info) == 0:
                self.build_step('回调失败' + str(user_verify.get('uid')))
                time.sleep(1)
            else:
                self.build_step('回调成功' + str(user_verify.get('uid')))
                time.sleep(1)


