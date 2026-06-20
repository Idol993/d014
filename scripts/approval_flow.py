import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import logger
from core.config_loader import config
from core.storage import storage
from core.state_machine import ReleaseState, StateMachine
from core.notifier import notifier
from scripts.pre_check import PreCheckResult


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    ESCALATED = "ESCALATED"
    SKIPPED = "SKIPPED"


class ApprovalFlowEngine:
    def __init__(self):
        self.state_machine = StateMachine("release")
        self.approval_cfg = config.get("approval", {})

    def _get_channel_config(self, release_type: str) -> Dict[str, Any]:
        return self.approval_cfg.get("release_channels", {}).get(
            release_type, self.approval_cfg["release_channels"]["normal"]
        )

    def _check_pre_check_passed(self, release: Dict[str, Any]) -> Tuple[bool, str]:
        pre_check_result = release.get("pre_check_result")
        if not pre_check_result:
            return False, (
                f"发布单 {release['id']} 尚未执行前置校验，"
                f"请先运行 pre_check.py --release-id {release['id']} 完成质量门禁检查"
            )

        overall_result = None
        if isinstance(pre_check_result, dict):
            overall_result = pre_check_result.get("overall_result")
        elif isinstance(pre_check_result, str):
            overall_result = pre_check_result

        if overall_result == PreCheckResult.FAIL:
            score = release.get("pre_check_score", "?")
            return False, (
                f"发布单 {release['id']} 前置校验未通过（得分: {score}），"
                f"不满足准入条件，无法进入审批流程。请修复问题后重新执行前置校验"
            )

        return True, ""

    def init_approvals(self, release_id: str) -> List[Dict[str, Any]]:
        release = storage.get_release(release_id)
        if not release:
            logger.error(f"发布单不存在: {release_id}")
            return []

        passed, msg = self._check_pre_check_passed(release)
        if not passed:
            logger.error(f"审批初始化被阻断: {msg}")
            print(f"\n🚫 审批初始化被阻断: {msg}\n")
            return []

        channel_cfg = self._get_channel_config(release["release_type"])
        stages = channel_cfg.get("stages", [])
        approval_mode = channel_cfg.get("approval_mode", "serial")

        existing = storage.get_approvals(release_id)
        if existing:
            logger.info(f"审批已初始化，跳过: {release_id}")
            return existing

        created_ids = []
        for stage in stages:
            is_retroactive = stage.get("is_retroactive", False)
            if is_retroactive and approval_mode != "parallel":
                continue

            if "roles" in stage:
                for role in stage["roles"]:
                    aid = storage.add_approval(
                        {
                            "release_id": release_id,
                            "stage_key": stage["key"],
                            "stage_name": stage["name"],
                            "approver_role": role,
                            "status": ApprovalStatus.PENDING,
                        }
                    )
                    created_ids.append(aid)
            else:
                aid = storage.add_approval(
                    {
                        "release_id": release_id,
                        "stage_key": stage["key"],
                        "stage_name": stage["name"],
                        "approver_role": stage["role"],
                        "status": ApprovalStatus.PENDING,
                    }
                )
                created_ids.append(aid)

        logger.info(
            f"审批流程初始化完成: {release_id}, "
            f"模式={approval_mode}, 审批节点={len(created_ids)}"
        )

        if release["state"] == ReleaseState.PENDING_SUBMIT:
            try:
                self.state_machine.transition(
                    release_id,
                    release["state"],
                    ReleaseState.PENDING_APPROVAL,
                    reason="审批流程初始化",
                )
                storage.update_release(release_id, {"state": ReleaseState.PENDING_APPROVAL})
            except ValueError as e:
                logger.warning(f"状态更新: {e}")

        all_approvals = storage.get_approvals(release_id)
        self._notify_current_stage(release, all_approvals)
        return all_approvals

    def _get_current_pending_stage(
        self, release: Dict[str, Any], approvals: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")

        if approval_mode == "parallel":
            pending = [a for a in approvals if a["status"] == ApprovalStatus.PENDING]
            return pending[0] if pending else None

        for stage in channel_cfg.get("stages", []):
            stage_key = stage["key"]
            stage_approvals = [a for a in approvals if a["stage_key"] == stage_key]
            if not stage_approvals:
                continue
            statuses = [a["status"] for a in stage_approvals]
            if ApprovalStatus.REJECTED in statuses:
                return None
            if ApprovalStatus.PENDING in statuses:
                return stage_approvals[0]
            if all(s in (ApprovalStatus.APPROVED, ApprovalStatus.SKIPPED) for s in statuses):
                continue
        return None

    def _notify_current_stage(
        self, release: Dict[str, Any], approvals: List[Dict[str, Any]]
    ):
        current = self._get_current_pending_stage(release, approvals)
        if not current:
            return

        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")

        if approval_mode == "parallel":
            pending_approvals = [
                a for a in approvals if a["status"] == ApprovalStatus.PENDING
            ]
            for approval in pending_approvals:
                self._send_approval_notification(release, approval)
        else:
            self._send_approval_notification(release, current)

    def _send_approval_notification(
        self, release: Dict[str, Any], approval: Dict[str, Any]
    ):
        release_type_display = (
            "常规迭代" if release["release_type"] == "normal" else "紧急热修复"
        )
        timeout_hours = 8

        channel_cfg = self._get_channel_config(release["release_type"])
        for s in channel_cfg.get("stages", []):
            if s["key"] == approval["stage_key"]:
                timeout_hours = s.get("timeout_hours", 8)
                break

        deadline = (datetime.now() + timedelta(hours=timeout_hours)).strftime(
            "%Y-%m-%d %H:%M"
        )

        notifier.send_all(
            "approval_pending",
            {
                "release_id": release["id"],
                "release_type": release_type_display,
                "version": release["version"],
                "summary": release.get("summary", "") or "(无摘要)",
                "applicant": release["applicant"],
                "stage": approval["stage_name"],
                "role": approval["approver_role"],
                "timeout": f"{timeout_hours}小时",
                "deadline": deadline,
                "approval_url": f"https://release.example.com/approval/{release['id']}",
            },
            roles=[approval["approver_role"]],
            release_id=release["id"],
        )

    def _get_ordered_stage_keys(self, release_type: str) -> List[str]:
        channel_cfg = self._get_channel_config(release_type)
        stages = channel_cfg.get("stages", [])
        result = []
        for s in stages:
            if s.get("is_retroactive"):
                continue
            result.append(s["key"])
        return result

    def _find_awaiting_stage(
        self, release: Dict[str, Any], approvals: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")
        if approval_mode != "serial":
            return None

        for stage in channel_cfg.get("stages", []):
            if stage.get("is_retroactive"):
                continue
            stage_key = stage["key"]
            stage_approvals = [a for a in approvals if a["stage_key"] == stage_key]
            if not stage_approvals:
                continue
            statuses = [a["status"] for a in stage_approvals]
            if ApprovalStatus.REJECTED in statuses:
                return {"stage_key": stage_key, "stage_name": stage.get("name", stage_key), "status": "rejected"}
            if ApprovalStatus.PENDING in statuses:
                return {"stage_key": stage_key, "stage_name": stage.get("name", stage_key), "status": "pending"}
            if all(s in (ApprovalStatus.APPROVED, ApprovalStatus.SKIPPED) for s in statuses):
                continue
        return None

    def approve(
        self,
        release_id: str,
        stage_key: str,
        approver: str,
        comment: str = "",
        approver_role: str = "",
    ) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        passed, msg = self._check_pre_check_passed(release)
        if not passed:
            logger.error(f"审批操作被阻断: {msg}")
            return {"success": False, "error": msg}

        approvals = storage.get_approvals(release_id)
        if not approvals:
            return {
                "success": False,
                "error": f"发布单 {release_id} 尚未初始化审批流程，请先执行审批初始化",
            }

        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")

        any_rejected = any(a["status"] == ApprovalStatus.REJECTED for a in approvals)
        if any_rejected:
            rejected_info = [
                f"{a['stage_name']}({a['approver_role']})"
                for a in approvals if a["status"] == ApprovalStatus.REJECTED
            ]
            return {
                "success": False,
                "error": f"发布单已存在驳回节点（{', '.join(rejected_info)}），后续审批不可继续",
            }

        if approval_mode == "serial":
            awaiting = self._find_awaiting_stage(release, approvals)
            if awaiting and awaiting["stage_key"] != stage_key:
                return {
                    "success": False,
                    "error": (
                        f"当前应先处理「{awaiting['stage_name']}」阶段的审批，"
                        f"不可跳过至「{stage_key}」。"
                        f"审批顺序: {' → '.join(self._get_ordered_stage_keys(release['release_type']))}"
                    ),
                }

        target = None
        for a in approvals:
            if (
                a["stage_key"] == stage_key
                and a["status"] == ApprovalStatus.PENDING
            ):
                if approver_role and a["approver_role"] != approver_role:
                    continue
                target = a
                break

        if not target:
            pending = [
                f"{a['stage_name']}({a['approver_role']}:{a['status']})"
                for a in approvals
            ]
            return {
                "success": False,
                "error": f"未找到可审批的节点，当前状态: {pending}",
            }

        storage.update_approval(
            target["id"], ApprovalStatus.APPROVED, approver, comment
        )
        logger.info(
            f"审批通过: {release_id} / {target['stage_name']} "
            f"/ {target['approver_role']} / 操作人: {approver}"
        )

        approvals = storage.get_approvals(release_id)
        all_approved = self._check_all_approved(release, approvals)
        any_rejected = any(a["status"] == ApprovalStatus.REJECTED for a in approvals)

        if any_rejected:
            self._handle_rejection(release, approvals, approver, comment)
            return {"success": True, "result": "rejected"}

        if all_approved:
            self._handle_full_approval(release_id, release)
            return {"success": True, "result": "all_approved"}

        self._notify_current_stage(release, approvals)
        return {"success": True, "result": "next_stage"}

    def reject(
        self,
        release_id: str,
        stage_key: str,
        approver: str,
        comment: str = "",
    ) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        passed, msg = self._check_pre_check_passed(release)
        if not passed:
            return {"success": False, "error": msg}

        approvals = storage.get_approvals(release_id)
        if not approvals:
            return {
                "success": False,
                "error": f"发布单 {release_id} 尚未初始化审批流程",
            }

        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")

        if approval_mode == "serial":
            awaiting = self._find_awaiting_stage(release, approvals)
            if awaiting and awaiting["stage_key"] != stage_key:
                return {
                    "success": False,
                    "error": (
                        f"当前应先处理「{awaiting['stage_name']}」阶段的审批，"
                        f"不可跳过至「{stage_key}」"
                    ),
                }

        target = None
        for a in approvals:
            if (
                a["stage_key"] == stage_key
                and a["status"] == ApprovalStatus.PENDING
            ):
                target = a
                break

        if not target:
            return {"success": False, "error": "未找到可审批的节点"}

        storage.update_approval(
            target["id"], ApprovalStatus.REJECTED, approver, comment
        )
        logger.warning(
            f"审批驳回: {release_id} / {target['stage_name']} / 操作人: {approver}, "
            f"原因: {comment}"
        )

        self._handle_rejection(release, approvals, approver, comment)
        return {"success": True, "result": "rejected"}

    def _check_all_approved(
        self, release: Dict[str, Any], approvals: List[Dict[str, Any]]
    ) -> bool:
        channel_cfg = self._get_channel_config(release["release_type"])
        approval_mode = channel_cfg.get("approval_mode", "serial")
        approval_rule = "all"

        for stage in channel_cfg.get("stages", []):
            if stage.get("is_retroactive"):
                continue
            stage_approvals = [
                a for a in approvals if a["stage_key"] == stage["key"]
            ]
            if not stage_approvals:
                continue
            if "approval_rule" in stage:
                approval_rule = stage["approval_rule"]

            statuses = [a["status"] for a in stage_approvals]
            if approval_mode == "parallel" and approval_rule == "any":
                if any(s == ApprovalStatus.APPROVED for s in statuses):
                    return True
            else:
                if not all(
                    s in (ApprovalStatus.APPROVED, ApprovalStatus.SKIPPED)
                    for s in statuses
                ):
                    return False
        return True

    def _handle_rejection(
        self,
        release: Dict[str, Any],
        approvals: List[Dict[str, Any]],
        rejector: str,
        comment: str,
    ):
        try:
            self.state_machine.transition(
                release["id"],
                release["state"],
                ReleaseState.REJECTED,
                operator=rejector,
                reason=comment,
            )
            storage.update_release(release["id"], {"state": ReleaseState.REJECTED})
        except ValueError as e:
            logger.warning(f"状态更新跳过: {e}")

        notifier.send_all(
            "pre_check_result",
            {
                "release_id": release["id"],
                "version": release["version"],
                "result": f"❌ **审批驳回**（{rejector}）",
                "score": 0,
                "metrics_detail": f"> 驳回意见：{comment}",
                "fix_suggestions_section": "",
            },
            roles=[release["applicant"]],
            release_id=release["id"],
        )

    def _handle_full_approval(self, release_id: str, release: Dict[str, Any]):
        logger.info(f"全部审批通过: {release_id}")
        latest = storage.get_release(release_id) or release
        current_state = latest.get("state", release.get("state"))
        try:
            self.state_machine.transition(
                release_id,
                current_state,
                ReleaseState.APPROVED,
                reason="全部审批节点已通过",
            )
            storage.update_release(
                release_id,
                {"state": ReleaseState.READY_FOR_DEPLOY},
            )
        except ValueError as e:
            logger.warning(f"状态更新跳过: {e}")
            storage.update_release(
                release_id,
                {"state": ReleaseState.READY_FOR_DEPLOY},
            )

        notifier.send_all(
            "pre_check_result",
            {
                "release_id": release_id,
                "version": release["version"],
                "result": "✅ **全部审批通过，可进入发布阶段**",
                "score": 100,
                "metrics_detail": "> 所有审批节点已完成，发布已就绪",
                "fix_suggestions_section": "",
            },
            roles=[release["applicant"]],
            release_id=release_id,
        )

    def check_timeouts(self) -> List[Dict[str, Any]]:
        timeout_approvals = []
        releases = storage.list_releases(state=ReleaseState.PENDING_APPROVAL)

        for release in releases:
            approvals = storage.get_approvals(release["id"])
            channel_cfg = self._get_channel_config(release["release_type"])

            for stage in channel_cfg.get("stages", []):
                if stage.get("is_retroactive"):
                    continue

                timeout_h = stage.get("timeout_hours", 8)
                for a in approvals:
                    if (
                        a["stage_key"] == stage["key"]
                        and a["status"] == ApprovalStatus.PENDING
                    ):
                        created = datetime.fromisoformat(a["created_at"])
                        elapsed = (datetime.now() - created).total_seconds() / 3600
                        if elapsed >= timeout_h:
                            timeout_approvals.append(
                                {"release": release, "approval": a, "stage": stage}
                            )
                            escalation_role = stage.get("escalation_role")
                            if escalation_role:
                                logger.warning(
                                    f"审批超时升级: {release['id']} / {a['stage_name']} "
                                    f"→ {escalation_role}"
                                )
                                notifier.send_all(
                                    "approval_pending",
                                    {
                                        "release_id": release["id"],
                                        "release_type": release["release_type"],
                                        "version": release["version"],
                                        "summary": release.get("summary", ""),
                                        "applicant": release["applicant"],
                                        "stage": f"{a['stage_name']}（超时升级）",
                                        "role": escalation_role,
                                        "timeout": f"原审批已超时{round(elapsed - timeout_h, 1)}小时",
                                        "deadline": datetime.now().strftime(
                                            "%Y-%m-%d %H:%M"
                                        ),
                                        "approval_url": f"https://release.example.com/approval/{release['id']}",
                                    },
                                    roles=[escalation_role],
                                    release_id=release["id"],
                                )

        return timeout_approvals

    def get_status(self, release_id: str) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        approvals = storage.get_approvals(release_id)
        channel_cfg = self._get_channel_config(release["release_type"])
        current = self._get_current_pending_stage(release, approvals)

        return {
            "success": True,
            "release": {
                "id": release["id"],
                "version": release["version"],
                "release_type": release["release_type"],
                "state": release["state"],
                "applicant": release["applicant"],
            },
            "approval_mode": channel_cfg.get("approval_mode"),
            "current_stage": (
                {
                    "stage_key": current["stage_key"],
                    "stage_name": current["stage_name"],
                    "approver_role": current["approver_role"],
                    "status": current["status"],
                }
                if current
                else None
            ),
            "approvals": [
                {
                    "id": a["id"],
                    "stage_key": a["stage_key"],
                    "stage_name": a["stage_name"],
                    "approver_role": a["approver_role"],
                    "approver": a.get("approver"),
                    "status": a["status"],
                    "comment": a.get("comment"),
                    "created_at": a["created_at"],
                    "approved_at": a.get("approved_at"),
                }
                for a in approvals
            ],
        }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="审批流转管理")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="初始化审批流程")
    p_init.add_argument("--release-id", required=True)

    p_status = sub.add_parser("status", help="查看审批状态")
    p_status.add_argument("--release-id", required=True)

    p_approve = sub.add_parser("approve", help="审批通过")
    p_approve.add_argument("--release-id", required=True)
    p_approve.add_argument("--stage-key", required=True)
    p_approve.add_argument("--approver", required=True)
    p_approve.add_argument("--role", default="")
    p_approve.add_argument("--comment", default="")

    p_reject = sub.add_parser("reject", help="审批驳回")
    p_reject.add_argument("--release-id", required=True)
    p_reject.add_argument("--stage-key", required=True)
    p_reject.add_argument("--approver", required=True)
    p_reject.add_argument("--comment", default="")

    p_timeout = sub.add_parser("check-timeouts", help="检查审批超时")

    args = parser.parse_args()
    engine = ApprovalFlowEngine()

    if args.cmd == "init":
        engine.init_approvals(args.release_id)
    elif args.cmd == "status":
        import json
        print(json.dumps(engine.get_status(args.release_id), ensure_ascii=False, indent=2))
    elif args.cmd == "approve":
        result = engine.approve(
            args.release_id, args.stage_key, args.approver, args.comment, args.role
        )
        print(result)
    elif args.cmd == "reject":
        result = engine.reject(
            args.release_id, args.stage_key, args.approver, args.comment
        )
        print(result)
    elif args.cmd == "check-timeouts":
        result = engine.check_timeouts()
        print(f"超时审批数量: {len(result)}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
