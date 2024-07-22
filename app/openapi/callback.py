#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/15
@Python：python 3.9
"""
import json

from flask import Blueprint, request

from models.task_m import *
from models.step_info import *
from models.step_list import *
from core.tools_core import *
import datetime

call_back_bp = Blueprint('callback', __name__)


@call_back_bp.route('/callback', methods=['POST'])
def create_task():
    data = request.get_json()
    try:
        # # 检查必填字段是否存在
        # if not data.get('task_name') or not data.get('task_type'):
        #     return make_response(message="task_name and task_type are required", status=400)

        # 创建 TaskM 实例
        task_id = TaskM.create_task(
            task_name=data['task_name'],
            task_type=data['task_type'],
            run_time=data['run_time'],
            status=data.get('status'),
            case_list=json.dumps(data.get('case_list', []))
        )
        return make_response(data={"task_id": task_id}, status=201)

    except Exception as e:
        return make_response(message=str(e), status=500)


# @index_page_bp.route('/get_step_log', methods=['POST'])
# def get_step_log():
#     data = request.get_json()
#     try:
#         # 检查必填字段是否存在
#         if not data.get('task_id'):
#             return make_response(message="task_name and task_type are required", status=400)
#
#         step_lists = StepList.get_steps_by_task_id (
#             task_id=data['task_id'],
#         )
#         # res_data = {}
#         res_list = []
#         for step_list in step_lists:
#             data_temp = {
#                 "step_name": step_list['step_name'],
#                 "step_id": str(step_list.id),
#                 "step_info": [],
#             }
#             step_infos = StepInfo.get_step_infos_by_step_id(
#                 step_id=str(step_list.id)
#             )
#             for step_info in step_infos:
#                 data_temp['step_info'].append({"step_message": step_info['step_message']})
#             res_list.append(data_temp)
#         res_data = res_list
#
#         return make_response(data=res_data, status=201)
#
#     except Exception as e:
#         return make_response(message=str(e), status=500)

