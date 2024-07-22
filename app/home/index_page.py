#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/15
@Python：python 3.9
"""

from flask import Blueprint, request

from models.task_m import *
from core.tools_core import *
import datetime

index_page_bp = Blueprint('index_page', __name__)


# @index_page_bp.route('/add', methods=['POST'])
# def add():
#     data = request.json
#     # 处理添加操作的逻辑
#     result = {'message': 'Added successfully', 'data': data}
#     return jsonify(result), 200
#
#
# @index_page_bp.route('/init', methods=['POST'])
# def init():
#     data = request.json
#     # 处理初始化操作的逻辑
#     result = {'message': 'Initialized successfully', 'data': data}
#     return jsonify(result), 200


@index_page_bp.route('/create_task', methods=['POST'])
def create_task():
    data = request.get_json()
    try:
        # 检查必填字段是否存在
        if not data.get('task_name') or not data.get('task_type'):
            return make_response(message="task_name and task_type are required", status=400)

        # 创建 TaskM 实例
        task_id = TaskM.create_task(
            task_name=data['task_name'],
            task_type=data['task_type'],
            run_time=datetime.datetime.strptime(data['run_time'], '%Y-%m-%d %H:%M:%S') if data.get('run_time') else None,
            status=data.get('status'),
            case_list=data.get('case_list', [])
        )
        return make_response(data={"task_id": task_id}, status=201)

    except Exception as e:
        return make_response(message=str(e), status=500)

