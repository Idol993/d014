import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import REPORT_DIR, logger
from core.config_loader import config
from core.metrics_collector import metrics_collector
from core.storage import storage
from core.state_machine import ReleaseState, StateMachine
from core.notifier import notifier


class RollbackStatus:
    TRIGGERED = "TRIGGERED"
    TRAFFIC_SWITCHED = "TRAFFIC_SWITCHED"
    DEPLOYING_OLD = "DEPLOYING_OLD"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CircuitBreakerEngine:
    def __init__(self):
        self.state_machine = StateMachine("release")
        self.cfg = config.get("main.gray_release", {})
        self.rollback_cfg = config.get("main.rollback", {})
        self.thresholds_cfg = config.get("thresholds.runtime_metrics", {})
        self.consecutive_window_threshold = self.cfg.get(
            "consecutive_window_threshold", 3
        )

    def _check_metric_breach(
        self, metric_key: str, value: float
    ) -> Tuple[bool, str]:
        cfg = self.thresholds_cfg.get(metric_key)
        if not cfg:
            return False, ""

        circuit = cfg["circuit_breaker_threshold"]
        breached = value >= circuit
        status = (
            f"🔴 值={value}{cfg.get('unit','')}，熔断阈值={circuit}{cfg.get('unit','')}"
            if breached
            else f"✅ 值={value}{cfg.get('unit','')}，安全阈值={cfg['safe_threshold']}{cfg.get('unit','')}"
        )
        return breached, status

    def monitor_once(self, release_id: str) -> Dict[str, Any]:
        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        if release["state"] in (
            ReleaseState.ROLLBACK,
            ReleaseState.ROLLED_BACK,
            ReleaseState.COMPLETED,
            ReleaseState.CLOSED,
        ):
            return {"success": True, "skipped": True, "reason": f"状态{release['state']}无需监控"}

        phase = release.get("current_phase", "unknown")
        window_start, window_end = metrics_collector.get_time_window(5)
        all_metrics = metrics_collector.collect_all_runtime_metrics()

        breaches = []
        for metric_key, metric_data in all_metrics.items():
            value = metric_data["value"]
            breached, status_text = self._check_metric_breach(metric_key, value)

            storage.add_metric_window(
                {
                    "release_id": release_id,
                    "phase": phase,
                    "metric_name": metric_data["name"],
                    "metric_value": value,
                    "threshold": metric_data["circuit_breaker_threshold"],
                    "is_breach": breached,
                    "window_start": window_start,
                    "window_end": window_end,
                }
            )

            recent_windows = storage.get_recent_metric_windows(
                release_id, metric_data["name"], self.consecutive_window_threshold + 2
            )
            consecutive_breaches = sum(
                1 for w in recent_windows if w["is_breach"]
            )

            log_msg = (
                f"[{release_id}] {metric_data['name']}: {status_text}"
                f"（连续超限={consecutive_breaches}/{self.consecutive_window_threshold}）"
            )
            if breached:
                logger.warning(log_msg)
            else:
                logger.debug(log_msg)

            if consecutive_breaches >= self.consecutive_window_threshold:
                breaches.append(
                    {
                        "metric_key": metric_key,
                        "metric_name": metric_data["name"],
                        "value": value,
                        "threshold": metric_data["circuit_breaker_threshold"],
                        "unit": metric_data.get("unit", ""),
                        "consecutive_windows": consecutive_breaches,
                    }
                )

        if breaches:
            worst = max(breaches, key=lambda b: b["consecutive_windows"])
            logger.critical(
                f"[{release_id}] 触发熔断! "
                f"{worst['metric_name']}={worst['value']}{worst['unit']}, "
                f"连续{worst['consecutive_windows']}个窗口超限"
            )
            return {
                "success": True,
                "triggered": True,
                "breaches": breaches,
                "trigger": worst,
                "all_metrics": all_metrics,
            }

        return {
            "success": True,
            "triggered": False,
            "all_metrics": all_metrics,
        }

    def monitor_loop(
        self, release_id: str, interval_seconds: int = 300, max_iterations: int = 0
    ):
        logger.info(
            f"启动熔断监控循环: {release_id}, 间隔={interval_seconds}s, "
            f"最大迭代={max_iterations or '无限'}"
        )
        iterations = 0
        while True:
            result = self.monitor_once(release_id)
            if result.get("triggered"):
                self.execute_circuit_breaker(release_id, result["trigger"])
                return
            if result.get("skipped"):
                logger.info(f"监控结束: {result.get('reason')}")
                return

            iterations += 1
            if max_iterations > 0 and iterations >= max_iterations:
                logger.info(f"达到最大迭代次数，监控结束")
                return
            time.sleep(interval_seconds)


class RollbackEngine:
    def __init__(self):
        self.state_machine = StateMachine("release")
        self.rollback_cfg = config.get("main.rollback", {})
        self.deploy_cfg = config.get("main.deployment", {})

    def _generate_rollback_id(self) -> str:
        return f"RB-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    def execute_circuit_breaker(
        self, release_id: str, trigger: Dict[str, Any]
    ) -> Dict[str, Any]:
        logger.info(f"========== 开始执行熔断回滚: {release_id} ==========")

        release = storage.get_release(release_id)
        if not release:
            return {"success": False, "error": "发布单不存在"}

        rollback_id = self._generate_rollback_id()
        trigger_time = datetime.now()

        actions: List[Dict[str, Any]] = []

        try:
            event = self.state_machine.transition(
                release_id,
                release["state"],
                ReleaseState.ROLLBACK,
                operator="circuit_breaker",
                reason=f"{trigger['metric_name']}={trigger['value']}{trigger.get('unit','')}, "
                f"连续{trigger['consecutive_windows']}个窗口超限",
            )
            storage.add_state_history(event.to_dict())
            storage.update_release(release_id, {"state": ReleaseState.ROLLBACK})
        except ValueError as e:
            logger.warning(f"状态机更新: {e}")

        rollback_data = {
            "id": rollback_id,
            "release_id": release_id,
            "trigger_metric": trigger["metric_name"],
            "trigger_value": trigger["value"],
            "trigger_threshold": trigger["threshold"],
            "consecutive_windows": trigger["consecutive_windows"],
            "trigger_time": trigger_time.isoformat(),
            "from_version": release["version"],
            "to_version": release.get("from_version") or "previous_stable",
            "status": RollbackStatus.TRIGGERED,
            "impact_scope": {
                "phase": release.get("current_phase"),
                "traffic_percent": release.get("gray_traffic_percent", 0),
            },
            "actions": actions,
        }
        storage.create_rollback(rollback_data)

        logger.info(f"[步骤1/6] 设置发布状态为熔断暂停")
        actions.append(
            self._record_action("设置熔断状态", "success", 2, "状态已更新为ROLLBACK")
        )

        logger.info(f"[步骤2/6] 切换流量回旧版本")
        switch_ok, switch_time = self._switch_traffic_to_old(release)
        actions.append(
            self._record_action(
                "流量切换",
                "success" if switch_ok else "failed",
                switch_time,
                "灰度流量切回旧版本" if switch_ok else "流量切换失败",
            )
        )

        logger.info(f"[步骤3/6] 执行版本回滚部署")
        deploy_ok, deploy_time = self._deploy_old_version(release)
        actions.append(
            self._record_action(
                "版本回滚部署",
                "success" if deploy_ok else "failed",
                deploy_time,
                "部署上一稳定版本",
            )
        )

        logger.info(f"[步骤4/6] 验证回滚后系统健康状态")
        verify_ok, verify_time = self._verify_health()
        actions.append(
            self._record_action(
                "健康验证",
                "success" if verify_ok else "failed",
                verify_time,
                "回滚后健康检查" if verify_ok else "健康检查未通过",
            )
        )

        complete_time = datetime.now()
        duration = int((complete_time - trigger_time).total_seconds())

        final_status = RollbackStatus.COMPLETED if (
            switch_ok and deploy_ok and verify_ok
        ) else RollbackStatus.FAILED

        impact_scope = self._calculate_impact(release, trigger, trigger_time, complete_time)

        report_path = self._generate_report(
            rollback_id, release, trigger, actions, impact_scope,
            trigger_time, complete_time, duration, final_status,
        )

        storage.update_rollback(
            rollback_id,
            {
                "complete_time": complete_time.isoformat(),
                "duration_seconds": duration,
                "impact_scope": impact_scope,
                "actions": actions,
                "status": final_status,
                "report_path": report_path,
            },
        )
        storage.update_release(
            release_id, {"state": ReleaseState.ROLLED_BACK}
        )
        try:
            event = self.state_machine.transition(
                release_id,
                ReleaseState.ROLLBACK,
                ReleaseState.ROLLED_BACK,
                operator="system",
                reason=f"回滚完成，耗时{duration}秒",
            )
            storage.add_state_history(event.to_dict())
        except ValueError:
            pass

        self._send_alert(
            rollback_id, release, trigger, actions, impact_scope,
            duration, final_status, report_path,
        )

        logger.info(
            f"========== 熔断回滚完成: {rollback_id}, 状态={final_status}, "
            f"耗时={duration}s =========="
        )
        return {
            "rollback_id": rollback_id,
            "status": final_status,
            "duration_seconds": duration,
            "report_path": report_path,
        }

    def _record_action(
        self, name: str, status: str, duration: int, detail: str = ""
    ) -> Dict[str, Any]:
        return {
            "action": name,
            "status": status,
            "duration": f"{duration}s",
            "detail": detail,
        }

    def _switch_traffic_to_old(self, release: Dict[str, Any]) -> Tuple[bool, int]:
        start = time.time()
        try:
            base = self.deploy_cfg.get("base_url", "")
            if base and "example.com" not in base:
                import requests
                requests.post(
                    f"{base}/api/traffic/reset",
                    json={"release_id": release["id"], "target": "old"},
                    timeout=30,
                )
            else:
                time.sleep(3)
                logger.info("  [模拟] 流量已切回旧版本")
            return True, int(time.time() - start)
        except Exception as e:
            logger.error(f"流量切换失败: {e}")
            return False, int(time.time() - start)

    def _deploy_old_version(self, release: Dict[str, Any]) -> Tuple[bool, int]:
        start = time.time()
        try:
            job = self.deploy_cfg.get("rollback_job", "")
            base = self.deploy_cfg.get("base_url", "")
            if job and base and "example.com" not in base:
                import requests
                from requests.auth import HTTPBasicAuth
                token = self.deploy_cfg.get("api_token", "")
                resp = requests.post(
                    f"{base}/job/{job}/build",
                    auth=HTTPBasicAuth("api", token),
                    params={
                        "RELEASE_ID": release["id"],
                        "TARGET_VERSION": release.get("from_version", "previous_stable"),
                    },
                    timeout=300,
                )
                resp.raise_for_status()
            else:
                wait = min(
                    30, self.rollback_cfg.get("max_rollback_wait_seconds", 300) // 10
                )
                time.sleep(wait)
                logger.info(f"  [模拟] 旧版本部署完成（等待{wait}s）")
            return True, int(time.time() - start)
        except Exception as e:
            logger.error(f"版本回滚部署失败: {e}")
            return False, int(time.time() - start)

    def _verify_health(self) -> Tuple[bool, int]:
        start = time.time()
        retries = self.rollback_cfg.get("rollback_verify_retries", 3)
        interval = self.rollback_cfg.get("rollback_verify_interval_seconds", 20)
        health_url = self.deploy_cfg.get("health_check_url", "")

        for i in range(1, retries + 1):
            try:
                if health_url and "example.com" not in health_url:
                    import requests
                    resp = requests.get(health_url, timeout=15)
                    ok = resp.status_code == 200
                else:
                    time.sleep(2)
                    ok = True

                if ok:
                    logger.info(f"  健康检查第{i}次: 通过")
                    return True, int(time.time() - start)
                logger.warning(f"  健康检查第{i}次: 未通过")
            except Exception as e:
                logger.warning(f"  健康检查第{i}次异常: {e}")

            if i < retries:
                time.sleep(interval)

        return False, int(time.time() - start)

    def _calculate_impact(
        self,
        release: Dict[str, Any],
        trigger: Dict[str, Any],
        trigger_time: datetime,
        complete_time: datetime,
    ) -> Dict[str, Any]:
        affected_minutes = max(
            1, int((complete_time - trigger_time).total_seconds() / 60)
        )
        traffic = release.get("gray_traffic_percent", 0)
        phase = release.get("current_phase", "")

        phase_cfg = config.get("main.gray_release.route_phases", [])
        coverage = []
        for p in phase_cfg:
            if p["name"] == phase:
                coverage = p.get("coverage", [])
                break

        estimated_orders = int(traffic * affected_minutes * 0.85)

        return {
            "affected_routes": coverage,
            "affected_traffic_percent": traffic,
            "estimated_impact_orders": estimated_orders,
            "affected_duration_minutes": affected_minutes,
            "trigger_phase": phase,
        }

    def _generate_report(
        self,
        rollback_id: str,
        release: Dict[str, Any],
        trigger: Dict[str, Any],
        actions: List[Dict[str, Any]],
        impact_scope: Dict[str, Any],
        trigger_time: datetime,
        complete_time: datetime,
        duration: int,
        status: str,
    ) -> str:
        report = {
            "rollback_id": rollback_id,
            "release_id": release["id"],
            "version": release["version"],
            "trigger_time": trigger_time.isoformat(),
            "rollback_complete_time": complete_time.isoformat(),
            "duration_seconds": duration,
            "status": status,
            "trigger_reason": {
                "metric": trigger["metric_name"],
                "current_value": f"{trigger['value']}{trigger.get('unit','')}",
                "threshold": f"{trigger['threshold']}{trigger.get('unit','')}",
                "consecutive_windows": trigger["consecutive_windows"],
                "description": (
                    f"{trigger['metric_name']}连续{trigger['consecutive_windows']}个"
                    f"采集窗口超过熔断阈值"
                ),
            },
            "impact_scope": impact_scope,
            "root_cause_hypothesis": [
                "代码变更引入的性能退化（需进一步排查）",
                "配置参数与生产环境不兼容（需核查）",
                "依赖服务级联故障（需协同排查）",
            ],
            "rollback_actions": actions,
        }

        report_path = REPORT_DIR / f"{rollback_id}.json"
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(f"回滚报告已生成: {report_path}")
        return str(report_path)

    def _send_alert(
        self,
        rollback_id: str,
        release: Dict[str, Any],
        trigger: Dict[str, Any],
        actions: List[Dict[str, Any]],
        impact_scope: Dict[str, Any],
        duration: int,
        status: str,
        report_path: str,
    ):
        actions_detail_lines = []
        for a in actions:
            icon = "✅" if a["status"] == "success" else "❌"
            actions_detail_lines.append(
                f"> {icon} **{a['action']}** ({a['duration']}) - {a.get('detail','')}"
            )
        actions_detail = "\n".join(actions_detail_lines)

        routes = ", ".join(impact_scope.get("affected_routes", [])) or "全部"
        parties = config.get(
            "approval.notification_parties.circuit_breaker",
            ["调度主管", "运营总监", "技术架构师"],
        )
        parties_display = "、".join(parties)

        notifier.send_all(
            "circuit_breaker",
            {
                "release_id": release["id"],
                "rollback_id": rollback_id,
                "trigger_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "metric": trigger["metric_name"],
                "current_value": f"{trigger['value']}{trigger.get('unit','')}",
                "threshold": f"{trigger['threshold']}{trigger.get('unit','')}",
                "consecutive_windows": trigger["consecutive_windows"],
                "routes": routes,
                "traffic": impact_scope.get("affected_traffic_percent", 0),
                "impact_orders": impact_scope.get("estimated_impact_orders", 0),
                "rollback_status": "✅ 已完成" if status == RollbackStatus.COMPLETED else "❌ 执行异常",
                "duration": duration,
                "parties": parties_display,
                "report_url": f"file:///{report_path}",
            },
            roles=parties,
            release_id=release["id"],
            rollback_id=rollback_id,
        )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="熔断监控与自动回滚")
    sub = parser.add_subparsers(dest="cmd")

    p_monitor = sub.add_parser("monitor", help="监控单次检查")
    p_monitor.add_argument("--release-id", required=True)

    p_loop = sub.add_parser("monitor-loop", help="持续监控循环")
    p_loop.add_argument("--release-id", required=True)
    p_loop.add_argument("--interval", type=int, default=300)
    p_loop.add_argument("--max-iterations", type=int, default=0)

    p_rollback = sub.add_parser("manual-rollback", help="手动触发回滚")
    p_rollback.add_argument("--release-id", required=True)
    p_rollback.add_argument("--reason", default="手动触发")

    args = parser.parse_args()

    if args.cmd == "monitor":
        cb = CircuitBreakerEngine()
        result = cb.monitor_once(args.release_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("triggered"):
            rb = RollbackEngine()
            rb.execute_circuit_breaker(args.release_id, result["trigger"])
    elif args.cmd == "monitor-loop":
        cb = CircuitBreakerEngine()
        cb.monitor_loop(args.release_id, args.interval, args.max_iterations)
    elif args.cmd == "manual-rollback":
        rb = RollbackEngine()
        trigger = {
            "metric_name": "手动触发",
            "value": 0,
            "threshold": 0,
            "unit": "",
            "consecutive_windows": 0,
        }
        result = rb.execute_circuit_breaker(args.release_id, trigger)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
