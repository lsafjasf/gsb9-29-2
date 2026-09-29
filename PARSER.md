# ns_parser — 命名空间感知的 XML 解析器

纯 Python 3 标准库实现，无第三方依赖。输出事件流中的限定名采用
Clark 记法 `{namespace-uri}local`，可直接与 `xml.etree.ElementTree` 对拍。

## 文件

- `ns_parser.py` — 解析库。`parse_events(text)` 返回
  `("start", qname, attrs)` / `("end", qname)` / `("text", data)` 事件；
  错误抛出 `NsParseError`（含 1 起始的 `line` / `column`）。
- `difftest.py` — 对拍脚本：逐文件与 ElementTree 参照实现比较完整
  限定名事件序列，并演示错误定位样例。
- `tests/test_ns_parser.py` — 单元测试（unittest）。
- `data/*.xml` — 对拍文档：无命名空间、默认命名空间、前缀重定义
  （含三层嵌套与兄弟节点隔离）、属性/默认命名空间交互。
- `data/errors/*.xml` — 必须报错的样例：未声明前缀（元素/属性）、
  展开后重名属性。

## 语义要点

- `xmlns` / `xmlns:prefix` 声明的作用域为所在元素及其子树；内层
  重定义遮蔽外层，离开子树后外层绑定恢复，不泄漏到兄弟节点。
- 无前缀元素回落到默认命名空间；无前缀属性**永远**不在任何命名空间。
- `xmlns=""` 取消默认命名空间；`xml` 前缀预绑定；禁止声明 `xmlns` 前缀。
- 使用未声明前缀立即报错，行列号指向出错的名字本身（元素名或属性名）。

## 运行

```sh
python3 -m unittest discover -s tests -v   # 单元测试（含对拍与错误定位）
python3 difftest.py                        # 对拍 + 错误定位样例输出
```

## 错误定位样例

`data/errors/undeclared_prefix.xml` 第 4 行 `<oops:child/>`：

```
undeclared namespace prefix 'oops' (line 4, column 6)
```

`data/errors/undeclared_attr_prefix.xml` 第 2 行属性 `bad:nope`：

```
undeclared namespace prefix 'bad' on attribute 'bad:nope' (line 2, column 21)
```
