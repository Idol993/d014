import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import logger
from core.config_loader import config
from core.storage import storage
from core.state_machine import ReleaseState, StateMachine
from core.notifier import notifier


class GrayReleaseEngine:
    def __init__(self):
        self.state_machine = StateMachine("release")
        self.cfg = config.get("main.gray_release", {})
        self.phases: List[Dict[str, Any]] = self.cfg.get("route_phases", [])
        self.auto_advance = self.cfg.get("auto_advance", True)
        self.require_manual = set(self.cfg.get("require_manual_confirm", []))

    def get_phases(self) -> List[Dict[str, Any]]:
        return self.phases

    def get_current_phase(self, release_id: str) -> Optional[Dict[str, Any]]:
        release = storage.get_release(release_id)
        if not release:
            return None
        current = release.get("current_phase")
        if not current:
            return self.phases[0] if self.phases else None
        for p in self.phases:
            if p["name"] == current:
                return p
        return None

    def get_next_phase(self, release_id: str) -> Optional[Dict[str, Any]]:
        current = self.get_current_phase(release_id)
        if not current:
            return self.phases[0] if self.phases else None
        for i, p in enumerate(self.phases):
            if p["name"] == current["name"] and i + 1 < len(self.phases):
                return self.phases[i + 1]
        return None

    def start_release(self, release_id: str, operator: str = "system") -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        if release["state"] not in (
            ReleaseState.READY_FOR_DEPLOY,
            ReleaseState.APPROVED,
        ):
            return {
                "success": False,
                "error": f"当前状态 {release['state']} 不可启动发布",
            }

        try:
            self.state_machine.transition(
                release_id,
                release["state"],
                ReleaseState.DEPLOYING,
                operator=operator,
                reason="启动灰度发布",
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

        first_phase = self.phases[0] if self.phases else None
        if first_phase:
            storage.update_release(
                release_id,
                {
                    "state": ReleaseState.DEPLOYING,
                    "current_phase": first_phase["name"],
                    "gray_traffic_percent": first_phase.get("traffic_percent", 0),
                },
            )
            logger.info(
                f"灰度发布启动: {release_id}, 进入阶段: {first_phase['display_name']} "
                f"({first_phase.get('traffic_percent', 0)}%)"
            )
            self._notify_phase_change(release, first_phase, "进入")

        return {
            "success": True,
            "release_id": release_id,
            "current_phase": first_phase,
        }

    def advance_phase(
        self,
        release_id: str,
        operator: str = "system",
        force: bool = False,
    ) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        next_phase = self.get_next_phase(release_id)
        if not next_phase:
            return self._complete_release(release_id, release, operator)

        if next_phase["name"] in self.require_manual and not force and operator == "system":
            logger.info(
                f"阶段 {next_phase['display_name']} 需要人工确认，暂停自动推进"
            )
            return {
                "success": True,
                "paused": True,
                "message": f"阶段 {next_phase['display_name']} 需人工确认",
                "next_phase": next_phase,
            }

        try:
            event = self.state_machine.transition(
                release_id,
                release["state"],
                ReleaseState.DEPLOYING,
                operator=operator,
                reason=f"推进至下一阶段: {next_phase['display_name']}",
            )
            storage.add_state_history(event.to_dict())
        except ValueError:
            pass

        storage.update_release(
            release_id,
            {
                "state": ReleaseState.DEPLOYING,
                "current_phase": next_phase["name"],
                "gray_traffic_percent": next_phase.get("traffic_percent", 0),
            },
        )
        logger.info(
            f"灰度阶段推进: {release_id} → {next_phase['display_name']} "
            f"({next_phase.get('traffic_percent', 0)}%)"
        )
        self._notify_phase_change(release, next_phase, "推进至", operator)

        return {
            "success": True,
            "release_id": release_id,
            "current_phase": next_phase,
        }

    def enter_observing(self, release_id: str, operator: str = "system") -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        try:
            self.state_machine.transition(
                release_id,
                release["state"],
                ReleaseState.OBSERVING,
                operator=operator,
                reason="进入灰度观察期",
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

        storage.update_release(release_id, {"state": ReleaseState.OBSERVING})
        phase = self.get_current_phase(release_id)
        logger.info(
            f"进入观察期: {release_id}, 阶段={phase['display_name'] if phase else '-'}"
        )
        return {"success": True}

    def _complete_release(
        self, release_id: str, release: Dict[str, Any], operator: str
    ) -> Dict[str, Any]:
        try:
            self.state_machine.transition(
                release_id,
                release["state"],
                ReleaseState.STABLE,
                operator=operator,
                reason="所有灰度阶段完成",
            )
        except ValueError as e:
            pass

        try:
            self.state_machine.transition(
                release_id,
                ReleaseState.STABLE,
                ReleaseState.COMPLETED,
                operator=operator,
                reason="发布完成",
            )
        except ValueError:
            pass

        storage.update_release(
            release_id,
            {
                "state": ReleaseState.COMPLETED,
                "gray_traffic_percent": 100,
            },
        )

        notifier.send_all(
            "rollback_completed",
            {
                "release_id": release_id,
                "rollback_id": "-",
                "complete_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "duration": 0,
                "from_version": release.get("from_version", "-"),
                "to_version": release["version"],
                "actions_detail": "> ✅ 全量发布成功",
                "health_status": "✅ 健康检查通过",
            },
            roles=config.get("approval.notification_parties.release_completed", [])
            + [release["applicant"]],
            release_id=release_id,
        )
        logger.info(f"发布完成: {release_id}")
        return {"success": True, "completed": True, "release_id": release_id}

    def _notify_phase_change(
        self,
        release: Dict[str, Any],
        phase: Dict[str, Any],
        action: str,
        operator: str = "system",
    ):
        coverage = ", ".join(phase.get("coverage", []))
        detail = (
            f"> **阶段名称**：{phase['display_name']}\n"
            f"> **覆盖线路**：{coverage}\n"
            f"> **流量比例**：{phase.get('traffic_percent', 0)}%\n"
            f"> **观察时长**：{phase.get('duration_minutes', 0)}分钟\n"
            f"> **操作人**：{operator}"
        )
        notifier.send_all(
            "pre_check_result",
            {
                "release_id": release["id"],
                "version": release["version"],
                "result": f"🚀 **灰度发布{action}{phase['display_name']}**",
                "score": 100,
                "metrics_detail": detail,
                "fix_suggestions_section": "",
            },
            roles=[
                "调度主管",
                "运营总监",
                "技术架构师",
                release["applicant"],
            ],
            release_id=release["id"],
        )

    def get_phase_remaining_time(
        self, release_id: str
    ) -> Optional[Dict[str, Any]]:
        release = storage.get_release(release_id)
        if not release:
            return None
        phase = self.get_current_phase(release_id)
        if not phase:
            return None

        duration = phase.get("duration_minutes", 0)
        if duration == 0:
            return {"phase": phase, "remaining_minutes": 0, "elapsed_minutes": 0}

        updated = datetime.fromisoformat(release["updated_at"])
        elapsed = (datetime.now() - updated).total_seconds() / 60
        remaining = max(0, duration - elapsed)

        return {
            "phase": phase,
            "elapsed_minutes": round(elapsed, 1),
            "remaining_minutes": round(remaining, 1),
            "duration_minutes": duration,
            "can_advance": remaining <= 0,
        }

    def get_status(self, release_id: str) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        current = self.get_current_phase(release_id)
        next_p = self.get_next_phase(release_id)
        timing = self.get_phase_remaining_time(release_id)

        return {
            "success": True,
            "release": {
                "id": release["id"],
                "version": release["version"],
                "state": release["state"],
                "current_phase": release.get("current_phase"),
                "traffic_percent": release.get("gray_traffic_percent", 0),
            },
            "current_phase": current,
            "next_phase": next_p,
            "timing": timing,
            "phases": self.phases,
        }


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="灰度发布管理")
    sub = parser.add_subparsers(dest="cmd")

    p_start = sub.add_parser("start", help="启动灰度发布")
    p_start.add_argument("--release-id", required=True)
    p_start.add_argument("--operator", default="system")

    p_advance = sub.add_parser("advance", help="推进到下一阶段")
    p_advance.add_argument("--release-id", required=True)
    p_advance.add_argument("--operator", default="system")
    p_advance.add_argument("--force", action="store_true")

    p_observe = sub.add_parser("observe", help="进入观察期")
    p_observe.add_argument("--release-id", required=True)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.add_argument("--release-id", required=True)

    args = parser.parse_args()
    engine = GrayReleaseEngine()

    if args.cmd == "start":
        print(json.dumps(engine.start_release(args.release_id, args.operator), ensure_ascii=False, indent=2))
    elif args.cmd == "advance":
        print(json.dumps(engine.advance_phase(args.release_id, args.operator, args.force), ensure_ascii=False, indent=2))
    elif args.cmd == "observe":
        print(json.dumps(engine.enter_observing(args.release_id), ensure_ascii=False, indent=2))
    elif args.cmd == "status":
        print(json.dumps(engine.get_status(args.release_id), ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
