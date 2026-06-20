import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import logger
from core.storage import storage
from core.state_machine import ReleaseState, StateMachine


def generate_release_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = ''.join(random.choices('0123456789', k=3))
    return f"REL-{ts}-{suffix}"


def create_release(
    version: str,
    release_type: str = "normal",
    summary: str = "",
    applicant: str = "developer",
    hotfix_reason: str = "",
    from_version: str = "",
) -> str:
    release_id = generate_release_id()
    storage.create_release(
        {
            "id": release_id,
            "version": version,
            "release_type": release_type,
            "summary": summary,
            "applicant": applicant,
            "state": ReleaseState.PENDING_SUBMIT,
            "hotfix_reason": hotfix_reason,
            "from_version": from_version,
        }
    )

    sm = StateMachine("release")
    event = sm.transition(
        release_id,
        None,
        ReleaseState.PENDING_SUBMIT,
        operator=applicant,
        reason="创建发布单",
    )
    storage.add_state_history(event.to_dict())

    logger.info(
        f"发布单已创建: {release_id} (v{version}, "
        f"类型={'常规迭代' if release_type == 'normal' else '紧急热修复'})"
    )
    return release_id


def show_release(release_id: str):
    release = storage.get_release(release_id)
    if not release:
        print(f"发布单不存在: {release_id}")
        return
    approvals = storage.get_approvals(release_id)
    print("\n" + "=" * 60)
    print(f"  发布单号: {release['id']}")
    print(f"  版本号:   {release['version']}")
    print(f"  发布类型: {'常规迭代' if release['release_type'] == 'normal' else '紧急热修复'}")
    print(f"  当前状态: {release['state']}")
    print(f"  申请人:   {release['applicant']}")
    print(f"  上一版本: {release.get('from_version') or '-'}")
    print(f"  灰度阶段: {release.get('current_phase') or '-'} ({release.get('gray_traffic_percent', 0)}%)")
    print(f"  校验得分: {release.get('pre_check_score') or '-'}")
    if release.get("summary"):
        print(f"  发布摘要: {release['summary']}")
    if release.get("hotfix_reason"):
        print(f"  紧急原因: {release['hotfix_reason']}")
    print("-" * 60)
    if approvals:
        print("  审批节点:")
        for a in approvals:
            status_icon = {"PENDING": "⏳", "APPROVED": "✅", "REJECTED": "❌",
                           "TIMEOUT": "⏰", "ESCALATED": "⬆️"}.get(a["status"], a["status"])
            who = a.get("approver") or "-"
            print(f"    {status_icon} {a['stage_name']} ({a['approver_role']}): {a['status']} by {who}")
    print("=" * 60 + "\n")


def list_releases(state: Optional[str] = None, limit: int = 20):
    releases = storage.list_releases(state=state, limit=limit)
    print(f"\n共 {len(releases)} 个发布单\n")
    print(f"{'发布单号':<25} {'版本':<12} {'类型':<8} {'状态':<20} {'申请人':<12} {'创建时间':<20}")
    print("-" * 100)
    for r in releases:
        rt = "常规" if r["release_type"] == "normal" else "紧急"
        print(
            f"{r['id']:<25} {r['version']:<12} {rt:<8} {r['state']:<20} "
            f"{r['applicant']:<12} {r['created_at'][:19]}"
        )
    print()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="干线运输调度系统 - 版本发布与自动回滚平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 创建常规发布单
  python release_cli.py create --version 2.3.1 --summary "调度算法优化" --applicant zhangsan

  # 创建紧急热修复
  python release_cli.py create --version 2.3.1-hotfix1 --type hotfix --hotfix-reason "P0故障：运费计算异常"

  # 查看发布单
  python release_cli.py show --release-id REL-20260620-001

  # 列出所有发布单
  python release_cli.py list

子命令 (scripts/ 目录下):
  pre_check.py       -- 发布前置校验
  approval_flow.py   -- 审批流转管理
  gray_release.py    -- 灰度发布管理
  rollback.py        -- 熔断监控与自动回滚
  drill_engine.py    -- 演练编排引擎
  report_generator.py-- 复盘报表生成
        """,
    )
    sub = parser.add_subparsers(dest="cmd")

    p_create = sub.add_parser("create", help="创建发布单")
    p_create.add_argument("--version", required=True, help="版本号，如 2.3.1")
    p_create.add_argument(
        "--type", choices=["normal", "hotfix"], default="normal", help="发布类型"
    )
    p_create.add_argument("--summary", default="", help="发布摘要")
    p_create.add_argument("--applicant", default="developer", help="申请人")
    p_create.add_argument("--hotfix-reason", default="", help="紧急修复原因（hotfix必填）")
    p_create.add_argument("--from-version", default="", help="上一版本号")

    p_show = sub.add_parser("show", help="查看发布单详情")
    p_show.add_argument("--release-id", required=True)

    p_list = sub.add_parser("list", help="列出发布单")
    p_list.add_argument("--state", default=None, help="按状态过滤")
    p_list.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.cmd == "create":
        if args.type == "hotfix" and not args.hotfix_reason:
            print("错误: 紧急热修复必须提供 --hotfix-reason")
            sys.exit(1)
        rid = create_release(
            args.version, args.type, args.summary, args.applicant,
            args.hotfix_reason, args.from_version
        )
        print(f"\n✅ 发布单已创建: {rid}")
        show_release(rid)
    elif args.cmd == "show":
        show_release(args.release_id)
    elif args.cmd == "list":
        list_releases(args.state, args.limit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
