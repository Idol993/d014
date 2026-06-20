from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

from . import logger


class ReleaseState(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    READY_FOR_DEPLOY = "READY_FOR_DEPLOY"
    DEPLOYING = "DEPLOYING"
    DEPLOYED = "DEPLOYED"
    OBSERVING = "OBSERVING"
    STABLE = "STABLE"
    ROLLBACK = "ROLLBACK"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"


TRANSITIONS: Dict[str, List[str]] = {
    ReleaseState.PENDING_SUBMIT: [ReleaseState.PENDING_APPROVAL],
    ReleaseState.PENDING_APPROVAL: [ReleaseState.APPROVED, ReleaseState.REJECTED],
    ReleaseState.APPROVED: [ReleaseState.READY_FOR_DEPLOY, ReleaseState.REJECTED],
    ReleaseState.REJECTED: [ReleaseState.PENDING_SUBMIT],
    ReleaseState.READY_FOR_DEPLOY: [ReleaseState.DEPLOYING, ReleaseState.CLOSED],
    ReleaseState.DEPLOYING: [ReleaseState.DEPLOYED, ReleaseState.ROLLBACK],
    ReleaseState.DEPLOYED: [ReleaseState.OBSERVING, ReleaseState.ROLLBACK],
    ReleaseState.OBSERVING: [ReleaseState.STABLE, ReleaseState.ROLLBACK],
    ReleaseState.STABLE: [ReleaseState.COMPLETED, ReleaseState.ROLLBACK],
    ReleaseState.ROLLBACK: [ReleaseState.ROLLED_BACK],
    ReleaseState.ROLLED_BACK: [ReleaseState.CLOSED],
    ReleaseState.COMPLETED: [ReleaseState.CLOSED],
    ReleaseState.CLOSED: [],
}


class StateChangeEvent:
    def __init__(
        self,
        entity_id: str,
        from_state: Optional[str],
        to_state: str,
        operator: str = "system",
        reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.entity_id = entity_id
        self.from_state = from_state
        self.to_state = to_state
        self.operator = operator
        self.reason = reason
        self.extra = extra or {}
        self.timestamp = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "operator": self.operator,
            "reason": self.reason,
            "extra": self.extra,
            "timestamp": self.timestamp.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"StateChangeEvent({self.entity_id}: {self.from_state} -> {self.to_state}, "
            f"op={self.operator}, reason={self.reason})"
        )


class StateMachine:
    def __init__(self, entity_type: str = "release"):
        self.entity_type = entity_type
        self._listeners: List[Callable[[StateChangeEvent], None]] = []

    def add_listener(self, listener: Callable[[StateChangeEvent], None]):
        self._listeners.append(listener)

    def _notify(self, event: StateChangeEvent):
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"状态变更监听器执行失败: {e}")

    def can_transition(self, from_state: Optional[str], to_state: str) -> bool:
        if from_state is None:
            return to_state == ReleaseState.PENDING_SUBMIT
        allowed = TRANSITIONS.get(from_state, [])
        return to_state in allowed

    def transition(
        self,
        entity_id: str,
        current_state: Optional[str],
        target_state: str,
        operator: str = "system",
        reason: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> StateChangeEvent:
        if not self.can_transition(current_state, target_state):
            raise ValueError(
                f"非法状态变更: {self.entity_type}={entity_id}, "
                f"{current_state} -> {target_state} 不被允许"
            )

        event = StateChangeEvent(
            entity_id=entity_id,
            from_state=current_state,
            to_state=target_state,
            operator=operator,
            reason=reason,
            extra=extra,
        )

        logger.info(
            f"[{self.entity_type}] 状态变更: {entity_id} "
            f"{current_state or 'INIT'} → {target_state} "
            f"(操作人: {operator}, 原因: {reason or '无'})"
        )

        self._notify(event)
        return event

    @staticmethod
    def get_terminal_states() -> List[str]:
        return [ReleaseState.CLOSED, ReleaseState.COMPLETED, ReleaseState.ROLLED_BACK]

    @staticmethod
    def is_terminal(state: str) -> bool:
        return state in StateMachine.get_terminal_states()
