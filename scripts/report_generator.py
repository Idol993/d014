import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import REPORT_DIR, logger
from core.storage import storage
from core.state_machine import ReleaseState


class ReportPeriod:
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ReportGenerator:
    def __init__(self):
        pass

    def _get_date_range(self, period: str) -> tuple:
        now = datetime.now()
        if period == ReportPeriod.DAILY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif period == ReportPeriod.WEEKLY:
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end = now
        else:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
        return start, end

    def _load_releases_in_range(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        all_releases = storage.list_releases(limit=500)
        result = []
        for r in all_releases:
            try:
                created = datetime.fromisoformat(r["created_at"])
                if start <= created <= end:
                    result.append(r)
            except (ValueError, KeyError):
                continue
        return result

    def generate_release_quality_report(
        self, period: str = ReportPeriod.WEEKLY
    ) -> Dict[str, Any]:
        start, end = self._get_date_range(period)
        releases = self._load_releases_in_range(start, end)

        total = len(releases)
        by_type = defaultdict(int)
        by_state = defaultdict(int)
        completed = 0
        rolled_back = 0
        total_score = 0.0
        scored_count = 0

        for r in releases:
            by_type[r["release_type"]] += 1
            by_state[r["state"]] += 1
            if r["state"] == ReleaseState.COMPLETED:
                completed += 1
            elif r["state"] == ReleaseState.ROLLED_BACK:
                rolled_back += 1
            score = r.get("pre_check_score")
            if score is not None and score > 0:
                total_score += score
                scored_count += 1

        success_rate = round((completed / total * 100), 2) if total > 0 else 0.0
        rollback_rate = round((rolled_back / total * 100), 2) if total > 0 else 0.0
        avg_score = round(total_score / scored_count, 2) if scored_count > 0 else 0.0

        report = {
            "report_type": "release_quality",
            "period": period,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total_releases": total,
                "completed": completed,
                "rolled_back": rolled_back,
                "rejected": by_state.get(ReleaseState.REJECTED, 0),
                "in_progress": total - completed - rolled_back - by_state.get(ReleaseState.REJECTED, 0),
                "success_rate": f"{success_rate}%",
                "rollback_rate": f"{rollback_rate}%",
                "average_precheck_score": f"{avg_score}",
            },
            "by_release_type": dict(by_type),
            "by_state": dict(by_state),
            "releases": [
                {
                    "id": r["id"],
                    "version": r["version"],
                    "release_type": r["release_type"],
                    "state": r["state"],
                    "applicant": r["applicant"],
                    "pre_check_score": r.get("pre_check_score"),
                    "created_at": r["created_at"],
                }
                for r in releases
            ],
        }
        return self._save_report(f"{period}_release_quality", report)

    def generate_approval_efficiency_report(
        self, period: str = ReportPeriod.WEEKLY
    ) -> Dict[str, Any]:
        start, end = self._get_date_range(period)
        releases = self._load_releases_in_range(start, end)

        approval_stats: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "approved": 0, "rejected": 0, "total_hours": 0.0}
        )

        for release in releases:
            approvals = storage.get_approvals(release["id"])
            for a in approvals:
                stage_name = a["stage_name"]
                stats = approval_stats[stage_name]
                stats["count"] += 1
                if a["status"] == "APPROVED":
                    stats["approved"] += 1
                    try:
                        created = datetime.fromisoformat(a["created_at"])
                        approved = datetime.fromisoformat(a["approved_at"])
                        stats["total_hours"] += (approved - created).total_seconds() / 3600
                    except (ValueError, TypeError):
                        pass
                elif a["status"] == "REJECTED":
                    stats["rejected"] += 1

        stage_summary = []
        for stage, stats in approval_stats.items():
            avg_hours = round(stats["total_hours"] / stats["approved"], 2) if stats["approved"] > 0 else 0
            approval_rate = round(stats["approved"] / stats["count"] * 100, 2) if stats["count"] > 0 else 0
            stage_summary.append(
                {
                    "stage": stage,
                    "total": stats["count"],
                    "approved": stats["approved"],
                    "rejected": stats["rejected"],
                    "approval_rate": f"{approval_rate}%",
                    "avg_approval_hours": f"{avg_hours}h",
                }
            )

        report = {
            "report_type": "approval_efficiency",
            "period": period,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "stage_summary": stage_summary,
        }
        return self._save_report(f"{period}_approval_efficiency", report)

    def generate_rollback_analysis_report(
        self, period: str = ReportPeriod.MONTHLY
    ) -> Dict[str, Any]:
        start, end = self._get_date_range(period)

        all_releases = storage.list_releases(limit=500)
        rollback_releases = []
        for r in all_releases:
            try:
                created = datetime.fromisoformat(r["created_at"])
                if start <= created <= end and r["state"] == ReleaseState.ROLLED_BACK:
                    rollback_releases.append(r)
            except (ValueError, KeyError):
                continue

        trigger_reasons: Dict[str, int] = defaultdict(int)
        total_duration = 0
        total_traffic = 0
        total_orders = 0

        rollback_details = []
        for r in rollback_releases:
            conn = storage._get_conn()
            row = conn.execute(
                "SELECT * FROM rollbacks WHERE release_id = ? ORDER BY id DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            if row:
                rb = dict(row)
                trigger = rb.get("trigger_metric") or "未知"
                trigger_reasons[trigger] += 1
                total_duration += rb.get("duration_seconds") or 0
                impact = storage._from_json(rb.get("impact_scope", "")) or {}
                total_traffic += impact.get("affected_traffic_percent", 0)
                total_orders += impact.get("estimated_impact_orders", 0)

                rollback_details.append(
                    {
                        "rollback_id": rb["id"],
                        "release_id": rb["release_id"],
                        "trigger_metric": rb.get("trigger_metric"),
                        "trigger_value": rb.get("trigger_value"),
                        "duration_seconds": rb.get("duration_seconds"),
                        "status": rb.get("status"),
                        "trigger_time": rb.get("trigger_time"),
                    }
                )

        count = len(rollback_details)
        report = {
            "report_type": "rollback_analysis",
            "period": period,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total_rollbacks": count,
                "avg_duration_seconds": round(total_duration / count) if count > 0 else 0,
                "avg_affected_traffic_percent": round(total_traffic / count, 1) if count > 0 else 0,
                "total_estimated_impact_orders": total_orders,
            },
            "trigger_reason_distribution": dict(trigger_reasons),
            "rollback_details": rollback_details,
        }
        return self._save_report(f"{period}_rollback_analysis", report)

    def generate_drill_effectiveness_report(
        self, period: str = ReportPeriod.MONTHLY
    ) -> Dict[str, Any]:
        start, end = self._get_date_range(period)

        conn = storage._get_conn()
        rows = conn.execute(
            "SELECT * FROM drills ORDER BY id DESC LIMIT 100"
        ).fetchall()

        drills = []
        for row in rows:
            d = dict(row)
            try:
                started = datetime.fromisoformat(d["started_at"])
                if start <= started <= end:
                    drills.append(d)
            except (ValueError, TypeError):
                continue

        by_type: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "passed": 0, "failed": 0, "total_minutes": 0}
        )
        all_issues: List[Dict[str, Any]] = []
        all_improvements: List[Dict[str, Any]] = []

        for d in drills:
            dtype = d["drill_type"]
            stats = by_type[dtype]
            stats["count"] += 1
            status = d["status"]
            if status == "PASSED":
                stats["passed"] += 1
            else:
                stats["failed"] += 1
            stats["total_minutes"] += d.get("duration_minutes") or 0

            issues = storage._from_json(d.get("issues", "")) or []
            improvements = storage._from_json(d.get("improvements", "")) or []
            all_issues.extend(issues)
            all_improvements.extend(improvements)

        type_summary = []
        for dtype, stats in by_type.items():
            pass_rate = round(stats["passed"] / stats["count"] * 100, 2) if stats["count"] > 0 else 0
            avg_min = round(stats["total_minutes"] / stats["count"], 1) if stats["count"] > 0 else 0
            type_summary.append(
                {
                    "drill_type": dtype,
                    "count": stats["count"],
                    "passed": stats["passed"],
                    "failed": stats["failed"],
                    "pass_rate": f"{pass_rate}%",
                    "avg_duration_minutes": f"{avg_min}min",
                }
            )

        report = {
            "report_type": "drill_effectiveness",
            "period": period,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "generated_at": datetime.now().isoformat(),
            "drill_type_summary": type_summary,
            "total_issues_found": len(all_issues),
            "total_improvements_identified": len(all_improvements),
            "top_issues": all_issues[:10],
            "top_improvements": all_improvements[:10],
        }
        return self._save_report(f"{period}_drill_effectiveness", report)

    def generate_all_reports(self, period: str = ReportPeriod.WEEKLY) -> Dict[str, Any]:
        logger.info(f"生成{period}报告...")
        results = {
            "release_quality": self.generate_release_quality_report(period),
            "approval_efficiency": self.generate_approval_efficiency_report(period),
        }
        if period in (ReportPeriod.MONTHLY,):
            results["rollback_analysis"] = self.generate_rollback_analysis_report(period)
            results["drill_effectiveness"] = self.generate_drill_effectiveness_report(period)
        return results

    def _save_report(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d")
        path = REPORT_DIR / f"{ts}_{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"报表已生成: {path}")
        return {"path": str(path), "data": data}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="复盘报表生成")
    sub = parser.add_subparsers(dest="cmd")

    p_release = sub.add_parser("release-quality", help="发布质量报表")
    p_release.add_argument(
        "--period", choices=["daily", "weekly", "monthly"], default="weekly"
    )

    p_approval = sub.add_parser("approval", help="审批效率报表")
    p_approval.add_argument(
        "--period", choices=["daily", "weekly", "monthly"], default="weekly"
    )

    p_rollback = sub.add_parser("rollback", help="回滚分析报表")
    p_rollback.add_argument(
        "--period", choices=["weekly", "monthly"], default="monthly"
    )

    p_drill = sub.add_parser("drill", help="演练效果报表")
    p_drill.add_argument(
        "--period", choices=["weekly", "monthly"], default="monthly"
    )

    p_all = sub.add_parser("all", help="生成全部报表")
    p_all.add_argument(
        "--period", choices=["daily", "weekly", "monthly"], default="weekly"
    )

    args = parser.parse_args()
    gen = ReportGenerator()

    if args.cmd == "release-quality":
        print(json.dumps(gen.generate_release_quality_report(args.period), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "approval":
        print(json.dumps(gen.generate_approval_efficiency_report(args.period), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "rollback":
        print(json.dumps(gen.generate_rollback_analysis_report(args.period), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "drill":
        print(json.dumps(gen.generate_drill_effectiveness_report(args.period), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "all":
        print(json.dumps(gen.generate_all_reports(args.period), ensure_ascii=False, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
