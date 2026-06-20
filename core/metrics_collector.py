import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import logger
from .config_loader import config


class MetricsCollector:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ReleasePlatform/1.0"})

    def _query_prometheus(self, promql: str) -> Optional[float]:
        base = config.get("main.external_systems.prometheus.base_url", "")
        if not base or "example.com" in base:
            logger.warning("Prometheus未配置，使用模拟数据")
            return None

        try:
            resp = self.session.get(
                f"{base}/api/v1/query",
                params={"query": promql},
                timeout=config.get("main.external_systems.prometheus.timeout", 10),
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success" and data["data"]["result"]:
                value = data["data"]["result"][0]["value"][1]
                return float(value)
        except Exception as e:
            logger.error(f"Prometheus查询失败: {e}")
        return None

    def _check_health(self, system_key: str) -> Tuple[bool, float]:
        sys_cfg = config.get(f"main.external_systems.{system_key}")
        if not sys_cfg or "example.com" in sys_cfg.get("base_url", ""):
            return True, 99.9

        url = f"{sys_cfg['base_url']}{sys_cfg.get('health_endpoint', '/health')}"
        success_count = 0
        total = 5
        for i in range(total):
            try:
                resp = self.session.get(url, timeout=sys_cfg.get("timeout", 5))
                if resp.status_code == 200:
                    success_count += 1
            except Exception:
                pass
            time.sleep(0.2)

        availability = (success_count / total) * 100
        return availability >= 99.5, availability

    @staticmethod
    def _simulate_metric(
        name: str, safe_threshold: float, circuit_threshold: float, higher_is_bad: bool = True
    ) -> float:
        variance = (circuit_threshold - safe_threshold) * 0.6
        base = safe_threshold + random.uniform(-variance * 0.5, variance)
        value = max(0, base)
        return round(value, 3)

    def collect_runtime_metric(
        self, metric_key: str, use_simulation_fallback: bool = True
    ) -> Optional[float]:
        metric_cfg = config.get(f"thresholds.runtime_metrics.{metric_key}")
        if not metric_cfg:
            logger.warning(f"未知指标: {metric_key}")
            return None

        promql = metric_cfg.get("promql", "")
        value = self._query_prometheus(promql) if promql else None

        if value is None and use_simulation_fallback:
            value = self._simulate_metric(
                metric_key,
                metric_cfg["safe_threshold"],
                metric_cfg["circuit_breaker_threshold"],
            )
            logger.debug(f"[{metric_key}] 模拟值: {value}{metric_cfg.get('unit','')}")

        return value

    def collect_all_runtime_metrics(
        self, use_simulation_fallback: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        metrics_cfg = config.get("thresholds.runtime_metrics", {})
        result = {}
        for key, cfg in metrics_cfg.items():
            value = self.collect_runtime_metric(key, use_simulation_fallback)
            if value is not None:
                result[key] = {
                    "name": cfg["name"],
                    "value": value,
                    "unit": cfg.get("unit", ""),
                    "safe_threshold": cfg["safe_threshold"],
                    "circuit_breaker_threshold": cfg["circuit_breaker_threshold"],
                }
        return result

    def collect_pre_check_metrics(
        self, use_simulation_fallback: bool = True
    ) -> Dict[str, Dict[str, Any]]:
        thresholds_cfg = config.get("thresholds.pre_check", {})
        result = {}

        for key, cfg in thresholds_cfg.items():
            if key == "overall_score":
                continue

            value: Optional[float] = None
            data_source = cfg.get("data_source", "")
            threshold = cfg.get("threshold", 0)
            operator = cfg.get("operator", ">=")

            if data_source == "monitor_api":
                value = self.collect_runtime_metric(
                    "offline_rate", use_simulation_fallback
                )
                if value is not None:
                    value = 100.0 - value
            elif data_source == "health_check":
                _, avail = self._check_health(key.replace("_availability", ""))
                value = avail
            elif use_simulation_fallback:
                if operator == ">=":
                    value = round(
                        threshold + random.uniform(-1.5, 2.0), 3
                    )
                else:
                    value = round(
                        max(0, threshold - random.uniform(-1.0, 1.5)), 3
                    )
                logger.debug(f"[预校验-{key}] 模拟值: {value}{cfg.get('unit','')}")

            if value is not None:
                result[key] = {
                    "name": cfg["name"],
                    "category": cfg.get("category", ""),
                    "weight": cfg.get("weight", 0),
                    "value": value,
                    "threshold": threshold,
                    "operator": operator,
                    "unit": cfg.get("unit", ""),
                    "data_source": data_source,
                }
        return result

    def get_time_window(self, minutes: int = 5) -> Tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(minutes=minutes)
        return start.isoformat(), end.isoformat()


metrics_collector = MetricsCollector()
