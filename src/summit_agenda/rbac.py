"""基于角色的访问控制（RBAC）。

两条正交规则：
1. 命令权限：什么角色能执行什么操作（提交/归并/审批/发布……）。
2. 摘要可见性：材料摘要按保密级别 + 角色 clearance + 机构归属做脱敏。
"""
from __future__ import annotations

from .domain import Actor, DomainError, ROLE_CLEARANCE, Role, Secrecy


#: 各命令所需角色。submitter 的撤回另有"本机构"约束，在 service 层校验。
COMMAND_ROLES: dict[str, frozenset[Role]] = {
    "submit": frozenset({Role.SUBMITTER, Role.COORDINATOR}),
    "withdraw": frozenset({Role.SUBMITTER, Role.COORDINATOR}),
    "revise_deadline": frozenset({Role.SUBMITTER, Role.COORDINATOR}),
    "merge": frozenset({Role.COORDINATOR}),
    "defer": frozenset({Role.COORDINATOR}),
    "transfer": frozenset({Role.COORDINATOR}),
    "request_approval": frozenset({Role.COORDINATOR}),
    "decide": frozenset({Role.APPROVER}),
    "publish": frozenset({Role.COORDINATOR}),
}


def require_command(actor: Actor, command: str) -> None:
    """校验操作者是否有权执行命令，无权则抛 DomainError(forbidden)。"""
    allowed = COMMAND_ROLES.get(command)
    if allowed is None:
        raise DomainError("invalid_command", f"未知命令: {command}")
    if actor.role not in allowed:
        raise DomainError(
            "forbidden",
            f"角色 {actor.role.value} 无权执行 {command}，需要: "
            + "/".join(sorted(r.value for r in allowed)),
        )


def can_view_summary(actor: Actor, secrecy: Secrecy, owner_org: str) -> bool:
    """判断操作者是否能看到某份材料的摘要。

    规则：
    - 材料保密级别不得超过角色 clearance；
    - 提交人只能看本机构材料（公开材料除外），防止敏感材料跨机构扩散。
    """
    if secrecy > ROLE_CLEARANCE[actor.role]:
        return False
    if actor.role is Role.SUBMITTER and owner_org != actor.org and secrecy > Secrecy.PUBLIC:
        return False
    return True


def redact_summary(view: dict, actor: Actor) -> dict:
    """对单个提案视图按操作者权限脱敏。无权时摘要以 null + redacted 标记返回。"""
    secrecy = Secrecy.parse(view["secrecy"])
    if can_view_summary(actor, secrecy, view.get("org", "")):
        out = dict(view)
        out["redacted"] = False
        return out
    out = dict(view)
    out["summary"] = None
    out["materials"] = []
    out["redacted"] = True
    return out
