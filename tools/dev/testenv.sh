# Local test environment. Usage: source tools/dev/testenv.sh <dbname_test>
# Needs a PostgreSQL 17 + pgvector container, e.g.
#   docker run -d --name gaoji-test -e POSTGRES_USER=qq_bot -e POSTGRES_PASSWORD=$GAOJI_TEST_PASSWORD \
#     -p 127.0.0.1:25440:5432 pgvector/pgvector:pg17
# then create the test database (name must end in _test or _acceptance) and migrate it:
#   AI_POSTGRES_DSN=$TEST_POSTGRES_DSN python -m src.bot_storage.cli upgrade
: "${GAOJI_TEST_PASSWORD:?set GAOJI_TEST_PASSWORD to the container password}"
_base=postgresql://qq_bot:$GAOJI_TEST_PASSWORD@127.0.0.1:${GAOJI_TEST_PORT:-25440}
export PYTHON_DOTENV_DISABLED=1
export AI_POSTGRES_SCHEMA=qq_bot_test TEST_POSTGRES_SCHEMA=qq_bot_test
export TEST_POSTGRES_DSN=$_base/${1:?database name}
export TEST_FILE_ACCEPTANCE_DSN=$TEST_POSTGRES_DSN TEST_OPS_POSTGRES_DSN=$TEST_POSTGRES_DSN TEST_SECURITY_POSTGRES_DSN=$TEST_POSTGRES_DSN
# Must differ from TEST_POSTGRES_DSN, or the storage tests refuse to run.
export AI_POSTGRES_DSN=$_base/qq_bot
export AI_STATE_DIR=$(mktemp -d)
unset _base
