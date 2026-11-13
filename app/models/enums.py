"""All enumerations used by the data model (stored as their string values)."""

from __future__ import annotations

from enum import StrEnum


# ------------------------------------------------------------------ accounts
class Role(StrEnum):
    SUPERADMIN = "superadmin"  # platform operator (security centre, review queue)
    OWNER = "owner"  # gym owner / gym admin
    STAFF = "staff"  # employee with a login (trainer, receptionist, accountant ...)
    CUSTOMER = "customer"


class IdStatus(StrEnum):
    MISSING = "missing"
    PENDING = "pending"
    PROCESSING = "processing"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class IdDocType(StrEnum):
    NATIONAL_ID = "national_id"
    PASSPORT = "passport"
    DRIVING_LICENSE = "driving_license"
    AADHAAR = "aadhaar"
    PAN = "pan"
    VOTER_ID = "voter_id"
    RESIDENCE_PERMIT = "residence_permit"
    STUDENT_ID = "student_id"
    OTHER = "other"


class TokenPurpose(StrEnum):
    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"
    INVITE = "invite"  # owner registered a customer -> set your password
    STAFF_INVITE = "staff_invite"  # owner added staff -> create/link your account


# ------------------------------------------------------------------ gyms
class VerificationStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PROCESSING = "processing"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    AI_UNAVAILABLE = "ai_unavailable"  # will be retried automatically


class BranchDocType(StrEnum):
    BUSINESS_REGISTRATION = "business_registration"
    TAX_REGISTRATION = "tax_registration"
    TRADE_LICENSE = "trade_license"
    FIRE_SAFETY_CERTIFICATE = "fire_safety_certificate"
    SECURITY_CERTIFICATE = "security_certificate"
    OCCUPANCY_CERTIFICATE = "occupancy_certificate"
    LIABILITY_INSURANCE = "liability_insurance"
    HEALTH_SAFETY_CERTIFICATE = "health_safety_certificate"
    OTHER = "other"


class PhotoKind(StrEnum):
    MACHINES = "machines"
    OVERALL = "overall"
    OTHER = "other"


# ------------------------------------------------------------------ staff
class EmployeeRole(StrEnum):
    TRAINER = "trainer"
    RECEPTIONIST = "receptionist"
    CLEANER = "cleaner"
    ACCOUNTANT = "accountant"
    MANAGER = "manager"
    SOFTWARE_DEVELOPER = "software_developer"
    NUTRITIONIST = "nutritionist"
    PHYSIOTHERAPIST = "physiotherapist"
    SECURITY = "security"
    MAINTENANCE = "maintenance"
    OTHER = "other"


class WorkMode(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    BOTH = "both"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    FIXED_TERM = "fixed_term"
    INTERN = "intern"


class EmploymentStatus(StrEnum):
    INVITED = "invited"  # waiting for the person to create/link their own account
    PENDING_VERIFICATION = "pending_verification"  # account linked; ID / documents being verified
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


class ShiftMode(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class EnquiryStatus(StrEnum):
    NEW = "new"
    REVIEWED = "reviewed"
    CONTACTED = "contacted"
    REJECTED = "rejected"
    HIRED = "hired"


# ------------------------------------------------------------------ billing
class PlanMode(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    COMBO = "combo"  # online + offline


class SubscriptionStatus(StrEnum):
    PENDING_OTP = "pending_otp"
    PENDING_PAYMENT = "pending_payment"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    FAILED = "failed"


class PurchaseChannel(StrEnum):
    ONLINE_CHECKOUT = "online_checkout"
    BRANCH_DESK_OTP = "branch_desk_otp"
    OWNER_MANUAL = "owner_manual"


class TxDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class TxCategory(StrEnum):
    SUBSCRIPTION = "subscription"
    SHOP_SALE = "shop_sale"
    SALARY = "salary"
    GRATUITY = "gratuity"
    TAX = "tax"
    REFUND = "refund"
    PAYOUT_FEE = "payout_fee"
    EXPENSE = "expense"
    OTHER = "other"


class TxStatus(StrEnum):
    CREATED = "created"
    PENDING = "pending"
    AWAITING_PROOF = "awaiting_proof"
    PROOF_REVIEW = "proof_review"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StatementStatus(StrEnum):
    DRAFT = "draft"
    DUE = "due"
    AWAITING_PROOF = "awaiting_proof"
    PROOF_REVIEW = "proof_review"
    PAID = "paid"
    VOID = "void"


# ------------------------------------------------------------------ health & coaching
class Goal(StrEnum):
    CALISTHENICS = "calisthenics"
    BULKING = "bulking"
    WEIGHT_LOSS = "weight_loss"
    GENERAL_FITNESS = "general_fitness"


class FitnessLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ConditionKind(StrEnum):
    CONDITION = "condition"  # illness / chronic condition (asthma, diabetes, hypertension ...)
    ALLERGY = "allergy"
    MEDICATION = "medication"
    INJURY = "injury"


class AnomalyLevel(StrEnum):
    NONE = "none"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    LIFE_THREATENING = "life_threatening"


class StressLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


# ------------------------------------------------------------------ engagement
class TaskKind(StrEnum):
    TASK = "task"
    REMINDER = "reminder"
    EVENT = "event"
    WORKOUT = "workout"
    BEDTIME = "bedtime"


class NotificationCategory(StrEnum):
    ACCOUNT = "account"
    SECURITY = "security"
    PAYMENT = "payment"
    HEALTH = "health"
    REMINDER = "reminder"
    MESSAGE = "message"
    VERIFICATION = "verification"
    STAFF = "staff"
    SYSTEM = "system"


class Severity(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class EmailStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    LOGGED = "logged"  # no SMTP configured - written to var/mail
    FAILED = "failed"


class ThreadStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ReportStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DECLINED = "declined"


# ------------------------------------------------------------------ system
class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"  # stopped because its requester left (exit policy 'cancel') or by an admin
    PAUSED = "paused"  # deferred while its requester is away (exit policy 'pause'); resumes when they return


class ThreatCategory(StrEnum):
    SQLI = "sqli"
    XSS = "xss"
    TRAVERSAL = "traversal"
    COMMAND_INJECTION = "command_injection"
    SCANNER = "scanner"
    PROBE = "probe"
    BRUTEFORCE = "bruteforce"
    RATE_LIMIT = "rate_limit"
    CSRF = "csrf"
    UPLOAD = "upload"
    FRAUD = "fraud"
    AI_ABUSE = "ai_abuse"  # per-user AI spam/flooding that reached a long cooldown (app/ai/guard.py)
    OTHER = "other"
