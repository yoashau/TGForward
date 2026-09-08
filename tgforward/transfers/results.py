"""提取结果的纯模型；源范围、最终送达事实与单次 RPC attempt 分层。

本模块不执行 I/O。调用方必须在最终目标 RPC 的线性化点调用 begin_attempt，
在明确返回后同步提交结果；下载及中转上传不得写入最终送达状态。
"""

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from tgforward.ui.i18n import tr


class SideEffectRole(StrEnum):
    DOWNLOAD = "download"
    STAGING_UPLOAD = "staging_upload"
    FINAL_DELIVERY = "final_delivery"


class AttemptState(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SourceResolution(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class MessageOutcome(StrEnum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SourceMessageOutcome(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DeliveryPart:
    id: str
    source_message_id: int


@dataclass(frozen=True)
class DeliverySnapshot:
    all_parts: frozenset[str]
    confirmed_delivered: frozenset[str]
    confirmed_failed: frozenset[str]
    uncertain: frozenset[str]

    def __post_init__(self):
        for name in ("all_parts", "confirmed_delivered", "confirmed_failed", "uncertain"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        groups = (self.confirmed_delivered, self.confirmed_failed, self.uncertain)
        if any(not group <= self.all_parts for group in groups):
            raise ValueError("final state contains an unknown part")
        if any(a & b for i, a in enumerate(groups) for b in groups[i + 1 :]):
            raise ValueError("final states must be disjoint")

    @property
    def not_attempted(self):
        return self.all_parts - self.confirmed_delivered - self.confirmed_failed - self.uncertain


class MessageDeliveryState:
    """封存后的 part 范围与不可逆 final state；公开集合均为不可变快照。"""

    def __init__(self):
        self._parts = {}
        self._sealed = False
        self._delivered = set()
        self._failed = set()
        self._uncertain = set()
        self._attempts = {}
        self._attempt_counts = {}
        self._reasons = {}

    @property
    def parts(self):
        return MappingProxyType(self._parts)

    @property
    def all_parts(self):
        return frozenset(self._parts)

    @property
    def total_parts(self):
        return len(self._parts)

    @property
    def sealed(self):
        return self._sealed

    @property
    def confirmed_delivered(self):
        return frozenset(self._delivered)

    @property
    def confirmed_failed(self):
        return frozenset(self._failed)

    @property
    def uncertain(self):
        return frozenset(self._uncertain)

    @property
    def not_attempted(self):
        return self.snapshot().not_attempted

    @property
    def attempts(self):
        return MappingProxyType(self._attempts)

    @property
    def attempt_counts(self):
        return MappingProxyType(self._attempt_counts)

    @property
    def reasons(self):
        return MappingProxyType(self._reasons)

    def seal(self, parts):
        parts = tuple(parts)
        mapping = {part.id: part for part in parts}
        if len(mapping) != len(parts):
            raise ValueError("duplicate part id")
        if self._sealed:
            if self._parts != mapping:
                raise ValueError("source parts are already sealed")
            return
        self._parts = mapping
        self._attempts = dict.fromkeys(mapping, AttemptState.READY)
        self._attempt_counts = dict.fromkeys(mapping, 0)
        self._sealed = True

    def _known(self, part):
        if not self._sealed or part not in self._parts:
            raise ValueError("unknown or unsealed part")

    def authorize(self, part, role=SideEffectRole.FINAL_DELIVERY):
        self._known(part)
        if role != SideEffectRole.FINAL_DELIVERY:
            raise ValueError("only final delivery may begin a delivery attempt")
        if part not in self.not_attempted or self._attempts[part] not in (
            AttemptState.READY,
            AttemptState.RETRY_WAIT,
        ):
            raise ValueError("part does not permit a new attempt")

    def begin_attempt(self, part, role=SideEffectRole.FINAL_DELIVERY):
        self.authorize(part, role)
        self._attempts[part] = AttemptState.IN_FLIGHT
        self._attempt_counts[part] += 1

    def reject(self, part, reason, *, retryable=False):
        self._require_attempt(part, AttemptState.IN_FLIGHT)
        self._reasons[part] = str(reason)
        self._attempts[part] = AttemptState.RETRY_WAIT if retryable else AttemptState.REJECTED

    def retryable_rejection(self, part, reason):
        self.reject(part, reason, retryable=True)

    def _require_attempt(self, part, *states):
        self._known(part)
        if part not in self.not_attempted or self._attempts[part] not in states:
            raise ValueError("invalid attempt transition")

    def confirm_delivered(self, part):
        self._known(part)
        if part in self._delivered:
            return
        self._require_attempt(part, AttemptState.IN_FLIGHT)
        self._delivered.add(part)
        self._attempts[part] = AttemptState.SUCCEEDED

    def hydrate_delivered(self, parts):
        """仅由已经验证 recovery identity 的调用方导入明确成功证据。"""
        parts = frozenset(parts)
        for part in parts:
            self._known(part)
            if part not in self._delivered:
                self._require_attempt(part, AttemptState.READY)
        for part in parts:
            self._delivered.add(part)
            self._attempts[part] = AttemptState.SUCCEEDED

    def finalize_failed(self, part):
        self._known(part)
        if part in self._failed:
            return
        self._require_attempt(part, AttemptState.REJECTED, AttemptState.RETRY_WAIT)
        self._failed.add(part)
        self._attempts[part] = AttemptState.REJECTED

    def mark_uncertain(self, part, reason=""):
        self._known(part)
        if part in self._uncertain:
            return
        self._require_attempt(part, AttemptState.IN_FLIGHT)
        self._uncertain.add(part)
        self._attempts[part] = AttemptState.UNKNOWN
        self._reasons[part] = str(reason)

    def settle(self):
        """执行结束：只收口实际开始过的 attempt，不虚构未尝试失败。"""
        for part in self.not_attempted:
            if self._attempts[part] == AttemptState.IN_FLIGHT:
                self.mark_uncertain(part, "execution ended without a definite RPC result")
            elif self._attempts[part] in (AttemptState.REJECTED, AttemptState.RETRY_WAIT):
                self.finalize_failed(part)

    def snapshot(self):
        return DeliverySnapshot(
            self.all_parts, self.confirmed_delivered, self.confirmed_failed, self.uncertain
        )


def derive_outcome(source_resolution, snapshot):
    if snapshot.uncertain:
        return MessageOutcome.UNCERTAIN
    if source_resolution != SourceResolution.COMPLETE or not snapshot.all_parts:
        return MessageOutcome.INCOMPLETE
    if snapshot.confirmed_delivered == snapshot.all_parts:
        return MessageOutcome.SUCCESS
    if snapshot.confirmed_failed == snapshot.all_parts:
        return MessageOutcome.FAILED
    return MessageOutcome.INCOMPLETE


@dataclass
class MessageResult:
    id: str
    commit_key: str
    delivery: MessageDeliveryState = field(default_factory=MessageDeliveryState)
    is_comment: bool = False
    summary: str = ""
    last_message_id: int | None = None
    sent_message: object | None = field(default=None, repr=False)
    _source_message_ids: tuple[int, ...] = field(default=(), init=False, repr=False)
    _source_resolution: SourceResolution = field(default=SourceResolution.PENDING, init=False)
    _source_error: str = field(default="", init=False)

    @property
    def source_message_ids(self):
        return self._source_message_ids

    @property
    def source_resolution(self):
        return self._source_resolution

    @property
    def source_error(self):
        return self._source_error

    def resolve_source(self, source_message_ids, parts):
        ids, parts = tuple(source_message_ids), tuple(parts)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate source message id")
        if {part.source_message_id for part in parts} != set(ids):
            raise ValueError("every source member must have its full delivery parts")
        if self._source_resolution == SourceResolution.COMPLETE and ids != self.source_message_ids:
            raise ValueError("source members are already sealed")
        self.delivery.seal(parts)
        self._source_message_ids = ids
        self._source_resolution = SourceResolution.COMPLETE
        self._source_error = ""

    def fail_source(self, error):
        self._source_resolution = SourceResolution.FAILED
        self._source_error = str(error)

    @property
    def outcome(self):
        return derive_outcome(self.source_resolution, self.delivery.snapshot())

    def source_outcomes(self):
        """按源消息投影；长文字多个 chunk 只贡献一个评论计数。"""
        snapshot = self.delivery.snapshot()
        outcomes = {}
        for source in self.source_message_ids:
            parts = frozenset(
                part.id for part in self.delivery.parts.values() if part.source_message_id == source
            )
            if parts & snapshot.uncertain:
                outcome = SourceMessageOutcome.UNCERTAIN
            elif not self.delivery.sealed or not parts:
                outcome = SourceMessageOutcome.SKIPPED
            elif parts <= snapshot.confirmed_delivered:
                outcome = SourceMessageOutcome.SUCCESS
            elif parts & snapshot.confirmed_delivered:
                outcome = SourceMessageOutcome.PARTIAL
            elif parts & snapshot.confirmed_failed:
                outcome = SourceMessageOutcome.FAILED
            else:
                outcome = SourceMessageOutcome.SKIPPED
            outcomes[source] = outcome
        return outcomes


@dataclass
class CommentResult:
    success: int = 0
    partial: int = 0
    uncertain: int = 0
    failed: int = 0
    skipped: int = 0
    truncated: bool = False
    stopped: bool = False
    empty: bool = False
    last_error: str = ""
    _sources: dict = field(default_factory=dict, repr=False)

    @property
    def incomplete(self):
        return bool(
            self.partial
            or self.uncertain
            or self.failed
            or self.skipped
            or self.truncated
            or self.stopped
            or self.last_error
        )

    def summary(self):
        if self.empty:
            return tr("该帖子暂时没有评论。")
        text = tr("评论提取：成功 {0} 条，失败 {1} 条。", self.success, self.failed)
        if self.partial:
            text += tr("\n部分发送 {0} 条。", self.partial)
        if self.uncertain:
            text += tr("\n发送结果无法确认 {0} 条。", self.uncertain)
        if self.skipped:
            text += tr("\n未尝试发送 {0} 条。", self.skipped)
        if self.last_error:
            text += tr("\n最后错误：{0}", self.last_error)
        if self.truncated:
            text += tr("\n已达到单次 1000 条上限，本次未全部提取；重新提取仍会从头读取。")
        if self.incomplete:
            text += tr("\n重新提取可能重复发送已成功的评论。")
        return text

    def observe(self, result):
        for source, outcome in result.source_outcomes().items():
            key = (result.id, source)
            previous = self._sources.get(key)
            if previous == outcome:
                continue
            if previous is not None:
                setattr(self, previous.value, getattr(self, previous.value) - 1)
            self._sources[key] = outcome
            setattr(self, outcome.value, getattr(self, outcome.value) + 1)
        if result.source_error:
            self.last_error = result.source_error
        if self._sources or self.last_error:
            self.empty = False


@dataclass
class ExtractionUnit:
    id: str
    task_token: str
    request_total: int = 1
    request_current: int = 0
    message_results: list[MessageResult] = field(default_factory=list)
    comment_results: list[CommentResult] = field(default_factory=list)
    status: object | None = field(default=None, repr=False)
    _messages: dict = field(default_factory=dict, repr=False)

    def comments_summary(self):
        if not self.comment_results:
            return None
        combined = CommentResult()
        for item in self.comment_results:
            for name in ("success", "partial", "uncertain", "failed", "skipped"):
                setattr(combined, name, getattr(combined, name) + getattr(item, name))
            combined.truncated |= item.truncated
            combined.stopped |= item.stopped
            combined.last_error = item.last_error or combined.last_error
        combined.empty = all(item.empty for item in self.comment_results)
        return combined.summary()

    def message(self, source_key):
        """同一 Task/Unit 中重入使用稳定逻辑身份，不因异常重新生成 ID。"""
        if source_key not in self._messages:
            ident = f"{self.id}:message:{len(self._messages)}"
            result = MessageResult(ident, f"{self.task_token}:{ident}")
            self._messages[source_key] = result
            self.message_results.append(result)
        return self._messages[source_key]

    def advance(self):
        if self.request_current >= self.request_total:
            raise ValueError("request progress exceeds input range")
        self.request_current += 1
