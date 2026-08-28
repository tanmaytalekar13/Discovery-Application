import asyncio
import logging
from pathlib import Path

from app.config import get_settings
from app.db.client import ArcadeDBClient, ArcadeDBError

# Configure basic logging for the bootstrap script
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Path to the initial schema file
SCHEMA_PATH = Path(
    "/app/arcadedb/init/001_schema.sql"
)
async def wait_for_arcadedb(db: ArcadeDBClient, attempts: int = 30) -> None:
    """Wait until the ArcadeDB server is responsive."""
    for attempt in range(1, attempts + 1):
        try:
            if await db.check_connection():
                logger.info("ArcadeDB is ready.")
                return
        except Exception as exc:
            logger.warning(f"Waiting for ArcadeDB ({attempt}/{attempts}): {exc}")

        await asyncio.sleep(2)

    raise RuntimeError("ArcadeDB did not become ready.")


def load_schema() -> list[str]:
    """Load and parse the schema SQL file into individual statements."""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    statements: list[str] = []

    for statement in sql.split(";"):
        statement = statement.strip()
        if not statement:
            continue

        # Filter out SQL comments and empty lines
        lines = [
            line for line in statement.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]

        cleaned = "\n".join(lines).strip()
        if not cleaned:
            continue

        # Database creation is handled by the bootstrap function, 
        # so we skip it if it's in the schema file.
        if cleaned.upper().startswith("CREATE DATABASE"):
            continue

        statements.append(cleaned)

    return statements


async def bootstrap() -> None:
    """Initialize the database and apply the schema."""
    settings = get_settings()
    db = ArcadeDBClient(settings)
    database_name = settings.arcadedb_database

    logger.info("Starting ArcadeDB bootstrap...")
    await wait_for_arcadedb(db)

    databases = await db.server_databases()

    if database_name not in databases:
        logger.info(f"Creating ArcadeDB database: {database_name}")
        await db.create_database(database_name)

        # Give ArcadeDB a moment to make the new database available via the HTTP API
        for _ in range(10):
            if database_name in await db.server_databases():
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError(f"Database '{database_name}' was not created successfully.")
    else:
        logger.info(f"Database '{database_name}' already exists.")

    statements = load_schema()
    logger.info(f"Applying {len(statements)} schema statements...")

    for index, statement in enumerate(statements, start=1):
        logger.info(f"Executing schema statement {index}/{len(statements)}...")
        try:
            await db.command(language="sql", command=statement)
        except ArcadeDBError as exc:
            # CREATE statements are intentionally allowed to fail if the schema 
            # already exists. This keeps the bootstrap script idempotent.
            message = str(exc).lower()
            if "already exists" in message or "already exist" in message:
                logger.info("  -> Already exists; continuing.")
                continue
            raise

    logger.info("ArcadeDB bootstrap completed.")


if __name__ == "__main__":
    asyncio.run(bootstrap())