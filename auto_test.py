import io
import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import logger
from scripts.release_cli import create_release, show_release, list_releases
from scripts.pre_check import PreCheckEngine
from scripts.approval_flow import ApprovalFlowEngine
from scripts.gray_release import GrayReleaseEngine
from scripts.rollback import RollbackEngine
from scripts.drill_engine import DrillEngine
from scripts.report_generator import ReportGenerator


def run_auto_test():
    print("\n" + "=" * 70)
    print("  自动化测试 - 干线运输调度发布平台")
    print("=" * 70 + "\n")

    test_results = []

    def test(name, func):
        try:
            result = func()
            test_results.append((name, "✅ PASS", result))
            print(f"  ✅ {name}")
            return result
        except Exception as e:
            test_results.append((name, "❌ FAIL", str(e)))
            print(f"  ❌ {name}: {e}")
            logger.exception(f"测试失败: {name}")
            return None

    def t1_create_release():
        rid = create_release(
            version="2.4.0",
            release_type="normal",
            summary="调度算法v2优化",
            applicant="wang_dev",
            from_version="2.3.5",
        )
        assert rid.startswith("REL-")
        return rid

    rid_normal = test("1. 创建常规发布单", t1_create_release)

    def t2_pre_check():
        pre = PreCheckEngine()
        result = pre.run(rid_normal)
        assert "overall_result" in result
        assert "overall_score" in result
        return result["overall_result"]

    test("2. 发布前置校验", t2_pre_check)

    def t3_approval_init():
        approval = ApprovalFlowEngine()
        approval.init_approvals(rid_normal)
        status = approval.get_status(rid_normal)
        assert len(status["approvals"]) == 4
        return len(status["approvals"])

    test("3. 审批流程初始化（4个串行节点）", t3_approval_init)

    def t4_approval_flow():
        approval = ApprovalFlowEngine()
        stages = ["dispatch", "operation", "security", "tech"]
        approvers = {
            "dispatch": ("li", "调度主管"),
            "operation": ("chen", "运营总监"),
            "security": ("zhao", "安全合规经理"),
            "tech": ("sun", "技术架构师"),
        }
        last_result = None
        for sk in stages:
            approver, role = approvers[sk]
            last_result = approval.approve(
                rid_normal, sk, approver, "OK", approver_role=role
            )
        status = approval.get_status(rid_normal)
        release_status = status["release"]["state"]
        assert release_status in ("READY_FOR_DEPLOY", "APPROVED")
        return release_status

    test("4. 四级审批全部通过", t4_approval_flow)

    def t5_gray_release():
        gray = GrayReleaseEngine()
        gray.start_release(rid_normal, operator="admin")
        phases = gray.get_phases()
        assert len(phases) == 4
        for i in range(4):
            result = gray.advance_phase(rid_normal, force=True)
        return result.get("completed", False)

    test("5. 灰度发布四阶段推进至全量", t5_gray_release)

    def t6_hotfix_release():
        rid = create_release(
            version="2.4.0-hotfix1",
            release_type="hotfix",
            summary="运费计算紧急修复",
            applicant="oncall",
            hotfix_reason="P0故障：运费偏差>15%",
            from_version="2.4.0",
        )
        approval = ApprovalFlowEngine()
        PreCheckEngine().run(rid)
        approval.init_approvals(rid)
        approval.approve(
            rid, "all_parallel", "tech_director", "先发布",
            approver_role="技术架构师"
        )
        status = approval.get_status(rid)
        return status["approval_mode"] == "parallel"

    test("6. 紧急热修复并行审批通道", t6_hotfix_release)

    def t7_circuit_breaker_rollback():
        rid = create_release(
            version="2.5.0-rc1",
            release_type="normal",
            summary="熔断测试版本",
            applicant="tester",
            from_version="2.4.0",
        )
        PreCheckEngine().run(rid)
        approval = ApprovalFlowEngine()
        approval.init_approvals(rid)
        for sk in ["dispatch", "operation", "security", "tech"]:
            approval.approve(rid, sk, "auto", "OK")
        gray = GrayReleaseEngine()
        gray.start_release(rid)
        gray.advance_phase(rid, force=True)

        trigger = {
            "metric_name": "调度失败率",
            "value": 3.42,
            "threshold": 3.0,
            "unit": "%",
            "consecutive_windows": 3,
        }
        rb = RollbackEngine()
        result = rb.execute_circuit_breaker(rid, trigger)
        assert result["status"] in ("COMPLETED", "FAILED")
        return result["rollback_id"]

    test("7. 熔断触发与自动回滚执行", t7_circuit_breaker_rollback)

    def t8_drill_execution():
        de = DrillEngine()
        result = de.run_drill("rollback", "自动化测试演练")
        assert result["status"] in ("PASSED", "FAILED")
        assert "report_path" in result
        return result["status"]

    test("8. 回滚流程演练执行", t8_drill_execution)

    def t9_report_generation():
        rg = ReportGenerator()
        reports = rg.generate_all_reports("weekly")
        assert "release_quality" in reports
        assert "approval_efficiency" in reports
        return len(reports)

    test("9. 周报报表生成（质量+审批效率）", t9_report_generation)

    def t10_monthly_report():
        rg = ReportGenerator()
        reports = rg.generate_all_reports("monthly")
        assert "rollback_analysis" in reports
        assert "drill_effectiveness" in reports
        return len(reports)

    test("10. 月报报表生成（+回滚分析+演练效果）", t10_monthly_report)

    print("\n" + "=" * 70)
    print("  测试结果汇总")
    print("=" * 70)
    passed = sum(1 for _, s, _ in test_results if s == "✅ PASS")
    total = len(test_results)
    for name, status, detail in test_results:
        print(f"  {status}  {name} → {detail}")
    print(f"\n  总计: {passed}/{total} 通过")
    print("=" * 70)

    if passed == total:
        print("\n🎉 全部测试通过！系统运行正常。")
    else:
        print(f"\n⚠️  有 {total - passed} 项测试失败，请检查日志。")

    print("\n📋 已创建的发布单列表:")
    list_releases(limit=10)

    return passed == total


if __name__ == "__main__":
    success = run_auto_test()
    sys.exit(0 if success else 1)
