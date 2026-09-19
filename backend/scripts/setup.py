"""
AIMScribe Backend - Setup Script
Initializes database, creates buckets, and verifies configuration.

Usage:
    python scripts/setup.py
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from dotenv import load_dotenv
load_dotenv()


def print_step(step_num, title):
    print(f"\n{'=' * 60}")
    print(f"  Step {step_num}: {title}")
    print('=' * 60)


def setup_database():
    """Initialize PostgreSQL database schema."""
    print_step(1, "Setting up PostgreSQL Database")

    try:
        import psycopg2

        # Connect to database
        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            dbname=os.getenv("POSTGRES_DB", "aimscribe_db"),
            user=os.getenv("POSTGRES_USER", "aimscribe_user"),
            password=os.getenv("POSTGRES_PASSWORD", "")
        )
        conn.autocommit = True
        cursor = conn.cursor()

        # Read and execute schema
        schema_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "init_database.sql"
        )

        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()

        # Execute schema (skip comments and empty lines)
        for statement in schema_sql.split(';'):
            statement = statement.strip()
            if statement and not statement.startswith('--'):
                try:
                    cursor.execute(statement)
                except psycopg2.errors.DuplicateTable:
                    pass  # Table already exists
                except psycopg2.errors.DuplicateObject:
                    pass  # Index already exists
                except Exception as e:
                    if "already exists" not in str(e):
                        print(f"  Warning: {e}")

        cursor.close()
        conn.close()

        print("  ✅ Database schema created successfully!")
        return True

    except Exception as e:
        print(f"  ❌ Database setup failed: {e}")
        return False


def setup_redis():
    """Verify Redis connection."""
    print_step(2, "Verifying Redis Connection")

    try:
        import redis

        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            password=os.getenv("REDIS_PASSWORD") or None
        )

        client.ping()
        print("  ✅ Redis connection verified!")
        return True

    except Exception as e:
        print(f"  ❌ Redis connection failed: {e}")
        return False


def setup_minio():
    """Create MinIO bucket if not exists."""
    print_step(3, "Setting up MinIO Storage")

    try:
        from minio import Minio

        client = Minio(
            os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            access_key=os.getenv("MINIO_ACCESS_KEY", "aimscribe"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "aimscribe123"),
            secure=os.getenv("MINIO_SECURE", "false").lower() == "true"
        )

        bucket = os.getenv("MINIO_BUCKET", "aimscribe-audio")

        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            print(f"  Created bucket: {bucket}")
        else:
            print(f"  Bucket already exists: {bucket}")

        print("  ✅ MinIO storage ready!")
        return True

    except Exception as e:
        print(f"  ❌ MinIO setup failed: {e}")
        return False


def create_directories():
    """Create required directories."""
    print_step(4, "Creating Directories")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    directories = [
        os.path.join(base_dir, "audio"),
        os.path.join(base_dir, "chromadb"),
        os.path.join(base_dir, "logs"),
        os.path.join(base_dir, "tests"),
    ]

    for directory in directories:
        if not os.path.exists(directory):
            os.makedirs(directory)
            print(f"  Created: {directory}")
        else:
            print(f"  Exists: {directory}")

    print("  ✅ Directories ready!")
    return True


def verify_azure_config():
    """Verify Azure OpenAI configuration."""
    print_step(5, "Verifying Azure OpenAI Configuration")

    config_items = [
        ("AZURE_TRANSCRIBE_ENDPOINT", "Transcription Endpoint"),
        ("AZURE_TRANSCRIBE_API_KEY", "Transcription API Key"),
        ("AZURE_TRANSCRIBE_DEPLOYMENT", "Transcription Deployment"),
        ("AZURE_NER_ENDPOINT", "NER Endpoint"),
        ("AZURE_NER_API_KEY", "NER API Key"),
        ("AZURE_NER_DEPLOYMENT", "NER Deployment"),
    ]

    all_configured = True
    for var, name in config_items:
        value = os.getenv(var, "")
        if value and "your-" not in value.lower():
            print(f"  ✅ {name}: Configured")
        else:
            print(f"  ⚠️  {name}: NOT CONFIGURED")
            all_configured = False

    if all_configured:
        print("\n  ✅ Azure OpenAI fully configured!")
    else:
        print("\n  ⚠️  Please update .env with your Azure credentials")

    return all_configured


def main():
    """Run complete setup."""
    print("\n" + "=" * 60)
    print("       AIMScribe Backend - Setup Wizard")
    print("=" * 60)

    results = []

    results.append(("Database", setup_database()))
    results.append(("Redis", setup_redis()))
    results.append(("MinIO", setup_minio()))
    results.append(("Directories", create_directories()))
    results.append(("Azure Config", verify_azure_config()))

    # Summary
    print("\n" + "=" * 60)
    print("  SETUP SUMMARY")
    print("=" * 60)

    for name, success in results:
        status = "✅" if success else "❌"
        print(f"  {status} {name}")

    all_passed = all(r[1] for r in results)

    if all_passed:
        print("\n  🎉 Setup complete! You can now run the backend.")
        print("\n  Next steps:")
        print("    1. python tests/test_azure_apis.py  (Test APIs)")
        print("    2. python src/main.py               (Start Flask server)")
        print("    3. python src/worker.py             (Start worker)")
    else:
        print("\n  ⚠️  Some components need attention. Check the errors above.")

    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
