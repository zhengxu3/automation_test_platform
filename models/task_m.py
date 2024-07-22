#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/19
@Python：python 3.9
"""
from mongoengine import *
from . import db
from bson import ObjectId
import datetime


class TaskM(db.Document):
    task_name = StringField(required=True)
    task_type = StringField(required=True)
    create_time = DateTimeField(default=datetime.datetime.utcnow)
    run_time = DateTimeField()
    status = StringField()
    case_list = ListField(db.StringField())
    meta = {
        'collection': 'tasks_list'  # 指定集合名
    }

    @classmethod
    def create_task(cls, task_name, task_type, run_time=None, status=None, case_list=None):
        """创建并保存一个新的 task，并返回其 ID"""
        task = cls(task_name=task_name, task_type=task_type, run_time=run_time, status=status, case_list=case_list)
        task.save()
        return str(task.id)

    @classmethod
    def get_task_by_id(cls, task_id):
        """根据 _id 查询 task"""
        try:
            return cls.objects(id=ObjectId(task_id)).first()
        except Exception as e:
            print(f"Error: {e}")
            return None

    @classmethod
    def get_tasks_by_conditions(cls, **conditions):
        """根据条件查询 tasks"""
        return cls.objects(**conditions)

    def update_task(self, **kwargs):
        """更新 task 字段"""
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.save()
        return self

    @classmethod
    def delete_task_by_id(cls, task_id):
        """根据 _id 删除 task"""
        try:
            task = cls.objects(id=ObjectId(task_id)).first()
            if task:
                task.delete()
                return True
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False

    @classmethod
    def delete_tasks_by_conditions(cls, **conditions):
        """根据条件删除 tasks"""
        return cls.objects(**conditions).delete()

    @classmethod
    def get_pending_tasks(cls):
        return cls.objects(status='pending')

    @classmethod
    def mark_task_completed(cls, task_id):
        task = cls.objects(id=task_id).first()
        if task:
            task.status = 'completed'
            task.save()
            return task
        return None
