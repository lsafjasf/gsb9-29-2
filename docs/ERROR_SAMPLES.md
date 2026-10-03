# 错误样例

以下输出由 `PYTHONPATH=. python3 examples/error_samples.py` 实际生成。
所有协商错误都附带双方能力清单：`offered` / `required` / `supported` / `agreed`，
以及对端原始报文 `peer raw view`。

```text

========================================================================
  1) 协商失败：客户端 required=zlib，服务端只支持 priority
========================================================================
[客户端抛出]
NegotiationFailed : 对端返回协议错误: negotiation_failed
能力清单:
  client offered  : ['priority', 'zlib']
  client required : ['zlib']
  server supported: ['priority']
  agreed          : ['priority']
  peer raw view   : {'type': 'error', 'code': 'negotiation_failed', 'detail': {'offered': ['priority', 'zlib'], 'required': ['zlib'], 'supported': ['priority'], 'agreed': ['priority'], 'missing': ['zlib']}}

[服务端抛出]
NegotiationFailed : 必需扩展未被对端支持: ['zlib']
能力清单:
  client offered  : ['priority', 'zlib']
  client required : ['zlib']
  server supported: ['priority']
  agreed          : ['priority']
  peer raw view   : {'ext': ['priority']}

========================================================================
  2) 声明矛盾：ack 包含客户端从未提供的扩展
========================================================================
NegotiationContradiction : 对端 ack 包含从未提供的扩展: ['telepathy']
能力清单:
  client offered  : ['priority', 'zlib']
  client required : []
  server supported: ['priority', 'zlib']
  agreed          : ['priority', 'telepathy', 'zlib']
  peer raw view   : {'ext_ack': ['priority', 'telepathy', 'zlib']}

========================================================================
  3) 声明矛盾：数据帧使用未协商成功的扩展字段
========================================================================
NegotiationContradiction : 对端使用了未协商成功的扩展字段: ['priority']
能力清单:
  client offered  : ['zlib']
  client required : []
  server supported: ['zlib']
  agreed          : ['zlib']
  peer used       : ['priority']
  peer raw view   : {'type': 'data', 'seq': 0, 'payload': 'x', 'pri': 0}

========================================================================
  4) 协商字段被篡改：MITM 从 hello 中删除 zlib
========================================================================
[客户端抛出]
NegotiationTampered : 对端返回协议错误: negotiation_tampered
能力清单:
  client offered  : ['priority']
  client required : []
  server supported: ['priority', 'zlib']
  agreed          : ['priority']
  peer raw view   : {'type': 'error', 'code': 'negotiation_tampered', 'detail': {'offered': ['priority'], 'required': [], 'supported': ['priority', 'zlib'], 'agreed': ['priority']}}

[服务端抛出]
NegotiationTampered : 客户端握手指纹不一致，协商字段可能被篡改或剥离
能力清单:
  client offered  : ['priority']
  client required : []
  server supported: ['priority', 'zlib']
  agreed          : ['priority']
  peer raw view   : {'type': 'finish', 'digest': '3c86ce78539846a3732d45e2b6c6db0e83a1a694e4b96a2ea90c9600958e714a'}

========================================================================
  5) 必需扩展遇到旧对端（禁止降级）
========================================================================
NegotiationFailed : 对端为旧版本（无协商字段），无法满足必需扩展
能力清单:
  client offered  : ['priority', 'zlib']
  client required : ['zlib']
  server supported: []
  agreed          : []
  peer raw view   : {'type': 'hello_ack', 'proto': 1}
```
