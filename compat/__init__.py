"""加密文件格式跨版本兼容性测试框架。

- format_v1 / format_v2：两个版本的参考实现（v2 变更了块大小与文件头布局）。
- framework：兼容性判定框架，输出兼容矩阵。
- samples：确定性地生成/保留各版本样例文件与密钥。
- selftest：框架自测（参考实现必须全过，错误实现必须被判失败）。
"""

SUPPORTED_VERSIONS = (1, 2)
