BASIC_AUTH = "basic"
DIGEST_AUTH = "digest"
OAUTH1 = "oauth1"
BEARER_AUTH = "bearer"

PASSWORD_PLACEHOLDER = '*' * 16

# If any remote service does not respond within 5 minutes, time out
REQUEST_TIMEOUT = 5 * 60

ALGO_AES = 'aes'

DATA_TYPE_UNKNOWN = None

COMMCARE_DATA_TYPE_TEXT = 'cc_text'
COMMCARE_DATA_TYPE_INTEGER = 'cc_integer'
COMMCARE_DATA_TYPE_DECIMAL = 'cc_decimal'
COMMCARE_DATA_TYPE_BOOLEAN = 'cc_boolean'
COMMCARE_DATA_TYPE_DATE = 'cc_date'
COMMCARE_DATA_TYPE_DATETIME = 'cc_datetime'
COMMCARE_DATA_TYPE_TIME = 'cc_time'
COMMCARE_DATA_TYPES = (
    COMMCARE_DATA_TYPE_TEXT,
    COMMCARE_DATA_TYPE_INTEGER,
    COMMCARE_DATA_TYPE_DECIMAL,
    COMMCARE_DATA_TYPE_BOOLEAN,
    COMMCARE_DATA_TYPE_DATE,
    COMMCARE_DATA_TYPE_DATETIME,
    COMMCARE_DATA_TYPE_TIME,
)
COMMCARE_DATA_TYPES_AND_UNKNOWN = COMMCARE_DATA_TYPES + (DATA_TYPE_UNKNOWN,)

DIRECTION_IMPORT = 'in'
DIRECTION_EXPORT = 'out'
DIRECTION_BOTH = None
DIRECTIONS = (
    DIRECTION_IMPORT,
    DIRECTION_EXPORT,
    DIRECTION_BOTH,
)
