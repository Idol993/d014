import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import logger
from scripts.release_cli import create_release, show_release
from scripts.pre_check import PreCheckEngine
from scripts.approval_flow import ApprovalFlowEngine
from scripts.gray_release import GrayReleaseEngine
from scripts.rollback import CircuitBreakerEngine, RollbackEngine
from scripts.drill_engine import DrillEngine
from scripts.report_generator import ReportGenerator


def print_banner(title: str):
    line = "=" * 70
    print(f"\n{line}")
    print(f"  {title}")
    print(line)


def demo_full_release_flow():
    print_banner("🚀 演示1：常规版本发布全流程（前置校验 → 四级审批 → 灰度发布）")

    release_id = create_release(
        version="2.4.0",
        release_type="normal",
        summary="调度算法v2优化 + 油耗模型升级",
        applicant="wang_dev",
        from_version="2.3.5",
    )
    show_release(release_id)
    time.sleep(0.5)

    print_banner("Step 1: 执行发布前置校验")
    pre = PreCheckEngine()
    pre_result = pre.run(release_id)
    print(f"\n校验结果: {pre_result.get('overall_result')}, 得分: {pre_result.get('overall_score')}")
    if pre_result.get("block_reasons"):
        for r in pre_result["block_reasons"]:
            print(f"  阻断原因: {r}")
    time.sleep(0.5)

    print_banner("Step 2: 初始化审批流程")
    approval = ApprovalFlowEngine()
    approval.init_approvals(release_id)
    status = approval.get_status(release_id)
    print(f"审批模式: {status['approval_mode']}")
    for a in status["approvals"]:
        print(f"  {a['stage_name']} ({a['approver_role']}): {a['status']}")
    time.sleep(0.5)

    print_banner("Step 3: 模拟四级串行审批通过")
    stages = ["dispatch", "operation", "security", "tech"]
    approvers = {
        "dispatch": ("li_scheduler", "调度主管"),
        "operation": ("chen_ops", "运营总监"),
        "security": ("zhao_sec", "安全合规经理"),
        "tech": ("sun_arch", "技术架构师"),
    }
    for stage_key in stages:
        approver, role = approvers[stage_key]
        result = approval.approve(
            release_id, stage_key, approver,
            comment=f"{role}审批通过", approver_role=role
        )
        print(f"  ✅ {role}审批通过 → {result.get('result')}")
        time.sleep(0.3)
    show_release(release_id)
    time.sleep(0.5)

    print_banner("Step 4: 启动灰度发布")
    gray = GrayReleaseEngine()
    gray.start_release(release_id, operator="release_manager")
    status = gray.get_status(release_id)
    print(f"当前阶段: {status['current_phase']['display_name'] if status['current_phase'] else '-'}")
    print(f"流量占比: {status['release']['traffic_percent']}%")

    for _ in range(3):
        result = gray.advance_phase(release_id, operator="release_manager", force=True)
        if result.get("completed"):
            print(f"  ✅ 发布完成: {result}")
            break
        phase = result.get("current_phase")
        if phase:
            print(f"  🚀 推进至: {phase.get('display_name')} ({phase.get('traffic_percent', 0)}%)")
        time.sleep(0.5)

    show_release(release_id)
    return release_id


def demo_hotfix_flow():
    print_banner("🚨 演示2：紧急热修复发布流程（并行审批 + 快速发布）")

    release_id = create_release(
        version="2.4.0-hotfix1",
        release_type="hotfix",
        summary="紧急修复运费计算异常导致的客户投诉",
        applicant="oncall_engineer",
        hotfix_reason="P0故障：华东区域运费计算偏差>15%，已影响200+运单",
        from_version="2.4.0",
    )
    show_release(release_id)
    time.sleep(0.5)

    print_banner("Step 1: 紧急前置校验（快速通道）")
    pre = PreCheckEngine()
    pre.run(release_id)
    time.sleep(0.3)

    print_banner("Step 2: 并行审批（任一人通过即可发布）")
    approval = ApprovalFlowEngine()
    approval.init_approvals(release_id)
    status = approval.get_status(release_id)
    print(f"审批模式: {status['approval_mode']}")

    result = approval.approve(
        release_id, "all_parallel", "tech_director",
        comment="紧急故障，先发布，后续补签", approver_role="技术架构师"
    )
    print(f"  ⚡ 技术架构师审批通过 → {result.get('result')}")
    show_release(release_id)
    return release_id


def demo_circuit_breaker_and_rollback():
    print_banner("🔥 演示3：熔断触发与自动回滚")

    release_id = create_release(
        version="2.5.0-rc1",
        release_type="normal",
        summary="新调度引擎灰度发布（用于熔断测试）",
        applicant="test_engineer",
        from_version="2.4.0",
    )

    print_banner("Step 1: 快速通过校验与审批")
    PreCheckEngine().run(release_id)
    approval = ApprovalFlowEngine()
    approval.init_approvals(release_id)
    for stage_key in ["dispatch", "operation", "security", "tech"]:
        approval.approve(release_id, stage_key, "auto_approver", "自动审批")

    print_banner("Step 2: 启动灰度发布至区域干线阶段")
    gray = GrayReleaseEngine()
    gray.start_release(release_id)
    gray.advance_phase(release_id, force=True)
    status = gray.get_status(release_id)
    print(f"当前阶段: {status['current_phase']['display_name'] if status['current_phase'] else '-'}")

    print_banner("Step 3: 模拟监控采集与连续超限窗口（触发熔断）")
    cb = CircuitBreakerEngine()
    print("  模拟 3 个连续超限窗口...")
    for i in range(3):
        print(f"  窗口 {i+1}/3: 采集指标并检查阈值...")
        time.sleep(0.3)

    trigger = {
        "metric_name": "调度失败率",
        "value": 3.42,
        "threshold": 3.0,
        "unit": "%",
        "consecutive_windows": 3,
    }
    print(f"\n  🔴 触发熔断: {trigger['metric_name']}={trigger['value']}% (阈值 {trigger['threshold']}%)")
    print(f"     连续 {trigger['consecutive_windows']} 个窗口超限\n")

    rb = RollbackEngine()
    result = rb.execute_circuit_breaker(release_id, trigger)
    print(f"\n回滚单号: {result.get('rollback_id')}")
    print(f"回滚状态: {result.get('status')}")
    print(f"执行耗时: {result.get('duration_seconds')} 秒")
    print(f"报告路径: {result.get('report_path')}")

    show_release(release_id)
    return release_id


def demo_drill_and_reports():
    print_banner("🎯 演示4：演练执行与复盘报表")

    print_banner("Step 1: 执行回滚流程演练")
    de = DrillEngine()
    result = de.run_drill("rollback", name="月度回滚自动化演练")
    print(f"演练ID: {result.get('drill_id')}")
    print(f"结果: {result.get('status')}")
    print(f"耗时: {result.get('duration_minutes')} 分钟")
    print(f"问题数: {len(result.get('issues', []))}")
    print(f"改进项: {len(result.get('improvements', []))}")
    for issue in result.get("issues", []):
        print(f"  问题: [{issue['severity']}] {issue['step']} - {issue['root_cause']}")
    time.sleep(0.5)

    print_banner("Step 2: 生成复盘报表")
    rg = ReportGenerator()
    reports = rg.generate_all_reports("weekly")
    for name, info in reports.items():
        path = info.get("path", "")
        data = info.get("data", {})
        summary = data.get("summary", {})
        print(f"  📊 {name}: {path}")
        if summary:
            for k, v in summary.items():
                print(f"      - {k}: {v}")
    return result.get("drill_id")


def main():
    print("\n" + "█" * 70)
    print("█  干线运输调度系统 - 版本发布与自动回滚自动化平台 DEMO")
    print("█" * 70)
    print()

    demos = [
        ("常规版本发布全流程", demo_full_release_flow),
        ("紧急热修复发布流程", demo_hotfix_flow),
        ("熔断触发与自动回滚", demo_circuit_breaker_and_rollback),
        ("演练执行与复盘报表", demo_drill_and_reports),
    ]

    for i, (name, func) in enumerate(demos, 1):
        print(f"  [{i}] {name}")
    print(f"  [0] 运行全部演示")
    print()

    choice = input("请选择要运行的演示 (0-4, 默认0): ").strip() or "0"

    if choice == "0":
        ids = []
        for _, func in demos:
            try:
                rid = func()
                ids.append(rid)
            except Exception as e:
                logger.exception(f"演示失败: {e}")
        print_banner("✅ 全部演示完成")
        print(f"创建的发布单/演练ID: {ids}")
    elif choice.isdigit() and 1 <= int(choice) <= len(demos):
        demos[int(choice) - 1][1]()
    else:
        print("无效选择")


if __name__ == "__main__":
    main()
