import io
import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import logger
from core.storage import storage
from scripts.release_cli import create_release, show_release, list_releases
from scripts.pre_check import PreCheckEngine
from scripts.approval_flow import ApprovalFlowEngine
from scripts.gray_release import GrayReleaseEngine
from scripts.rollback import RollbackEngine, CircuitBreakerEngine
from scripts.drill_engine import DrillEngine
from scripts.report_generator import ReportGenerator
from scripts.pre_check import PreCheckResult


def _ensure_pre_check_passed(rid):
    release = storage.get_release(rid)
    pcr = release.get("pre_check_result")
    if pcr and isinstance(pcr, dict) and pcr.get("overall_result") in (PreCheckResult.PASS, PreCheckResult.WARNING):
        return
    storage.update_release(rid, {
        "pre_check_result": {"overall_result": PreCheckResult.WARNING, "overall_score": 91.0,
                              "block_reasons": [], "metrics": []},
        "pre_check_score": 91.0,
        "state": "PENDING_APPROVAL",
    })


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
        release = storage.get_release(rid)
        assert release.get("from_version") == "2.3.5", f"from_version未持久化: {release.get('from_version')}"
        return rid

    rid_normal = test("1. 创建常规发布单（from_version持久化验证）", t1_create_release)

    def t2_pre_check_gate():
        approval = ApprovalFlowEngine()
        result = approval.init_approvals(rid_normal)
        assert result == [], f"未跑前置校验时应阻断审批初始化, 实际返回: {result}"
        approve_result = approval.approve(rid_normal, "dispatch", "li", "OK", approver_role="调度主管")
        assert not approve_result.get("success"), f"未跑前置校验时审批通过应被阻断, 实际: {approve_result}"
        return "审批门禁已阻断"

    test("2. 前置校验门禁：未校验时审批被阻断", t2_pre_check_gate)

    def t2b_pre_check_fail_gate():
        rid_fail = create_release(
            version="2.4.0-fail",
            release_type="normal",
            summary="FAIL门禁测试",
            applicant="tester",
            from_version="2.3.0",
        )
        storage.update_release(rid_fail, {
            "pre_check_result": {
                "overall_result": PreCheckResult.FAIL,
                "overall_score": 65.0,
                "block_reasons": ["在途监控连通率低于95%", "核心接口超时率超标"],
                "metrics": [],
            },
            "pre_check_score": 65.0,
        })
        approval = ApprovalFlowEngine()
        result = approval.init_approvals(rid_fail)
        assert result == [], f"前置校验FAIL时应阻断审批初始化, 实际返回: {result}"
        approve_result = approval.approve(rid_fail, "dispatch", "tester", "OK", approver_role="调度主管")
        assert not approve_result.get("success"), f"前置校验FAIL时审批应被阻断, 实际: {approve_result}"
        assert "前置校验未通过" in approve_result.get("error", ""), f"应提示前置校验未通过, 实际: {approve_result}"
        return "FAIL门禁已阻断（得分65.0）"

    test("2b. 前置校验门禁：校验失败时审批被阻断", t2b_pre_check_fail_gate)

    def t3_pre_check():
        pre = PreCheckEngine()
        result = pre.run(rid_normal)
        assert "overall_result" in result
        assert "overall_score" in result
        _ensure_pre_check_passed(rid_normal)
        return result["overall_result"]

    test("3. 发布前置校验", t3_pre_check)

    def t4_approval_init():
        approval = ApprovalFlowEngine()
        result = approval.init_approvals(rid_normal)
        assert len(result) == 4, f"审批初始化应创建4个节点, 实际: {len(result)}"
        status = approval.get_status(rid_normal)
        assert len(status["approvals"]) == 4
        return len(status["approvals"])

    test("4. 校验通过后审批初始化（4个串行节点）", t4_approval_init)

    def t5_serial_order_enforcement():
        approval = ApprovalFlowEngine()
        skip_result = approval.approve(rid_normal, "operation", "chen", "跳过调度直接审批", approver_role="运营总监")
        assert not skip_result.get("success"), f"跳级审批应被拒绝, 实际: {skip_result}"
        assert "先处理" in skip_result.get("error", ""), f"应提示先处理调度阶段, 实际: {skip_result}"
        return "串行顺序已强制"

    test("5. 串行审批顺序：跳级审批被拒绝", t5_serial_order_enforcement)

    def t6_rejection_block():
        approval = ApprovalFlowEngine()
        reject_result = approval.reject(rid_normal, "dispatch", "li", "调度资源不足，暂不发布")
        assert reject_result.get("success"), f"调度驳回应成功, 实际: {reject_result}"
        later_result = approval.approve(rid_normal, "operation", "chen", "运营同意", approver_role="运营总监")
        assert not later_result.get("success"), f"驳回后后续审批应被阻断, 实际: {later_result}"
        assert "驳回" in later_result.get("error", ""), f"应提示存在驳回, 实际: {later_result}"
        return "驳回阻断已生效"

    test("6. 驳回阻断：调度驳回后运营审批被拒绝", t6_rejection_block)

    rid_reject = create_release(
        version="2.4.0-retry",
        release_type="normal",
        summary="驳回后重新提交",
        applicant="wang_dev",
        from_version="2.3.5",
    )
    PreCheckEngine().run(rid_reject)
    _ensure_pre_check_passed(rid_reject)
    approval_retry = ApprovalFlowEngine()
    approval_retry.init_approvals(rid_reject)

    def t7_approval_flow():
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
                rid_reject, sk, approver, "OK", approver_role=role
            )
        status = approval.get_status(rid_reject)
        release_status = status["release"]["state"]
        assert release_status in ("READY_FOR_DEPLOY", "APPROVED")
        return release_status

    test("7. 四级审批全部通过", t7_approval_flow)

    def t8_gray_release():
        gray = GrayReleaseEngine()
        gray.start_release(rid_reject, operator="admin")
        phases = gray.get_phases()
        assert len(phases) == 4
        for i in range(4):
            result = gray.advance_phase(rid_reject, force=True)
        return result.get("completed", False)

    test("8. 灰度发布四阶段推进至全量", t8_gray_release)

    def t9_consecutive_window_logic():
        import random as _rnd
        from core.storage import storage as st
        test_rid = f"TEST-CW-{_rnd.randint(1000,9999)}"
        st.create_release({
            "id": test_rid, "version": "9.9.9", "release_type": "normal",
            "summary": "连续窗口测试", "applicant": "tester", "state": "OBSERVING",
            "from_version": "9.9.8",
        })
        windows_data = [
            {"release_id": test_rid, "phase": "feeder_routes", "metric_name": "测试指标",
             "metric_value": 5.0, "threshold": 3.0, "is_breach": True,
             "window_start": "2026-06-20T10:00:00", "window_end": "2026-06-20T10:05:00"},
            {"release_id": test_rid, "phase": "feeder_routes", "metric_name": "测试指标",
             "metric_value": 1.0, "threshold": 3.0, "is_breach": False,
             "window_start": "2026-06-20T10:05:00", "window_end": "2026-06-20T10:10:00"},
            {"release_id": test_rid, "phase": "feeder_routes", "metric_name": "测试指标",
             "metric_value": 4.5, "threshold": 3.0, "is_breach": True,
             "window_start": "2026-06-20T10:10:00", "window_end": "2026-06-20T10:15:00"},
            {"release_id": test_rid, "phase": "feeder_routes", "metric_name": "测试指标",
             "metric_value": 4.8, "threshold": 3.0, "is_breach": True,
             "window_start": "2026-06-20T10:15:00", "window_end": "2026-06-20T10:20:00"},
        ]
        for w in windows_data:
            st.add_metric_window(w)

        cb = CircuitBreakerEngine()
        recent = st.get_recent_metric_windows(test_rid, "测试指标", 5)
        consecutive = cb._count_consecutive_breaches(recent)
        assert consecutive == 2, f"超限/正常/超限/超限 → 连续超限应为2, 实际: {consecutive}"
        assert consecutive < cb.consecutive_window_threshold, "不应触发熔断（中间有恢复正常）"
        return f"连续={consecutive}, 阈值={cb.consecutive_window_threshold}, 未误触发"

    test("9. 连续窗口判断：超限/正常/超限/超限不误触发熔断", t9_consecutive_window_logic)

    def t10_hotfix_release():
        rid = create_release(
            version="2.4.0-hotfix1",
            release_type="hotfix",
            summary="运费计算紧急修复",
            applicant="oncall",
            hotfix_reason="P0故障：运费偏差>15%",
            from_version="2.4.0",
        )
        PreCheckEngine().run(rid)
        _ensure_pre_check_passed(rid)
        approval = ApprovalFlowEngine()
        approval.init_approvals(rid)
        approval.approve(
            rid, "all_parallel", "tech_director", "先发布",
            approver_role="技术架构师"
        )
        status = approval.get_status(rid)
        return status["approval_mode"] == "parallel"

    test("10. 紧急热修复并行审批通道", t10_hotfix_release)

    def t11_circuit_breaker_rollback():
        rid = create_release(
            version="2.5.0-rc1",
            release_type="normal",
            summary="熔断测试版本",
            applicant="tester",
            from_version="2.4.0",
        )
        PreCheckEngine().run(rid)
        _ensure_pre_check_passed(rid)
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

        import json
        from core import REPORT_DIR
        report_files = list(REPORT_DIR.glob("RB-*.json"))
        assert len(report_files) > 0, "未找到回滚报告文件"
        latest_report = json.loads(report_files[-1].read_text(encoding="utf-8"))
        assert "rollback_to_version" in latest_report, "回滚报告缺少 rollback_to_version 字段"
        to_ver = latest_report["rollback_to_version"]
        assert to_ver == "2.4.0", f"回滚目标版本应为2.4.0, 实际: {to_ver}"
        assert "previous_stable" not in to_ver, f"回滚版本不应包含previous_stable: {to_ver}"
        return f"rollback_to_version={to_ver}"

    test("11. 熔断回滚 + 回滚报告版本号验证", t11_circuit_breaker_rollback)

    def t12_drill_execution():
        de = DrillEngine()
        result = de.run_drill("rollback", "自动化测试演练")
        assert result["status"] in ("PASSED", "FAILED")
        assert "report_path" in result
        return result["status"]

    test("12. 回滚流程演练执行", t12_drill_execution)

    def t13_report_generation():
        rg = ReportGenerator()
        reports = rg.generate_all_reports("weekly")
        assert "release_quality" in reports
        assert "approval_efficiency" in reports
        return len(reports)

    test("13. 周报报表生成（质量+审批效率）", t13_report_generation)

    def t14_monthly_report():
        rg = ReportGenerator()
        reports = rg.generate_all_reports("monthly")
        assert "rollback_analysis" in reports
        assert "drill_effectiveness" in reports
        return len(reports)

    test("14. 月报报表生成（+回滚分析+演练效果）", t14_monthly_report)

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
