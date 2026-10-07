"""命令行入口：会务人员通过子命令完成提交、审批、发布、重放与解释。

用法示例：
  python run_cli.py submit --org 机构A --workgroup 甲组 --owner 张三 \
      --title 数字丝路 --summary ... --deadline 2026-11-01 --secrecy internal --idem k-1
  python run_cli.py explain T-1
  python run_cli.py replay 2
  python run_cli.py serve --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys

from .domain import Actor, DomainError
from .service import SummitService


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _actor(args) -> Actor:
    return Actor.of(args.actor, args.role, args.org)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="summit", description="峰会议题协同后端命令行")
    parser.add_argument("--data-dir", default=".summit_data",
                        help="数据目录（事件日志），默认 .summit_data")
    parser.add_argument("--actor", default="cli-operator", help="操作者标识")
    parser.add_argument("--role", default="coordinator",
                        help="角色: submitter/coordinator/approver/auditor")
    parser.add_argument("--org", default="", help="所属机构（提交人必配）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="提交议题提案")
    p.add_argument("--idem", required=True, help="幂等键（重复提交不产生副作用）")
    p.add_argument("--title", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--org", dest="proposal_org", required=True)
    p.add_argument("--workgroup", required=True)
    p.add_argument("--owner", required=True, help="责任人")
    p.add_argument("--deadline", required=True, help="YYYY-MM-DD")
    p.add_argument("--secrecy", default="public",
                   help="public/internal/confidential/secret")
    p.add_argument("--depends-on", action="append", default=[],
                   help="前置议题（议题号或标题），可多次指定")
    p.add_argument("--material", action="append", default=[], help="材料标识，可多次指定")

    p = sub.add_parser("withdraw", help="撤回提案（幂等）")
    p.add_argument("proposal_id")
    p.add_argument("--idem", required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("revise-deadline", help="修订提案截止时间（留下修订链）")
    p.add_argument("proposal_id")
    p.add_argument("--to", required=True, help="新截止时间 YYYY-MM-DD")
    p.add_argument("--reason", required=True)
    p.add_argument("--idem", required=True)

    p = sub.add_parser("revisions", help="查看提案截止时间修订链")
    p.add_argument("proposal_id")

    p = sub.add_parser("merge", help="归并议题")
    p.add_argument("--survivor", required=True, help="存活议题号")
    p.add_argument("--merged", nargs="+", required=True, help="被归并议题号")
    p.add_argument("--reason", required=True)
    p.add_argument("--idem", required=True)

    p = sub.add_parser("defer", help="延期议题")
    p.add_argument("topic_id")
    p.add_argument("--until", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--idem", required=True)

    p = sub.add_parser("transfer", help="移交议题到其他工作组")
    p.add_argument("topic_id")
    p.add_argument("--to-workgroup", required=True)
    p.add_argument("--to-org", default=None)
    p.add_argument("--reason", default="")
    p.add_argument("--idem", required=True)

    p = sub.add_parser("request-approval", help="发起审批")
    p.add_argument("topic_id")
    p.add_argument("--idem", required=True)

    p = sub.add_parser("decide", help="审批决定")
    p.add_argument("approval_id")
    p.add_argument("--decision", required=True, choices=["approved", "rejected"])
    p.add_argument("--reason", default="")
    p.add_argument("--idem", required=True)

    p = sub.add_parser("publish", help="发布议程版本（不可改写）")
    p.add_argument("--idem", required=True)

    sub.add_parser("agendas", help="列出全部议程版本")

    p = sub.add_parser("replay", help="重放指定议程版本")
    p.add_argument("version", type=int)

    p = sub.add_parser("explain", help="解释议题为何被合并/延期/拒绝/移交")
    p.add_argument("topic_id")

    p = sub.add_parser("list", help="列出提案（按角色脱敏）")
    p.add_argument("--org", dest="filter_org", default=None)

    p = sub.add_parser("topics", help="列出议题")
    p.add_argument("--status", default=None)

    p = sub.add_parser("show", help="查看议题详情")
    p.add_argument("topic_id")

    sub.add_parser("pending", help="列出未完成审批")
    sub.add_parser("conflicts", help="冲突检查")
    sub.add_parser("verify", help="校验事件链、议程链与修订链")

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    sub.add_parser("demo", help="端到端演示：提交→归并→审批→发布→重放→解释")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = SummitService.open(args.data_dir)
    try:
        if args.cmd == "serve":
            return _serve(service, args)
        if args.cmd == "demo":
            return _demo(service)
        actor = _actor(args)
        result = _run(service, actor, args)
        if result is not None:
            _print(result)
        return 0
    except DomainError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


def _run(service: SummitService, actor: Actor, args):
    cmd = args.cmd
    if cmd == "submit":
        return service.submit_proposal(
            actor, idem=args.idem, title=args.title, summary=args.summary,
            org=args.proposal_org, workgroup=args.workgroup, owner=args.owner,
            deadline=args.deadline, secrecy=args.secrecy,
            depends_on=args.depends_on, materials=args.material)
    if cmd == "withdraw":
        return service.withdraw_proposal(actor, args.proposal_id,
                                         idem=args.idem, reason=args.reason)
    if cmd == "revise-deadline":
        return service.revise_deadline(actor, args.proposal_id,
                                       new_deadline=args.to, reason=args.reason,
                                       idem=args.idem)
    if cmd == "revisions":
        return service.revision_chain(actor, args.proposal_id)
    if cmd == "merge":
        return service.merge_topics(actor, survivor_id=args.survivor,
                                    merged_ids=args.merged, reason=args.reason,
                                    idem=args.idem)
    if cmd == "defer":
        return service.defer_topic(actor, args.topic_id, until=args.until,
                                   reason=args.reason, idem=args.idem)
    if cmd == "transfer":
        return service.transfer_topic(actor, args.topic_id,
                                      to_workgroup=args.to_workgroup,
                                      to_org=args.to_org, reason=args.reason,
                                      idem=args.idem)
    if cmd == "request-approval":
        return service.request_approval(actor, args.topic_id, idem=args.idem)
    if cmd == "decide":
        return service.decide_approval(actor, args.approval_id,
                                       decision=args.decision,
                                       reason=args.reason, idem=args.idem)
    if cmd == "publish":
        return service.publish_agenda(actor, idem=args.idem)
    if cmd == "agendas":
        return service.list_agendas(actor)
    if cmd == "replay":
        return service.replay_agenda(actor, args.version)
    if cmd == "explain":
        return service.explain_topic(actor, args.topic_id)
    if cmd == "list":
        return service.list_proposals(actor, org=args.filter_org)
    if cmd == "topics":
        return service.list_topics(actor, status=args.status)
    if cmd == "show":
        return service.get_topic(actor, args.topic_id)
    if cmd == "pending":
        return service.pending_approvals(actor)
    if cmd == "conflicts":
        return service.check_conflicts(actor)
    if cmd == "verify":
        return service.verify(actor)
    raise DomainError("invalid_command", f"未知命令: {cmd}")


def _serve(service: SummitService, args) -> int:
    from .api import create_server

    server = create_server(service, host=args.host, port=args.port)
    pending = service.pending_approvals(Actor.of("boot", "auditor"))
    print(f"峰会协同服务已启动: http://{args.host}:{args.port} "
          f"(恢复待审批 {len(pending)} 项)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _demo(service: SummitService) -> int:
    """端到端演示完整流程。重复执行安全（全部命令带幂等键）。"""
    coord = Actor.of("li-hui", "coordinator")
    sub_a = Actor.of("wang-ke", "submitter", org="机构A")
    sub_b = Actor.of("zhao-min", "submitter", org="机构B")
    approver = Actor.of("chen-shen", "approver")

    # 1. 两个工作组重复承诺同一议题 → 自动挂靠 + 冲突告警
    r1 = service.submit_proposal(
        sub_a, idem="demo-p1", title="数字丝路合作", summary="跨境数据流动试点方案",
        org="机构A", workgroup="甲组", owner="张三", deadline="2026-11-15",
        secrecy="internal", materials=["M-101"])
    service.submit_proposal(
        sub_b, idem="demo-p2", title="数字丝路合作", summary="数字丝路联合倡议草案",
        org="机构B", workgroup="乙组", owner="李四", deadline="2026-11-20",
        secrecy="confidential", materials=["M-202"])
    # 2. 近似议题另立一题，由协调员显式归并
    r3 = service.submit_proposal(
        sub_b, idem="demo-p3", title="数字丝绸之路倡议", summary="倡议文本与路线图",
        org="机构B", workgroup="乙组", owner="李四", deadline="2026-11-25",
        secrecy="internal")
    tid = r1["topic_id"]
    # 3. 截止时间修订（留下修订链）
    service.revise_deadline(sub_a, r1["proposal_id"], new_deadline="2026-11-30",
                            reason="筹备组统一延后", idem="demo-r1")
    # 4. 归并 → 审批 → 发布
    service.merge_topics(coord, survivor_id=tid, merged_ids=[r3["topic_id"]],
                         reason="与数字丝路合作议题重复，归并处理", idem="demo-m1")
    ap = service.request_approval(coord, tid, idem="demo-a1")
    service.decide_approval(approver, ap["approval_id"], decision="approved",
                            reason="符合峰会方向", idem="demo-d1")
    pub = service.publish_agenda(coord, idem="demo-pub1")
    out = {
        "topic_id": tid,
        "merged_topic_id": r3["topic_id"],
        "agenda": pub,
        "replay": service.replay_agenda(coord, pub["version"]),
        "explain_survivor": service.explain_topic(coord, tid),
        "explain_merged": service.explain_topic(coord, r3["topic_id"]),
        "conflicts": service.check_conflicts(coord),
        "verify": service.verify(coord),
    }
    _print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
