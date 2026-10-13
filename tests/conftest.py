"""测试全局配置：live 用例开关。

`pytest --live` 只在网内实跑真依赖（真 boto3/真桶/真库）的用例；默认跳过
——CI 无网内通道，但「骗不了人的用例」必须存在（摄取契约缺陷曾因测试只
覆盖替身形态而长期潜伏）。
"""


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="实跑标记为 live 的真依赖用例（需网内环境变量）")
