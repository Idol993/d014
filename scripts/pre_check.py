import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import logger
from core.config_loader import config
from core.metrics_collector import metrics_collector
from core.storage import storage
from core.state_machine import ReleaseState, StateMachine
from core.notifier import notifier


class PreCheckResult:
    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"


class PreCheckEngine:
    def __init__(self):
        self.thresholds_cfg = config.get("thresholds.pre_check", {})
        self.state_machine = StateMachine("release")

    @staticmethod
    def _check_operator(
        value: float, threshold: float, operator: str
    ) -> Tuple[bool, float]:
        score = 0.0
        if operator == ">=":
            passed = value >= threshold
            if passed:
                ratio = min(1.0, value / threshold) if threshold > 0 else 1.0
                score = ratio
            else:
                ratio = value / threshold if threshold > 0 else 0
                score = min(0.9, ratio)
        elif operator == "<=":
            passed = value <= threshold
            if passed:
                ratio = 1.0 - min(1.0, (value - threshold) / (threshold * 0.5 + 1e-9))
                score = min(1.0, max(0.0, ratio))
            else:
                excess = value - threshold
                ratio = 1.0 - excess / (threshold + 1e-9)
                score = max(0.0, ratio)
        else:
            passed = value >= threshold
            score = 1.0 if passed else 0.0

        return passed, round(score, 4)

    def _evaluate_single(
        self, key: str, metric_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = self.thresholds_cfg.get(key, {})
        if not cfg:
            return {
                "key": key,
                "name": key,
                "passed": True,
                "score": 1.0,
                "weight": 0,
                "message": "未配置",
            }

        value = metric_data.get("value", 0)
        threshold = metric_data.get("threshold", cfg.get("threshold", 0))
        operator = metric_data.get("operator", cfg.get("operator", ">="))
        unit = metric_data.get("unit", cfg.get("unit", ""))
        weight = cfg.get("weight", 0)
        name = cfg.get("name", key)
        category = cfg.get("category", "")
        block_on_fail = cfg.get("block_on_fail", False)
        hard_block_threshold = cfg.get("hard_block_threshold")

        passed, score_ratio = self._check_operator(value, threshold, operator)
        weighted_score = round(score_ratio * weight, 2)

        hard_blocked = False
        if hard_block_threshold is not None:
            if operator == ">=" and value < hard_block_threshold:
                hard_blocked = True
            elif operator == "<=" and value > hard_block_threshold:
                hard_blocked = True

        message = (
            f"{name}: {value}{unit}（阈值 {operator} {threshold}{unit}）"
            f" → {'✅ 通过' if passed else '❌ 未达标'}"
            + (" 🔴 硬阻断" if hard_blocked else "")
        )

        return {
            "key": key,
            "name": name,
            "category": category,
            "value": value,
            "threshold": threshold,
            "operator": operator,
            "unit": unit,
            "weight": weight,
            "passed": passed,
            "hard_blocked": hard_blocked,
            "block_on_fail": block_on_fail,
            "score_ratio": score_ratio,
            "weighted_score": weighted_score,
            "message": message,
            "fix_suggestions": cfg.get("fix_suggestions", []) if not passed else [],
        }

    def _evaluate_overall(
        self, results: List[Dict[str, Any]]
    ) -> Tuple[str, float, List[str]]:
        overall_cfg = self.thresholds_cfg.get("overall_score", {})
        pass_threshold = overall_cfg.get("pass_threshold", 90)
        warning_threshold = overall_cfg.get("risk_warning_threshold", 95)

        total_weight = sum(r["weight"] for r in results) or 100
        total_score = round(sum(r["weighted_score"] for r in results), 2)
        scaled_score = round((total_score / total_weight) * 100, 2)

        hard_blocked = any(r.get("hard_blocked") for r in results)
        core_blocked = any(
            r.get("block_on_fail") and not r["passed"] for r in results
        )
        failed_count = sum(1 for r in results if not r["passed"])

        reasons: List[str] = []
        if hard_blocked:
            reasons.append("存在硬阻断指标，强制阻断发布")
        if core_blocked:
            reasons.append("核心指标未达标，阻断发布")
        if scaled_score < pass_threshold:
            reasons.append(f"综合得分 {scaled_score} 低于通过线 {pass_threshold}")

        if reasons:
            result = PreCheckResult.FAIL
        elif scaled_score < warning_threshold or failed_count > 0:
            result = PreCheckResult.WARNING
        else:
            result = PreCheckResult.PASS

        return result, scaled_score, reasons

    def run(self, release_id: str) -> Dict[str, Any]:
        logger.info(f"========== 开始前置校验: {release_id} ==========")

        release = storage.get_release(release_id)
        if not release:
            logger.error(f"发布单不存在: {release_id}")
            return {"success": False, "error": "发布单不存在"}

        raw_metrics = metrics_collector.collect_pre_check_metrics()

        detail_results: List[Dict[str, Any]] = []
        for key, metric_data in raw_metrics.items():
            evaluated = self._evaluate_single(key, metric_data)
            detail_results.append(evaluated)
            logger.info(evaluated["message"])
            if not evaluated["passed"] and evaluated["fix_suggestions"]:
                for suggestion in evaluated["fix_suggestions"][:2]:
                    logger.info(f"  💡 修复建议: {suggestion}")

        overall_result, overall_score, block_reasons = self._evaluate_overall(
            detail_results
        )

        logger.info("-" * 60)
        logger.info(
            f"综合得分: {overall_score} | 结果: {overall_result}"
        )
        for r in block_reasons:
            logger.info(f"  🔴 {r}")

        fix_suggestions_all = []
        for r in detail_results:
            if not r["passed"] and r["fix_suggestions"]:
                fix_suggestions_all.append(
                    {
                        "metric": r["name"],
                        "current": f"{r['value']}{r['unit']}",
                        "threshold": f"{r['operator']} {r['threshold']}{r['unit']}",
                        "suggestions": r["fix_suggestions"],
                    }
                )

        full_result = {
            "release_id": release_id,
            "check_time": datetime.now().isoformat(),
            "overall_result": overall_result,
            "overall_score": overall_score,
            "pass_threshold": self.thresholds_cfg.get("overall_score", {}).get(
                "pass_threshold", 90
            ),
            "block_reasons": block_reasons,
            "metrics": detail_results,
            "fix_suggestions": fix_suggestions_all,
        }

        self._persist_result(release_id, full_result, overall_score, overall_result)

        self._notify(release, full_result)

        logger.info(f"========== 前置校验完成: {overall_result} ==========")
        return full_result

    def _persist_result(
        self,
        release_id: str,
        result: Dict[str, Any],
        score: float,
        overall_result: str,
    ):
        storage.update_release(
            release_id,
            {"pre_check_result": result, "pre_check_score": score},
        )

        if overall_result in (PreCheckResult.PASS, PreCheckResult.WARNING):
            release = storage.get_release(release_id)
            try:
                self.state_machine.transition(
                    release_id,
                    release["state"] if release else None,
                    ReleaseState.PENDING_APPROVAL,
                    reason=f"前置校验通过（得分: {score}）",
                )
                if release:
                    storage.update_release(
                        release_id, {"state": ReleaseState.PENDING_APPROVAL}
                    )
            except ValueError as e:
                logger.warning(f"状态更新跳过: {e}")

    def _notify(self, release: Dict[str, Any], result: Dict[str, Any]):
        metrics_detail_lines = []
        for m in result["metrics"]:
            icon = "✅" if m["passed"] else "❌"
            metrics_detail_lines.append(
                f"> {icon} **{m['name']}**: {m['value']}{m['unit']} "
                f"（阈值 {m['operator']} {m['threshold']}{m['unit']}）"
            )
        metrics_detail = "\n".join(metrics_detail_lines)

        fix_suggestions_section = ""
        if result["fix_suggestions"]:
            parts = ["> \n> **🔧 修复建议**："]
            for item in result["fix_suggestions"]:
                parts.append(f"> \n> **{item['metric']}**（{item['current']} vs {item['threshold']}）：")
                for s in item["suggestions"]:
                    parts.append(f">   - {s}")
            fix_suggestions_section = "\n".join(parts)

        if result["overall_result"] == PreCheckResult.PASS:
            result_display = "✅ **全部通过**"
        elif result["overall_result"] == PreCheckResult.WARNING:
            result_display = "⚠️ **有风险提示，需关注**"
        else:
            result_display = "❌ **校验未通过，已阻断发布**"

        roles = []
        if result["overall_result"] != PreCheckResult.FAIL:
            first_stage = config.get(
                f"approval.release_channels.{release['release_type']}.stages[0]", {}
            )
            if first_stage:
                roles = [first_stage.get("role") or first_stage.get("roles", [])[0]]

        notifier.send_all(
            "pre_check_result",
            {
                "release_id": release["id"],
                "version": release["version"],
                "result": result_display,
                "score": result["overall_score"],
                "metrics_detail": metrics_detail,
                "fix_suggestions_section": fix_suggestions_section,
            },
            roles=roles + [release["applicant"]],
            release_id=release["id"],
        )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="发布前置校验")
    parser.add_argument(
        "--release-id", required=True, help="发布单号，如 REL-20260620-001"
    )
    args = parser.parse_args()

    engine = PreCheckEngine()
    result = engine.run(args.release_id)

    if result.get("overall_result") == PreCheckResult.FAIL:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
