from flask import Flask
from .home.index_page import index_page_bp
from .openapi.callback import call_back_bp
from flask_mongoengine import MongoEngine
# from .user.views import user_bp

from mongoengine import Document, BooleanField, StringField, EmailField, DateTimeField
import random
from config import config
from core.tools_core import *
from core.tools_core import setup_logging
setup_logging()


def create_app(config_name='default'):
    app = Flask(__name__)
    logging.info('start flask app')

    app.config.from_object(config[config_name])
    db = MongoEngine()

    # 将配置对象存储在 Flask 全局变量中
    app.config_obj = app.config

    db.init_app(app)

    # class UserInfo(db.Document):
    #     username = StringField(required=True, unique=True)
    #     email = EmailField(required=True, unique=True)
    #     created_at = DateTimeField(auto_now_add=True)
    #     is_active = BooleanField(default=True)

    # # 示例: 创建和保存文档
    # new_user = UserInfo(username='john_' + str(random.randint(1000,100000)) + '_doe', email='john@' + str(random.randint(1000,100000)) + 'example.com')
    # new_user.save()
    # print(new_user.id)

    # 注册蓝图
    app.register_blueprint(index_page_bp, url_prefix='/home')
    app.register_blueprint(call_back_bp, url_prefix='/openapi')

    return app
