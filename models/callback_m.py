#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-
"""
@Author: Zheng.Xu
@Date: 2024/7/19
@Python：python 3.9
"""
from . import db
from bson import ObjectId
from mongoengine import Document, BooleanField, StringField, EmailField, DateTimeField


class Callback(db.Document):
    uid = StringField(required=True)
    call_status = StringField()
    q_id = StringField()

    @classmethod
    def create_callback(cls, uid, call_status, q_id):
        """创建并保存一个新的 callback，并返回其 ID"""
        callback = cls(uid=uid, call_status=call_status, q_id=q_id)
        callback.save()
        return str(callback.id)

    @classmethod
    def get_callback_by_id(cls, callback_id):
        """根据 _id 查询 callback"""
        try:
            return cls.objects(id=ObjectId(callback_id)).first()
        except Exception as e:
            print(f"Error: {e}")
            return None

    @classmethod
    def get_callbacks_by_conditions(cls, uid=None, q_id=None):
        """根据 uid 和 q_id 查询 callback"""
        conditions = {}
        if uid is not None:
            conditions['uid'] = uid
        if q_id is not None:
            conditions['q_id'] = q_id
        return cls.objects(**conditions)

    def update_callback(self, **kwargs):
        """更新 callback 字段"""
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.save()
        return self

    @classmethod
    def delete_callback_by_id(cls, callback_id):
        """根据 _id 删除 callback"""
        try:
            callback = cls.objects(id=ObjectId(callback_id)).first()
            if callback:
                callback.delete()
                return True
            return False
        except Exception as e:
            print(f"Error: {e}")
            return False

    @classmethod
    def delete_callbacks_by_conditions(cls, uid=None, q_id=None):
        """根据条件删除 callback"""
        conditions = {}
        if uid is not None:
            conditions['uid'] = uid
        if q_id is not None:
            conditions['q_id'] = q_id
        return cls.objects(**conditions).delete()