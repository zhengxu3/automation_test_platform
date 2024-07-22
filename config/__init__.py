

class Config:
    MONGODB_SETTINGS = {
        'db': 'match_test_platform',
        'host': '127.0.0.1',
        'port': 27017,
        'username': 'adminuser',    # MongoDB 用户名
        'password': 'admin123',    # MongoDB 密码
        'authentication_source': 'match_test_platform'
    }


class ProductionConfig(Config):
    DEBUG = False
    ENV = 'production'
    MONGODB_SETTINGS = {
        'db': 'match_test_platform',
        'host': '54.248.23.255',
        'port': 27017,
        'username': 'adminadmin',    # MongoDB 用户名
        'password': 'admin7788#!',    # MongoDB 密码
    }


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    ENV = 'testing'
    MONGODB_SETTINGS = {
        'db': 'match_test_platform',
        'host': '127.0.0.1',
        'port': 27017,
        'username': 'adminuser',    # MongoDB 用户名
        'password': 'admin123',    # MongoDB 密码
        'authentication_source': 'match_test_platform'
    }


config = {
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': TestingConfig
}
