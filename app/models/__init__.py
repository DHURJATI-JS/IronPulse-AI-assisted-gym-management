"""Import every model so ``Base.metadata`` knows all tables (and string relationships resolve)."""

from app.models.base import EMBEDDING_DIM  # noqa: F401
from app.models.billing import OtpChallenge, Plan, SalaryStatement, Subscription, Transaction  # noqa: F401
from app.models.engagement import (  # noqa: F401
    AssistantMessage,
    ChatMessage,
    ChatThread,
    EmailOutbox,
    MisconductReport,
    Notification,
    Task,
)
from app.models.enums import *  # noqa: F401,F403
from app.models.gyms import (  # noqa: F401
    Branch,
    BranchDocument,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    Gym,
    VerificationRun,
)
from app.models.health import (  # noqa: F401
    Exercise,
    HealthAlert,
    HealthCondition,
    MetricEntry,
    SuspensionEvent,
    Video,
    WellnessCheck,
    WorkoutPlan,
)
from app.models.staff import (  # noqa: F401
    Employee,
    EmployeeDocument,
    JobEnquiry,
    JobPosting,
    TrainerClient,
    TrainerShift,
)
from app.models.live import WorkoutSession, WorkoutSessionEvent  # noqa: F401
from app.models.shop import (  # noqa: F401
    CartItem,
    Coupon,
    InventoryMovement,
    OrderStatus,
    Product,
    ProductCategory,
    ProductImage,
    ProductReview,
    Refund,
    RefundStatus,
    ShopOrder,
    ShopOrderItem,
    WishlistItem,
)
from app.models.governance import GovernanceWaiver  # noqa: F401
from app.models.remembered import RememberedAccount  # noqa: F401
from app.models.system import AICallLog, AuditLog, IpBan, Job, SecurityEvent  # noqa: F401
from app.models.trust import AccountBan, BlockedIdentity, StaffViolation, TrustIpAction  # noqa: F401
from app.models.users import (  # noqa: F401
    AuthToken,
    CustomerProfile,
    FitnessProfile,
    IdentityDocument,
    User,
    UserSession,
)
