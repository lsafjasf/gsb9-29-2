# 作用域用例集：预期与实际对照表

- 参照基线：RFC 6265 §4/§5（Cookie 作用域）与 §5.4（回传选择）
- 时钟固定注入为 `1700000000.0`；公共后缀清单使用库内置示例集（`com`、`co.uk`、`appspot.com` 等，可注入替换）
- “实际”一列由 `scripts/gen_scope_table.py` 真实执行 CookieJar 得出；`tests/test_scope.py` 对每行做相等断言

| 编号 | 场景 | Set-Cookie | 设置 URL | 请求 URL | 预期 | 实际 | 一致 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SC-01 | 主机精确匹配（host-only 默认） | `sid=1; Path=/` | `https://example.com/` | `https://example.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-02 | host-only Cookie 不下发到子域 | `sid=1; Path=/` | `https://example.com/` | `https://sub.example.com/` | 接受，不回传 | 接受，不回传 | 是 |
| SC-03 | Domain 属性覆盖子域 | `sid=1; Domain=example.com; Path=/` | `https://example.com/` | `https://sub.example.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-04 | 子域设置父域（合法上溯） | `sid=1; Domain=example.com; Path=/` | `https://sub.example.com/` | `https://example.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-05 | 父域设置子域（越权下钻） | `sid=1; Domain=sub.example.com` | `https://example.com/` | `—` | 拒绝（INVALID_DOMAIN） | 拒绝（INVALID_DOMAIN） | 是 |
| SC-06 | 前缀相似但不同域 notexample.com | `sid=1; Domain=example.com; Path=/` | `https://example.com/` | `https://notexample.com/` | 接受，不回传 | 接受，不回传 | 是 |
| SC-07 | 连字符相似域 evil-example.com | `sid=1; Domain=example.com; Path=/` | `https://example.com/` | `https://evil-example.com/` | 接受，不回传 | 接受，不回传 | 是 |
| SC-08 | Domain 是主机后缀但无点边界 ample.com | `sid=1; Domain=ample.com` | `https://example.com/` | `—` | 拒绝（INVALID_DOMAIN） | 拒绝（INVALID_DOMAIN） | 是 |
| SC-09 | 域名大小写不敏感 | `sid=1; Domain=EXAMPLE.com; Path=/` | `https://WWW.EXAMPLE.COM/` | `https://example.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-10 | Domain 前导点被剥离 | `sid=1; Domain=.example.com; Path=/` | `https://example.com/` | `https://sub.example.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-11 | 公共后缀 com 拒绝 | `sid=1; Domain=com` | `https://example.com/` | `—` | 拒绝（PUBLIC_SUFFIX） | 拒绝（PUBLIC_SUFFIX） | 是 |
| SC-12 | 公共后缀 co.uk 拒绝 | `sid=1; Domain=co.uk` | `https://shop.example.co.uk/` | `—` | 拒绝（PUBLIC_SUFFIX） | 拒绝（PUBLIC_SUFFIX） | 是 |
| SC-13 | 公共后缀之下的可注册域正常 | `sid=1; Domain=example.co.uk; Path=/` | `https://shop.example.co.uk/` | `https://www.example.co.uk/` | 接受，回传 | 接受，回传 | 是 |
| SC-14 | IPv4 字面量精确匹配 | `sid=1; Path=/` | `http://127.0.0.1/` | `http://127.0.0.1/` | 接受，回传 | 接受，回传 | 是 |
| SC-15 | IPv4 字面量不做后缀匹配 | `sid=1; Domain=127.0.0.1; Path=/` | `http://127.0.0.1/` | `http://127.0.0.2/` | 接受，不回传 | 接受，不回传 | 是 |
| SC-16 | IPv4 Domain 属性与主机相等可接受 | `sid=1; Domain=127.0.0.1; Path=/` | `http://127.0.0.1/` | `http://127.0.0.1/` | 接受，回传 | 接受，回传 | 是 |
| SC-17 | IPv4 Domain 属性后缀欺骗 0.0.1 | `sid=1; Domain=0.0.1` | `http://127.0.0.1/` | `—` | 拒绝（IP_LITERAL_MISMATCH） | 拒绝（IP_LITERAL_MISMATCH） | 是 |
| SC-18 | 端口不参与作用域隔离 | `sid=1; Path=/` | `http://example.com:8080/` | `http://example.com:9090/` | 接受，回传 | 接受，回传 | 是 |
| SC-19 | 路径前缀陷阱 /foo 不匹配 /foobar | `sid=1; Path=/foo` | `https://example.com/foo` | `https://example.com/foobar` | 接受，不回传 | 接受，不回传 | 是 |
| SC-20 | 路径前缀合法下延 /foo/bar | `sid=1; Path=/foo` | `https://example.com/foo` | `https://example.com/foo/bar` | 接受，回传 | 接受，回传 | 是 |
| SC-21 | 路径精确匹配 /foo | `sid=1; Path=/foo` | `https://example.com/foo` | `https://example.com/foo` | 接受，回传 | 接受，回传 | 是 |
| SC-22 | Path=/foo/ 不匹配 /foo | `sid=1; Path=/foo/` | `https://example.com/foo/` | `https://example.com/foo` | 接受，不回传 | 接受，不回传 | 是 |
| SC-23 | 默认路径推导 /account/login -> /account | `sid=1` | `https://example.com/account/login` | `https://example.com/account/settings` | 接受，回传 | 接受，回传 | 是 |
| SC-24 | 默认路径边界 /account 不匹配 /accountant | `sid=1` | `https://example.com/account/login` | `https://example.com/accountant` | 接受，不回传 | 接受，不回传 | 是 |
| SC-25 | 单段 URI 默认路径为 / | `sid=1` | `https://example.com/login` | `https://example.com/anything` | 接受，回传 | 接受，回传 | 是 |
| SC-26 | IPv6 字面量精确匹配（端口忽略） | `sid=1; Path=/` | `http://[::1]:8080/` | `http://[::1]:9090/` | 接受，回传 | 接受，回传 | 是 |
| SC-27 | 公共后缀即主机本身时按 host-only 接受 | `sid=1; Domain=appspot.com; Path=/` | `https://appspot.com/` | `https://appspot.com/` | 接受，回传 | 接受，回传 | 是 |
| SC-28 | 公共后缀子域试图设置父后缀 appspot.com | `sid=1; Domain=appspot.com` | `https://myapp.appspot.com/` | `—` | 拒绝（PUBLIC_SUFFIX） | 拒绝（PUBLIC_SUFFIX） | 是 |
| SC-29 | 空 Domain 属性按缺省 host-only 处理 | `sid=1; Domain=; Path=/` | `https://example.com/` | `https://sub.example.com/` | 接受，不回传 | 接受，不回传 | 是 |
| SC-30 | 主机尾点规范化 example.com. == example.com | `sid=1; Path=/` | `https://example.com./` | `https://example.com/` | 接受，回传 | 接受，回传 | 是 |

## 说明

- **SC-01 / SC-02**：无 `Domain` 属性时为 host-only，仅设置主机本身可见。
- **SC-03 ~ SC-05**：`Domain` 只能上溯到父域，不能下钻到子域。
- **SC-06 ~ SC-08**：子域匹配必须以 `.` 为边界，禁止前缀包含。
- **SC-11 ~ SC-13 / SC-27 / SC-28**：`Domain` 为公共后缀时拒绝；主机本身恰为该后缀时按 host-only 接受（RFC 6265 §5.3 第 6 步）。
- **SC-14 ~ SC-17 / SC-26**：IP 字面量只做精确匹配，不允许任何后缀域。
- **SC-18**：Cookie 不按端口隔离（RFC 6265 §7.2），是否收紧由调用方决定。
- **SC-19 ~ SC-25**：路径匹配要求路径边界为 `/`，默认路径取请求 URI 最后一段 `/` 之前的部分。
- **SC-29 / SC-30**：空 `Domain` 回退 host-only；主机尾点规范化。
