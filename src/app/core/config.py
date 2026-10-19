from enum import Enum
from pathlib import Path

from pydantic import computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class BaseAppSettings(BaseSettings):
    """所有配置类的公共基类，统一指定 .env 路径与 UTF-8 编码。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


class AppSettings(BaseAppSettings):
    APP_NAME: str = "FastAPI Template"
    APP_DESCRIPTION: str | None = None
    APP_VERSION: str | None = "0.1.0"
    LICENSE_NAME: str | None = "MIT"
    CONTACT_NAME: str | None = None
    CONTACT_EMAIL: str | None = None


class LoggingSettings(BaseAppSettings):
    LOG_LEVEL: str = "INFO"
    DB_ECHO: bool = False


class CryptSettings(BaseAppSettings):
    SECRET_KEY: str = "change-me-please-generate-a-random-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7


class DatabaseSettings(BaseAppSettings):
    """数据库配置的标记基类，具体实现见各数据库子类。"""


class SQLiteSettings(DatabaseSettings):
    SQLITE_URI: str = "./sql_app.db"
    SQLITE_SYNC_PREFIX: str = "sqlite:///"
    SQLITE_ASYNC_PREFIX: str = "sqlite+aiosqlite:///"


class MySQLSettings(DatabaseSettings):
    MYSQL_USER: str = "username"
    MYSQL_PASSWORD: str = "password"
    MYSQL_SERVER: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_DB: str = "dbname"
    MYSQL_SYNC_PREFIX: str = "mysql://"
    MYSQL_ASYNC_PREFIX: str = "mysql+aiomysql://"
    MYSQL_URL: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def MYSQL_URI(self) -> str:  # type: ignore[prop-decorator]  # noqa: N802
        return f"{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_SERVER}:{self.MYSQL_PORT}/{self.MYSQL_DB}"


class PostgresSettings(DatabaseSettings):
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "postgres"
    POSTGRES_SYNC_PREFIX: str = "postgresql://"
    POSTGRES_ASYNC_PREFIX: str = "postgresql+asyncpg://"
    POSTGRES_URL: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def POSTGRES_URI(self) -> str:  # type: ignore[prop-decorator]  # noqa: N802
        return (
            f"{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


class FirstUserSettings(BaseAppSettings):
    ADMIN_NAME: str = "admin"
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "change-this-password"


class TestSettings(BaseAppSettings):
    """测试环境预留配置。"""


class RedisCacheSettings(BaseAppSettings):
    REDIS_CACHE_HOST: str = "localhost"
    REDIS_CACHE_PORT: int = 6379

    @computed_field  # type: ignore[prop-decorator]
    @property
    def REDIS_CACHE_URL(self) -> str:  # type: ignore[prop-decorator]  # noqa: N802
        return f"redis://{self.REDIS_CACHE_HOST}:{self.REDIS_CACHE_PORT}"


class ClientSideCacheSettings(BaseAppSettings):
    CLIENT_CACHE_MAX_AGE: int = 60


class RedisQueueSettings(BaseAppSettings):
    REDIS_QUEUE_HOST: str = "localhost"
    REDIS_QUEUE_PORT: int = 6379


class RedisRateLimiterSettings(BaseAppSettings):
    REDIS_RATE_LIMIT_HOST: str = "localhost"
    REDIS_RATE_LIMIT_PORT: int = 6379

    @computed_field  # type: ignore[prop-decorator]
    @property
    def REDIS_RATE_LIMIT_URL(self) -> str:  # type: ignore[prop-decorator]  # noqa: N802
        return f"redis://{self.REDIS_RATE_LIMIT_HOST}:{self.REDIS_RATE_LIMIT_PORT}"


class DefaultRateLimitSettings(BaseAppSettings):
    DEFAULT_RATE_LIMIT_LIMIT: int = 10
    DEFAULT_RATE_LIMIT_PERIOD: int = 3600


class SkipInfraSettings(BaseAppSettings):
    SKIP_INFRA_ON_START: bool = False


class EnvironmentOption(Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


class EnvironmentSettings(BaseAppSettings):
    ENVIRONMENT: EnvironmentOption = EnvironmentOption.LOCAL


class EmbeddingModelSettings(BaseAppSettings):
    DASHSCOPE_API_KEY: str | None = None
    DASHSCOPE_MODEL: str | None = None


class RerankModelSettings(BaseAppSettings):
    """qwen3-rerank 重排序模型配置。"""

    DASHSCOPE_RERANK_MODEL: str = "qwen3-rerank"


class DeepSeekSettings(BaseAppSettings):
    DEEPSEEK_API_KEY: str | None = None
    DEEPSEEK_MODEL: str | None = None
    DEEPSEEK_BASE_URL: str | None = None


class RagEvaluationSettings(BaseAppSettings):
    """RAG 评估（RAGAS）配置。"""

    RAGAS_LLM_MODEL: str = "deepseek-chat"
    RAGAS_METRICS: str = "faithfulness,answer_relevancy,context_precision,context_recall"


class LangSmithSettings(BaseAppSettings):
    """可观测性：LangSmith 追踪配置。

    LangChain / LangGraph 会自动读取 ``LANGSMITH_*`` 环境变量并上报分层追踪，
    应用启动时由 :func:`app.core.observability.apply_langsmith_settings` 把这里的
    配置写入进程环境变量。
    """

    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str | None = None
    LANGSMITH_PROJECT: str | None = "weather-agent"
    LANGSMITH_ENDPOINT: str | None = "https://api.smith.langchain.com"


class ObservabilitySettings(BaseAppSettings):
    """可观测性的 Metric 层配置：本地指标窗口与 Token 成本估算单价。

    可观测性分三层：Trace（LangSmith）、Metric（延迟/成本/错误率）、Log（结构化日志）。
    本配置只服务于进程内的指标采集：最近多少轮纳入统计，以及估算成本时的 Token 单价。
    单价默认值按 deepseek-chat 的公开价位量级设置（元/千 Token），仅用于估算，
    不同模型/不同折扣请按实际账单调整。
    """

    #: 进程内保留的最近轨迹条数（同时决定指标统计窗口）。
    OBSERVABILITY_TRACE_WINDOW: int = 200
    #: 估算成本：每千输入 Token 的单价（元）。
    OBSERVABILITY_COST_PER_1K_INPUT: float = 0.002
    #: 估算成本：每千输出 Token 的单价（元）。
    OBSERVABILITY_COST_PER_1K_OUTPUT: float = 0.008
    #: 结构化日志开关；关闭时退化为普通 logger 调用。
    OBSERVABILITY_STRUCTURED_LOG: bool = True


class AgentEvaluationSettings(BaseAppSettings):
    """多 Agent 评估配置：核心指标验收阈值与 LLM-as-Judge 判官。"""

    #: LLM-as-Judge 使用的裁判模型（默认复用 DeepSeek）。
    AGENT_JUDGE_MODEL: str = "deepseek-chat"
    #: 是否默认启用 LLM-as-Judge；关闭时只算规则类指标，不调用外部模型。
    AGENT_JUDGE_ENABLED: bool = True
    #: 核心指标的验收阈值，用于评估结果里的 pass 判定。
    AGENT_TARGET_COMPLETION_RATE: float = 0.9
    AGENT_TARGET_TOOL_ACCURACY: float = 0.95
    AGENT_TARGET_CONFLICT_RATE: float = 0.05
    AGENT_TARGET_AVG_LATENCY: float = 5.0


class UploadSettings(BaseAppSettings):
    """文件上传配置，用于后续 LangChain RAG 离线阶段的文档加载与处理。"""

    UPLOAD_DIR: Path = PROJECT_ROOT / "storage" / "uploads"
    MAX_UPLOAD_SIZE_MB: int = 100


class DocumentSettings(BaseAppSettings):
    """本地文档目录配置，供 LangChain RAG 离线阶段按路径加载文档。"""

    DOCUMENT_DIR: Path = PROJECT_ROOT / "storage" / "documents"
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 150


class MilvusSettings(BaseAppSettings):
    """Milvus 向量数据库连接配置。

    ``ENABLE_MILVUS`` 默认关闭，未部署 Milvus 时不会发起连接，避免启动或调用报错。
    """

    ENABLE_MILVUS: bool = False
    # 连接成功后是否自动创建 RAG 所需的集合与索引；关闭后需手动调用
    # ``rag_collections.ensure_collections`` 完成初始化。
    AUTO_CREATE_RAG_COLLECTIONS: bool = False
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_URI: str | None = None
    MILVUS_TOKEN: str | None = None
    MILVUS_USER: str | None = None
    MILVUS_PASSWORD: str | None = None
    MILVUS_DB_NAME: str = "default"


class QWeatherJwtSettings(BaseAppSettings):
    """和风天气（QWeather）JWT 认证配置。"""

    QWEATHER_JWT_ALGORITHM: str = "EdDSA"
    QWEATHER_JWT_PRIVATE_KEY: str | None = None
    QWEATHER_JWT_KEY_ID: str | None = None
    QWEATHER_JWT_SUBJECT: str | None = None
    QWEATHER_JWT_ISSUER: str | None = None
    QWEATHER_API_HOST: str | None = None  # API Host 订阅域名，如 xxx.re.qweatherapi.com
    QWEATHER_JWT_TTL_SECONDS: int = 86400  # 86400 秒 = 24 小时，和风天气要求 exp - iat 不超过 24 小时
    QWEATHER_JWT_LEEWAY_SECONDS: int = 30


class DingTalkSettings(BaseAppSettings):
    """钉钉 DWS CLI 配置（钉协助手智能体）。"""

    DINGTALK_DWS_BIN: str = "dws"  # dws 可执行文件，可在 .env 中用绝对路径覆盖
    DINGTALK_ENABLE_WRITE: bool = False  # 写操作总开关：发消息/DING/创建日程等，默认关闭
    DINGTALK_TIMEOUT_SECONDS: int = 30  # dws 子命令执行超时（秒）
    DINGTALK_OUTPUT_MAX_CHARS: int = 4000  # CLI 输出截断长度
    DINGTALK_ROBOT_WEBHOOK_TOKEN: str | None = None  # 群自定义机器人 Webhook token（用于机器人消息）


class ECommerceSettings(BaseAppSettings):
    """电商导购智能体（带三层记忆）配置。

    记忆与商品源均通过后端字符串解耦，切换时只需修改配置，无需改动业务代码：

    - 短期记忆（Checkpointer）：``memory`` 进程内，``postgres`` 持久化。
    - 短期记忆窗口管理：``truncate`` 截断最近 N 条、``token`` 按 token 预算裁剪、
      ``summary`` 旧消息总结压缩进长期画像，三种策略可配置切换。
    - 长期记忆（MemoryStore）：``inmemory`` 进程内零依赖，``postgres`` 走 pgvector。
    - 商品源（ProductSource）：``builtin`` 内置目录、``pgvector`` 向量 RAG、
      ``milvus`` 向量 RAG、``hybrid`` RAG + 目录兜底。
    """

    # ---------------- 商品源 ----------------
    ECOMMERCE_PRODUCT_SOURCE: str = "builtin"  # builtin | pgvector | milvus | hybrid
    ECOMMERCE_PRODUCT_TOP_K: int = 5  # 商品检索返回条数
    ECOMMERCE_PRODUCT_MIN_SIMILARITY: float = 0.3  # 商品向量检索相似度阈值

    # ---------------- 短期记忆（Checkpointer） ----------------
    ECOMMERCE_CHECKPOINT_BACKEND: str = "memory"  # memory | postgres
    ECOMMERCE_WINDOW_STRATEGY: str = "truncate"  # 短期记忆窗口策略：truncate | token | summary | hybrid
    ECOMMERCE_WINDOW_MAX_MESSAGES: int = 20  # truncate 策略保留的最近消息条数
    ECOMMERCE_WINDOW_MAX_TOKENS: int = 4000  # token 策略的上下文预算

    # ---------------- 长期记忆（MemoryStore） ----------------
    ECOMMERCE_MEMORY_BACKEND: str = "inmemory"  # inmemory | postgres
    ECOMMERCE_MEMORY_TOP_K: int = 3  # 长期记忆召回条数
    ECOMMERCE_MEMORY_DECAY_DAYS: int = 30  # 记忆时间衰减半衰期（天）
    ECOMMERCE_MEMORY_MIN_RELEVANCE: float = 0.2  # 召回语义相关度阈值，低于该值视为无关记忆
    ECOMMERCE_MEMORY_MAX_PER_USER: int = 100  # 单用户长期记忆条数上限，超过后淘汰低分记忆（0 表示不限）
    ECOMMERCE_PROFILE_MAX_PREFS: int = 6  # 画像偏好键数上限，超出后按 query 相关性裁剪（0 表示不裁剪）
    ECOMMERCE_PROFILE_SUMMARY_MAX_CHARS: int = 400  # 画像历史摘要注入上限（0 表示不截断）

    # ---------------- 窗口压缩（summary / hybrid） ----------------
    ECOMMERCE_WINDOW_SUMMARY_KEEP: int = 10  # summary 策略保留的最近消息条数
    ECOMMERCE_WINDOW_SUMMARY_HARD_LIMIT: int = 50  # summary 策略硬上限：总结失败时强制丢弃，防止无界增长
    ECOMMERCE_WINDOW_HYBRID_KEEP: int = 12  # hybrid 策略保留的最近消息条数（原文）
    ECOMMERCE_WINDOW_HYBRID_MAX_TOKENS: int = 3000  # hybrid 策略喂给大模型的 token 预算兜底
    ECOMMERCE_WINDOW_HYBRID_HARD_LIMIT: int = 60  # hybrid 策略硬上限：超过且总结失败时强制丢弃
    ECOMMERCE_WINDOW_TOOL_MAX_CHARS: int = 800  # 工具输出截断字符数，避免大段工具结果撑爆上下文


class VoiceSettings(BaseAppSettings):
    """实时语音导购配置：Qwen3-ASR 实时语音识别与 TTS 语音合成。"""

    VOICE_ASR_MODEL: str = "qwen3-asr-flash-realtime"  # Qwen3-ASR 实时语音识别模型
    VOICE_ASR_LANGUAGE: str = "zh"  # 识别语言
    VOICE_ASR_TURN_MODE: str = "manual"  # manual（前端静音后 commit）| server_vad
    VOICE_ASR_SILENCE_DURATION_MS: int = 800  # 前端静音分句时长（毫秒）
    VOICE_ASR_MAX_WORKERS: int = 4  # 识别会话建连/关闭线程池大小
    VOICE_ASR_TIMEOUT_S: float = 10.0  # 识别会话建连/关闭超时（秒）
    VOICE_SKIP_MEMORY_WRITE: bool = True  # 语音回复跳过「记忆提取」额外 LLM 调用
    VOICE_TTS_BACKEND: str = "dashscope"  # dashscope | local
    VOICE_TTS_MODEL: str = "cosyvoice-v2"  # 语音合成模型
    VOICE_TTS_VOICE: str = "longxiaochun_v2"  # 音色
    VOICE_TTS_MAX_WORKERS: int = 4  # 语音合成线程池大小
    VOICE_TTS_TIMEOUT_S: float = 15.0  # 语音合成超时（秒）


class BISettings(BaseAppSettings):
    """智能问数（Text-to-SQL）配置。

    采用「黑名单 + 全局只读」模式：默认开放数据库内所有表，仅排除配置中列出的
    表与敏感列。表结构在运行时通过数据库 catalog 动态内省（见
    core/agents/bi/schema.py），因此之后新建的表会自动进入问数范围，无需改代码。
    """

    # 禁止问数的表（逗号分隔的表名，大小写不敏感）。留空表示默认开放所有表。
    BI_TABLE_BLOCKLIST: str = ""
    # 敏感列（逗号分隔的列名）：SQL 中出现这些列时直接拒绝查询。
    BI_SENSITIVE_COLUMNS: str = "password_hash,embedding"
    # 单次查询返回的最大行数：超过则自动夹逼 LIMIT。
    BI_MAX_ROWS: int = 100
    # 单次查询执行超时（秒）。
    BI_QUERY_TIMEOUT_SECONDS: float = 10.0
    # 表结构内省结果的缓存时长（秒）。期间新建的表会延迟可见，可调用刷新接口立即生效。
    BI_SCHEMA_CACHE_TTL_SECONDS: int = 300
    # SQL 生成 / 执行失败后的自修正重试次数（0 表示不重试）。
    BI_SQL_MAX_RETRIES: int = 2
    # SQL 生成所用的大模型温度：问数建议使用低温度以获得稳定输出。
    BI_TEMPERATURE: float = 0.0
    # 是否在回答时顺带生成图表配置（由结果列自动推导）。
    BI_CHART_ENABLED: bool = True
    # 多轮对话保留的历史消息条数上限（注入提示词时只取最近若干条）。
    BI_HISTORY_MESSAGES: int = 6
    # 生成 SQL / 解读结果所用的大模型（OpenAI 兼容）。
    # 三项都留空时复用 DEEPSEEK_* 配置；需要换供应商（如内网网关）时只改这三项。
    BI_LLM_MODEL: str | None = None
    BI_LLM_BASE_URL: str | None = None
    BI_LLM_API_KEY: str | None = None


class OrchestratorSettings(BaseAppSettings):
    """多智能体编排器配置。"""

    ORCHESTRATOR_SUBAGENT_TRANSPORT: str = "inprocess"  # inprocess | a2a


class A2ASettings(BaseAppSettings):
    """A2A 协议层配置。"""

    A2A_PUBLIC_BASE_URL: str = "http://localhost:8888"  # Agent Card 对外暴露的基础 URL（生产须填真实公网地址）
    A2A_TASK_STORE: str = "postgres"  # memory | sqlite | postgres（任务持久化后端）
    A2A_POSTGRES_URL: str | None = None  # A2A_TASK_STORE=postgres 时的异步连接串（postgresql+asyncpg://...）
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "postgres"
    POSTGRES_SYNC_PREFIX: str = "postgresql://"
    POSTGRES_ASYNC_PREFIX: str = "postgresql+asyncpg://"

    @model_validator(mode="after")
    def _default_a2a_postgres_url(self) -> "A2ASettings":
        """未显式配置 A2A_POSTGRES_URL 时，按 POSTGRES_* 配置拼接异步连接串。"""
        if not self.A2A_POSTGRES_URL:
            self.A2A_POSTGRES_URL = (
                f"{self.POSTGRES_ASYNC_PREFIX}{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        return self

    # A2A 客户端：连接池 / 超时 / 重试
    A2A_CLIENT_TIMEOUT_SECONDS: float = 30.0  # 单次请求整体超时（连接/读/写/池）
    A2A_CLIENT_MAX_CONNECTIONS: int = 100  # 每个子智能体连接池最大连接数
    A2A_CLIENT_MAX_KEEPALIVE_CONNECTIONS: int = 20  # 连接池最大空闲保活连接数
    A2A_CLIENT_MAX_RETRIES: int = 2  # 连接建立/首个响应前的瞬态网络错误重试次数
    A2A_CLIENT_RETRY_BACKOFF_SECONDS: float = 0.5  # 重试指数退避的基数（秒）

    @field_validator("A2A_PUBLIC_BASE_URL", mode="after")
    @classmethod
    def _normalize_public_base_url(cls, value: str) -> str:
        """去掉末尾斜杠，并确保是绝对 http(s) 地址。"""
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("A2A_PUBLIC_BASE_URL 必须是绝对 http(s) 地址")
        return value


class AnnouncementSettings(BaseAppSettings):
    """对外公告发布审批智能体（Human-in-the-loop）配置。"""

    # 目标群名：审批通过后发布群公告；也可用 conversation_id 直接指定跳过群名解析
    ANNOUNCEMENT_NOTICE_GROUP: str = "刘夏的师生群"
    ANNOUNCEMENT_NOTICE_CONVERSATION_ID: str | None = None
    # 进入人工审批时是否推送机器人通知
    ANNOUNCEMENT_NOTIFY_ENABLED: bool = True
    # 机器人安全关键词：钉钉机器人设置了关键词时，通知文本必须包含其一才能送达
    ANNOUNCEMENT_NOTIFY_KEYWORD: str | None = None
    # 命中这些关键词的公告强制升级为人工审批（逗号分隔）
    ANNOUNCEMENT_RISK_KEYWORDS: str = "全体,紧急,重要,官方,正式,通知"


class Settings(
    AppSettings,
    LoggingSettings,
    PostgresSettings,
    CryptSettings,
    FirstUserSettings,
    TestSettings,
    RedisCacheSettings,
    ClientSideCacheSettings,
    RedisQueueSettings,
    RedisRateLimiterSettings,
    DefaultRateLimitSettings,
    SkipInfraSettings,
    EnvironmentSettings,
    EmbeddingModelSettings,
    RerankModelSettings,
    DeepSeekSettings,
    RagEvaluationSettings,
    LangSmithSettings,
    ObservabilitySettings,
    AgentEvaluationSettings,
    UploadSettings,
    DocumentSettings,
    MilvusSettings,
    QWeatherJwtSettings,
    DingTalkSettings,
    ECommerceSettings,
    VoiceSettings,
    BISettings,
    OrchestratorSettings,
    A2ASettings,
    AnnouncementSettings,
):
    """聚合所有配置的顶层配置对象。"""


settings = Settings()
