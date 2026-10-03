"""扩展协商纯逻辑：与具体传输无关，便于单测。"""

from .errors import NegotiationContradiction, NegotiationFailed


def _normalize(names, field):
    if names is None:
        return []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise NegotiationContradiction(
            f"协商字段 {field!r} 必须是字符串数组",
            peer_capabilities=names,
        )
    # 去重并保持确定性顺序；重复声明是良性但需规范化
    return sorted(set(names))


def validate_offer(offered, required):
    """校验本端/对端提供的协商字段是否合法。

    - 字段必须为字符串数组（否则是矛盾/篡改后的畸形声明）。
    - required 必须是 offered 的子集。
    返回 (offered, required) 规范化后的有序列表。
    """
    offered = _normalize(offered, "ext")
    required = _normalize(required, "ext_required")
    extra = set(required) - set(offered)
    if extra:
        raise NegotiationContradiction(
            f"required 扩展 {sorted(extra)} 不在 offered 列表中",
            offered=offered, required=required,
            peer_capabilities={"ext": offered, "ext_required": required},
        )
    return offered, required


def negotiate(client_offered, client_required, server_supported):
    """计算协商结果：客户端 offered ∩ 服务端 supported。

    所有客户端 required 扩展都必须在交集内，否则抛 NegotiationFailed
    （错误中带双方能力清单）。
    """
    offered, required = validate_offer(client_offered, client_required)
    supported = _normalize(server_supported, "server_supported")

    agreed = sorted(set(offered) & set(supported))
    missing = sorted(set(required) - set(agreed))
    if missing:
        raise NegotiationFailed(
            f"必需扩展未被对端支持: {missing}",
            offered=offered, required=required,
            supported=supported, agreed=agreed,
            peer_capabilities={"ext": supported},
        )
    return agreed


def validate_ack(ack_ext, offered, supported=()):
    """校验服务端 ack 中的扩展列表。

    ack 只能包含客户端 offered 过的扩展（多余 => 对端声明矛盾）。
    """
    ack = _normalize(ack_ext, "ext")
    if not set(ack) <= set(offered):
        unexpected = sorted(set(ack) - set(offered))
        raise NegotiationContradiction(
            f"对端 ack 包含从未提供的扩展: {unexpected}",
            offered=offered, supported=supported, agreed=ack,
            peer_capabilities={"ext_ack": ack},
        )
    return ack
