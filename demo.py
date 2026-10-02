"""端到端演示：配置只写引用，运行时按需从密钥服务取值。

运行：python3 demo.py
"""
from secretref import CycleError, LazyConfig, Resolver

# ---- 一个会打印调用日志的假密钥服务（真实场景替换为 KMS/Vault 客户端）----
class LoggedSecretService:
    def __init__(self, values):
        self._values = values

    def get(self, key, *, timeout=None):
        print(f"  [密钥服务] get({key!r}, timeout={timeout!r})")
        return self._values[key]


service = LoggedSecretService({
    "prod/db/host": "db.internal",
    "prod/db/password": "DB-SECRET-9f3a",
    "prod/api/token": "TOKEN-SHOULD-NOT-BE-FETCHED",
})

config = LazyConfig(
    {
        "db_url": "postgres://${secret:prod/db/host}/app",
        "db_password": "${secret:prod/db/password}",
        "api_token": "${secret:prod/api/token}",
    },
    Resolver(service, timeout=2.0),
)

print("== 只使用 db_url / db_password，api_token 不应被取 ==")
print("db_url     =", config["db_url"])
print("db_password=", config["db_password"])

print("\n== 循环引用报错（只显示路径，不显示任何密钥值）==")
try:
    Resolver(
        LoggedSecretService({
            "a": "${secret:b}",
            "b": "${secret:a}",
        })
    ).resolve("${secret:a}", origin="config:x")
except CycleError as exc:
    print(type(exc).__name__ + ":", exc)
