# 策略说明

本库以 RFC 6265 为基线，并吸收 RFC 6265bis 的 SameSite 语义。时间源与公共后缀清单均通过构造函数注入，不依赖系统时钟或网络。

## 解析规则（Set-Cookie）

- 第一个 `;` 之前必须含 `=`，否则拒绝：`MISSING_EQUALS`；空名（`=value`）拒绝：`EMPTY_NAME`；空头拒绝：`EMPTY_HEADER`。
- 名称、值按空格裁剪；值允许为空（`sid=`）；首尾配对双引号会被剥离；值中可含 `=`。
- 属性：`Expires`、`Max-Age`、`Domain`、`Path`、`Secure`、`HttpOnly`、`SameSite` 全部解析；属性名大小写不敏感；未知属性忽略（符合 RFC 6265“忽略未知”原则）。
- 日期解析实现 RFC 6265 §5.1.1 宽松算法，兼容 RFC 1123、RFC 850、asctime 与两位年份（0–69→2000s，70–99→1900s）。非法日期忽略 `Expires` 属性；非法 `Max-Age` 忽略该属性。
- `Max-Age` 优先级高于 `Expires`；`Max-Age` 在**入库时刻**换算为绝对过期时间，之后只由注入时钟判定。
- 严格模式（`Policy.strict_attributes=True`）下，非法 `SameSite` 值直接拒绝：`INVALID_ATTRIBUTE`；默认宽松模式下忽略该属性。
- 名称+值的 UTF-8 字节数超过 `max_cookie_bytes`（默认 4096）拒绝：`COOKIE_TOO_LONG`。

## 域名作用域

- 无 `Domain` 属性 → host-only，规范化域名为请求主机本身，只回传给该主机。
- `Domain` 属性：前导点剥离、小写、尾点规范化；必须等于请求主机或以 `请求主机. + domain` 的点边界后缀成立，否则拒绝 `INVALID_DOMAIN`。这保证 `ample.com` 不匹配 `example.com`、`example.com` 不匹配 `notexample.com` 与 `evil-example.com`。
- **公共后缀**：`Domain` 落在注入清单内时拒绝（`PUBLIC_SUFFIX`）；若请求主机本身恰等于该后缀（如主机就是 `appspot.com`），按 RFC 6265 §5.3 第 6 步降级为 host-only 接受。清单支持精确项与单级通配符 `*.ck`（仅匹配下一级，与 PSL 通配符语义一致）。
- **IP 字面量**（IPv4/IPv6）：只做精确匹配；`Domain` 与 IP 主机不相等一律拒绝（`IP_LITERAL_MISMATCH`），不允许任何后缀解释。
- **端口不参与作用域**（RFC 6265 §7.2：明确不提供端口隔离）。如需端口隔离应由调用方在上层强制；用例 SC-18 固化此行为。
- 域名比较大小写不敏感；主机尾点会被规范化去掉。
- host-only Cookie 与同名同域同路径的 Domain Cookie 共享同一个存储键，后者覆盖前者（反之亦然），不会并存两份。

## 路径作用域

- 无 `Path` 属性或值不是绝对路径时，默认路径推导自请求 URI：不含 `/` 或只有一个 `/` → `/`；否则取最后一个 `/` 之前（不含该斜杠）的部分。
- 匹配（RFC 6265 §5.1.4）：路径相等；或请求路径以 Cookie 路径为前缀且 Cookie 路径以 `/` 结尾；或前缀之后的第一个字符是 `/`。`/foo` 不匹配 `/foobar`；`/foo/` 不匹配 `/foo`。

## 安全策略

- 默认安全协议集合为 `{https, wss}`。在非安全协议上设置带 `Secure` 的 Cookie 默认拒绝：`INSECURE_SCHEME`（可由 `allow_secure_from_insecure_scheme` 放开，放开后回传时仍按协议过滤）。回传时非安全请求排除 Secure Cookie，原因码 `NOT_SECURE_CONTEXT`。
- `SameSite=None` 必须同时带 `Secure`，否则设置时拒绝：`SAMESITE_NONE_WITHOUT_SECURE`（现代浏览器行为，可用策略关闭）。
- 无 SameSite 属性的 Cookie 默认按 **Lax** 处理（`default_same_site_lax`，对齐 Chrome 的默认行为）：
  - `Strict`：跨站请求不回传（`SAMESITE_STRICT`）。
  - `Lax`：仅同站、或跨站顶层导航且方法为安全方法（GET/HEAD/OPTIONS/TRACE）时回传；跨站 POST 或子资源不回传（`SAMESITE_LAX`）。
  - `None`：不限制（Secure 另算）。
- 同站判定上下文（same-site / cross-site、顶层导航、HTTP 方法）由调用方注入；库内提供 `scope.registrable_domain` / `scope.same_site` 辅助函数（基于注入的公共后缀清单）。
- `HttpOnly`：HTTP 请求上下文正常回传；`for_http=False`（如 document.cookie 类非 HTTP API）时排除，原因码 `HTTPONLY`。

## 存储规则（确定性）

- **存储键**：`(name, domain, path)`，域名为规范化后的值。
- **覆盖**：同键 Cookie 用新值与新属性替换，但保留原 `creation_time` 与创建序号（RFC 6265bis），因此覆盖不会改变该 Cookie 在回传排序中的位置；`last_access` 更新为当前时间。
- **过期**：`expires_at <= now` 即过期；入库即过期的 Set-Cookie 不存储，并删除同键旧 Cookie（删除语义，`removed_existing=True`，返回码 `ALREADY_EXPIRED`）。过期项在每次读写时惰性淘汰。
- **容量**：`max_total_cookies`（默认 3000）与 `max_per_domain`（默认 180，按规范化域名字符串计数）。
- **淘汰顺序**：先淘汰所有已过期项（确定性，入库顺序由序号保证）；再按 LRU（`last_access` 升序）淘汰，同时间以创建序号（更早创建）为第二键。新入库 Cookie 的 `last_access` 为当前时间，因此永远不会成为同批首个受害者。`SetResult.evicted` 返回被淘汰 Cookie 的有序列表。

## 回传选择与顺序

- 过滤顺序（首个命中即排除，原因码写入 `SendResult.excluded`）：过期 → 域名 → 路径 → 安全协议 → HttpOnly → SameSite。
- 排序严格按 RFC 6265 §5.4（即 Chrome/Firefox 等参照实现遵循的规则）：**路径长度降序；同路径按创建时间升序**（用单调创建序号表达）。输出形如 `k=v; k2=v2`。
- 每次成功选中回传会刷新对应 Cookie 的 `last_access`，与容量淘汰联动。

## 时间与时间跳变

- 时钟为无参可调用对象，返回 epoch 秒；所有判定只使用该值。
- 向前跳变越过 `expires_at` → Cookie 立即淘汰；向后跳变 → Cookie 自然“续命”，行为确定且可复现；会话 Cookie（无 Expires/Max-Age）不受任何跳变影响。

## 已知边界

- 仅处理 ASCII 域名（未做 IDNA 编码）；输入须为 URL 字符串，解析失败返回 `INVALID_URL`。
- 公共后缀清单为**注入数据**：内置的是小样本示例，生产环境应注入完整 PSL 快照（含例外规则时需调用方自行展平，本库匹配精确项与单级 `*.` 通配符）。
- 不实现 Cookie 前缀（`__Secure-` / `__Host-`）校验与 RFC 2965 的 `$` 属性；如需要可在策略层扩展。
