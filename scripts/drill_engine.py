import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import REPORT_DIR, logger
from core.config_loader import config
from core.storage import storage
from core.notifier import notifier


class DrillType:
    ROLLBACK = "rollback"
    FULL_RELEASE = "full_release"
    FAULT_INJECTION = "fault_injection"
    HOTFIX = "hotfix"


DRILL_DEFINITIONS = {
    DrillType.ROLLBACK: {
        "name": "回滚流程演练",
        "description": "验证回滚自动化流程有效性",
        "frequency": "每月1次",
        "estimated_minutes": 30,
        "steps": [
            {"key": "env_init", "name": "演练环境初始化", "timeout": 5},
            {"key": "deploy_new", "name": "部署模拟新版本", "timeout": 5},
            {"key": "traffic_gray", "name": "切量至灰度版本", "timeout": 3},
            {"key": "inject_fault", "name": "注入故障触发告警", "timeout": 3},
            {"key": "verify_circuit", "name": "验证熔断机制触发", "timeout": 5},
            {"key": "verify_rollback", "name": "验证自动回滚执行", "timeout": 10},
            {"key": "verify_restore", "name": "验证业务指标恢复", "timeout": 3},
            {"key": "cleanup", "name": "演练环境清理", "timeout": 2},
        ],
    },
    DrillType.FULL_RELEASE: {
        "name": "全链路发布演练",
        "description": "模拟完整发布流程（含审批+灰度）",
        "frequency": "每季度1次",
        "estimated_minutes": 60,
        "steps": [
            {"key": "env_init", "name": "演练环境初始化", "timeout": 5},
            {"key": "create_release", "name": "创建模拟发布单", "timeout": 2},
            {"key": "pre_check", "name": "执行前置校验", "timeout": 5},
            {"key": "approval_1", "name": "调度审批", "timeout": 3},
            {"key": "approval_2", "name": "运营审批", "timeout": 3},
            {"key": "approval_3", "name": "安全审批", "timeout": 3},
            {"key": "approval_4", "name": "技术审批", "timeout": 3},
            {"key": "gray_phase_1", "name": "灰度阶段1（支线）", "timeout": 5},
            {"key": "gray_phase_2", "name": "灰度阶段2（区域干线）", "timeout": 5},
            {"key": "gray_phase_3", "name": "灰度阶段3（全国干线）", "timeout": 5},
            {"key": "full_release", "name": "全量发布", "timeout": 3},
            {"key": "verify_stable", "name": "验证发布稳定", "timeout": 5},
            {"key": "cleanup", "name": "演练环境清理", "timeout": 3},
        ],
    },
    DrillType.FAULT_INJECTION: {
        "name": "故障注入演练",
        "description": "在灰度环境注入特定故障验证熔断",
        "frequency": "每季度1次",
        "estimated_minutes": 45,
        "steps": [
            {"key": "env_init", "name": "演练环境初始化", "timeout": 5},
            {"key": "deploy_gray", "name": "部署至灰度环境", "timeout": 5},
            {"key": "inject_dispatch_fail", "name": "注入调度失败故障", "timeout": 3},
            {"key": "verify_alarm_1", "name": "验证调度失败率告警", "timeout": 5},
            {"key": "recover_1", "name": "恢复调度故障", "timeout": 3},
            {"key": "inject_offline", "name": "注入车辆离线故障", "timeout": 3},
            {"key": "verify_alarm_2", "name": "验证离线率告警", "timeout": 5},
            {"key": "recover_2", "name": "恢复离线故障", "timeout": 3},
            {"key": "inject_freight_error", "name": "注入运费计算异常", "timeout": 3},
            {"key": "verify_alarm_3", "name": "验证运费异常告警", "timeout": 5},
            {"key": "full_recover", "name": "全面恢复", "timeout": 3},
            {"key": "cleanup", "name": "演练环境清理", "timeout": 2},
        ],
    },
    DrillType.HOTFIX: {
        "name": "紧急发布演练",
        "description": "验证Hotfix通道审批与发布时效",
        "frequency": "每半年1次",
        "estimated_minutes": 45,
        "steps": [
            {"key": "env_init", "name": "演练环境初始化", "timeout": 5},
            {"key": "create_hotfix", "name": "创建紧急热修复单", "timeout": 2},
            {"key": "parallel_approval", "name": "并行审批通道", "timeout": 10},
            {"key": "rapid_deploy", "name": "快速部署", "timeout": 5},
            {"key": "verify_fix", "name": "验证修复效果", "timeout": 10},
            {"key": "retroactive_sign", "name": "事后补签流程", "timeout": 5},
            {"key": "cleanup", "name": "演练环境清理", "timeout": 2},
        ],
    },
}


class DrillEngine:
    def __init__(self):
        self.cfg = config.get("main.drill", {})

    def _generate_drill_id(self, drill_type: str) -> str:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = drill_type.upper()[:3]
        return f"DRILL-{prefix}-{ts}"

    def list_drill_types(self) -> Dict[str, Any]:
        return {k: {"name": v["name"], "description": v["description"],
                    "frequency": v["frequency"], "estimated_minutes": v["estimated_minutes"],
                    "steps_count": len(v["steps"])}
                for k, v in DRILL_DEFINITIONS.items()}

    def run_drill(
        self,
        drill_type: str,
        name: str = "",
        operator: str = "system",
        simulate_failures: bool = True,
    ) -> Dict[str, Any]:
        definition = DRILL_DEFINITIONS.get(drill_type)
        if not definition:
            return {"success": False, "error": f"未知演练类型: {drill_type}"}

        drill_id = self._generate_drill_id(drill_type)
        drill_name = name or definition["name"]

        logger.info(
            f"========== 开始演练: {drill_name} ({drill_id}) =========="
        )
        logger.info(f"演练类型: {drill_type}, 预计耗时: {definition['estimated_minutes']}分钟")

        storage.create_drill(
            {
                "id": drill_id,
                "drill_type": drill_type,
                "name": drill_name,
                "status": "RUNNING",
                "started_at": datetime.now().isoformat(),
            }
        )

        step_results: List[Dict[str, Any]] = []
        issues: List[Dict[str, Any]] = []
        improvements: List[Dict[str, Any]] = []
        all_passed = True

        for step in definition["steps"]:
            result = self._execute_step(
                drill_id, step, simulate_failures, issues, improvements
            )
            step_results.append(result)
            if not result["passed"]:
                all_passed = False
                logger.warning(f"  ❌ {step['name']}: {result.get('message','失败')}")
            else:
                logger.info(f"  ✅ {step['name']}: {result.get('message','通过')} ({result['duration_seconds']}s)")

        total_duration = sum(s["duration_seconds"] for s in step_results)
        status = "PASSED" if all_passed else "FAILED"

        logger.info(
            f"========== 演练完成: {drill_name}, 结果={status}, "
            f"耗时={round(total_duration/60, 1)}分钟 =========="
        )

        summary = {
            "drill_id": drill_id,
            "drill_type": drill_type,
            "drill_name": drill_name,
            "total_steps": len(step_results),
            "passed_steps": sum(1 for s in step_results if s["passed"]),
            "failed_steps": sum(1 for s in step_results if not s["passed"]),
            "total_duration_seconds": total_duration,
            "status": status,
            "steps": step_results,
        }

        report_path = self._save_report(
            drill_id, drill_type, drill_name, summary, issues, improvements
        )

        storage.update_drill(
            drill_id,
            {
                "status": status,
                "completed_at": datetime.now().isoformat(),
                "duration_minutes": round(total_duration / 60, 1),
                "result_summary": summary,
                "issues": issues,
                "improvements": improvements,
                "report_path": report_path,
            },
        )

        self._notify(drill_id, drill_name, drill_type, status,
                     total_duration, issues, improvements)

        return {
            "success": True,
            "drill_id": drill_id,
            "status": status,
            "duration_minutes": round(total_duration / 60, 1),
            "issues": issues,
            "improvements": improvements,
            "report_path": report_path,
        }

    def _execute_step(
        self,
        drill_id: str,
        step: Dict[str, Any],
        simulate_failures: bool,
        issues: List[Dict[str, Any]],
        improvements: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        start = time.time()
        timeout = step.get("timeout", 5)
        simulated_fail_rate = 0.15 if simulate_failures else 0.0

        time.sleep(min(0.8, timeout * 0.05))

        passed = random.random() >= simulated_fail_rate
        duration = round(time.time() - start + random.uniform(0.5, min(3.0, timeout * 0.3)), 2)

        result = {
            "step_key": step["key"],
            "step_name": step["name"],
            "passed": passed,
            "duration_seconds": duration,
            "timeout_seconds": timeout,
            "message": "执行通过" if passed else "执行异常",
        }

        if not passed:
            issue = {
                "step": step["name"],
                "severity": random.choice(["low", "medium", "high"]),
                "description": f"{step['name']}环节模拟故障",
                "root_cause": random.choice([
                    "自动化脚本参数配置偏差",
                    "外部依赖接口响应超时",
                    "状态机流转分支覆盖不全",
                    "告警通知渠道偶发失败",
                ]),
            }
            issues.append(issue)
            improvements.append(
                {
                    "related_issue": step["name"],
                    "action": f"优化{step['name']}环节的容错与重试机制",
                    "owner": "技术团队",
                    "priority": "high" if issue["severity"] == "high" else "medium",
                }
            )
            result["message"] = issue["root_cause"]

        return result

    def _save_report(
        self,
        drill_id: str,
        drill_type: str,
        drill_name: str,
        summary: Dict[str, Any],
        issues: List[Dict[str, Any]],
        improvements: List[Dict[str, Any]],
    ) -> str:
        report = {
            "drill_id": drill_id,
            "drill_type": drill_type,
            "drill_name": drill_name,
            "generated_at": datetime.now().isoformat(),
            "summary": summary,
            "issues": issues,
            "improvements": improvements,
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{drill_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def _notify(
        self,
        drill_id: str,
        drill_name: str,
        drill_type: str,
        status: str,
        duration_seconds: int,
        issues: List[Dict[str, Any]],
        improvements: List[Dict[str, Any]],
    ):
        issues_lines = []
        for i, issue in enumerate(issues, 1):
            sev = {"low": "低", "medium": "中", "high": "高"}[issue["severity"]]
            issues_lines.append(
                f"> {i}. [{sev}风险] {issue['step']}: {issue['description']}（原因：{issue['root_cause']}）"
            )
        issues_detail = "\n".join(issues_lines) if issues_lines else "> 无问题"

        improvements_lines = []
        for i, imp in enumerate(improvements, 1):
            p = {"high": "高", "medium": "中", "low": "低"}[imp.get("priority", "medium")]
            improvements_lines.append(
                f"> {i}. [{p}优先级] {imp['action']}（责任人：{imp['owner']}）"
            )
        improvements_detail = "\n".join(improvements_lines) if improvements_lines else "> 无需改进"

        result_display = "✅ **通过**" if status == "PASSED" else "❌ **未通过**"

        notifier.send_all(
            "drill_completed",
            {
                "drill_id": drill_id,
                "drill_type": drill_name,
                "result": result_display,
                "duration": round(duration_seconds / 60, 1),
                "issues_detail": issues_detail,
                "improvements_detail": improvements_detail,
            },
            roles=["技术架构师", "调度主管", "运营总监"],
            drill_id=drill_id,
        )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="演练编排引擎")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出可用演练类型")

    p_run = sub.add_parser("run", help="执行演练")
    p_run.add_argument(
        "--type",
        required=True,
        choices=list(DRILL_DEFINITIONS.keys()),
        help="演练类型",
    )
    p_run.add_argument("--name", default="")
    p_run.add_argument("--operator", default="system")
    p_run.add_argument("--no-failures", action="store_true", help="不模拟故障")

    args = parser.parse_args()
    engine = DrillEngine()

    if args.cmd == "list":
        print(json.dumps(engine.list_drill_types(), ensure_ascii=False, indent=2))
    elif args.cmd == "run":
        result = engine.run_drill(
            args.type, args.name, args.operator, not args.no_failures
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
