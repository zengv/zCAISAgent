from enum import StrEnum


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABSTAINED = "abstained"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


class AutonomyLevel(StrEnum):
    L0_DIRECT = "L0"
    L1_FAST = "L1"
    L2_STANDARD = "L2"
    L3_DEEP = "L3"


class SourceType(StrEnum):
    DOCUMENT = "document"
    WEB = "web"
    API = "api"
    SQL = "sql"
    TOOL = "tool"
    MEMORY = "memory"


class Support(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class NodeName(StrEnum):
    ANALYZE = "analyze"
    ROUTE = "route"
    DIRECT_ANSWER = "direct_answer"
    RETRIEVE = "retrieve"
    SYNTHESIZE = "synthesize"
    VERIFY = "verify"
    RESPOND = "respond"
    ABSTAIN = "abstain"
